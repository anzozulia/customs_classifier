"""GET /api/history · GET /api/history/{id} · GET /api/history/export.csv.

The read side of the register. ChatKit's own sidebar lists CONVERSATIONS; this lists
CLASSIFICATIONS — the input, the codes, the tool calls that produced them and, deliberately,
the failures. A turn that errored is a row here rather than something that quietly never
happened.

Three rules hold this file together:

* **Every statement carries `user_id`.** Same boundary as `app/chat/store.py`: a history leak
  is the Store bug in a different table. The two child queries in the detail path could have
  keyed off an id that was just proved to be the caller's, but they join back to
  `classification` and repeat the predicate anyway — so "does this statement mention user_id"
  stays a property that can be checked mechanically rather than argued about per query.

* **404, never 403.** "Not yours" and "does not exist" must be the same answer or an
  authenticated session becomes an existence oracle over `secrets.token_urlsafe` ids. (The SPA
  reinforces this: `web/src/lib/api.ts` treats 403 as a dead session and redirects to /login,
  so a 403 here would log the user out for clicking someone else's link.)

* **The JSON is the frontend's type file.** `web/src/lib/history.ts` is snake_case on purpose
  — there is no camelCase mapping layer — and `tests/test_history_api.py` compares these
  models' key sets against that file so the contract cannot drift silently.
"""

from __future__ import annotations

import base64
import csv
import io
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.auth.deps import CurrentUser, current_user
from app.db import acquire, get_pool

router = APIRouter(
    prefix="/api",
    tags=["history"],
    # Structural, not decorative: a route added to this router later cannot forget to
    # authenticate. FastAPI caches a dependency per request, so the per-handler
    # `Depends(current_user)` below resolves to the same call and costs no second query.
    dependencies=[Depends(current_user)],
)

_DEFAULT_LIMIT: Final = 20
_MAX_LIMIT: Final = 100

# The DB column is the union of two vocabularies: the agent's ("conversation") and the
# record's. The frontend knows four values, so "conversation" — a turn that completed and
# emitted no codes — collapses to "classified" here. Anything the column grows later degrades
# to a valid badge instead of rendering `undefined` in `OUTCOME_LABEL[outcome]`.
_OUTCOME: Final[dict[str, str]] = {
    "pending": "pending",
    "classified": "classified",
    "clarification": "clarification",
    "error": "error",
    "conversation": "classified",
}

# ---------------------------------------------------------------------------- the contract


Outcome = Literal["classified", "clarification", "error", "pending"]


class HistoryCode(BaseModel):
    code: str
    description: str
    #: Ancestors concatenated. 2,473 of the 10,490 leaf descriptions are literally "інші", so
    #: the UI never renders a bare `description` and this column is not optional.
    full_path: str
    is_primary: bool


class HistoryEntry(BaseModel):
    id: str
    created_at: datetime
    input_text: str
    outcome: Outcome
    duration_ms: int | None = None
    thread_id: str | None = None
    codes: list[HistoryCode] = Field(default_factory=list)
    clarification_question: str | None = None
    error_class: str | None = None


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] | None = None
    summary: str | None = None
    duration_ms: int | None = None
    ok: bool = True
    error: str | None = None


class HistoryDetail(HistoryEntry):
    """The audit view: the run parameters an answer stops being interpretable without."""

    model: str | None = None
    prompt_version: str | None = None
    dataset_sha256: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


class HistoryPage(BaseModel):
    items: list[HistoryEntry]
    #: The keyset cursor to pass back as `before`. Null means this was the last page — the
    #: SPA hides "Показати ще" on null, so it must not be set speculatively.
    next_before: str | None = None


# ---------------------------------------------------------------------------- the cursor


def _encode_cursor(created_at: datetime, ident: str) -> str:
    """base64url of "<iso>|<id>".

    The cursor has to carry BOTH halves of the sort key. A bare id would mean a lookup on
    every page just to learn its timestamp (and a 404 mid-scroll once the row is deleted), and
    a bare timestamp is not unique — two classifications started in the same millisecond would
    make the page boundary ambiguous, which is precisely the case keyset pagination exists to
    get right. base64 because the payload contains `|`, `:` and `+` and travels in a query
    string; the padding is stripped because it means nothing and would only be percent-encoded.
    """
    raw = f"{created_at.isoformat()}|{ident}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
        iso, separator, ident = raw.partition("|")
        if not separator or not ident:
            raise ValueError("cursor carries no id half")
        return datetime.fromisoformat(iso), ident
    except ValueError as exc:
        # binascii.Error and UnicodeDecodeError are both ValueError subclasses, so this one
        # clause covers "not base64", "not UTF-8", "not a timestamp" and "no id".
        # 400, not 500: the value is client-supplied even though we minted it.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Невірний курсор.") from exc


# ---------------------------------------------------------------------------- the filters


def _like(q: str) -> str:
    """A LIKE pattern for a user's raw search string.

    `%` and `_` are wildcards, so a search for "100_%" must not match everything. The escape
    character is declared explicitly in the SQL (`ESCAPE '\\'`) rather than relying on the
    Postgres default, because it is the default in exactly one of the two engines that run
    these statements — the other is the SQLite double in the tests.
    """
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _scope(user_id: int, q: str, before: str | None) -> tuple[str, list[Any]]:
    """The WHERE shared by the page and the export. `c` is the `classification` alias.

    Returns the SQL and its positional parameters; the caller appends its own ($LIMIT) after.
    """
    params: list[Any] = [user_id]
    clauses = ["c.user_id = $1"]

    if before is not None:
        created_at, ident = _decode_cursor(before)
        params += [created_at, ident]
        # Row-value comparison, which is the whole keyset: it is a single predicate over
        # (created_at DESC, id DESC) and matches the composite index, where the hand-expanded
        # `a < x OR (a = x AND b < y)` form does not.
        # The casts are not decoration: inside a row-wise comparison Postgres has no operand
        # to infer a bare parameter's type from and answers "could not determine data type".
        clauses.append(
            f"(c.created_at, c.id) < (${len(params) - 1}::timestamptz, ${len(params)}::text)"
        )

    if q:
        params.append(_like(q))
        # Two dimensions in one box, because that is how the register is actually searched:
        # "плівка" is the description the user typed, "3919" is the code they got back. The
        # trigram GIN index on input_text serves the left side; the right side is an index
        # lookup on the child table's (classification_id, position) key.
        clauses.append(
            f"(c.input_text ILIKE ${len(params)} ESCAPE '\\'"
            f" OR EXISTS (SELECT 1 FROM classification_code hit"
            f"            WHERE hit.classification_id = c.id"
            f"              AND hit.code ILIKE ${len(params)} ESCAPE '\\'))"
        )

    return " AND ".join(clauses), params


# ---------------------------------------------------------------------------- row mapping


def _arguments(raw: Any) -> dict[str, Any] | None:
    """JSONB comes back as `str`: no codec is installed (see the note in `app/db.py`)."""
    if raw is None or isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _code(row: Any) -> HistoryCode:
    return HistoryCode(
        code=row["code"],
        description=row["description"],
        full_path=row["full_path"],
        is_primary=bool(row["is_primary"]),
    )


def _entry_fields(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "input_text": row["input_text"],
        "outcome": _OUTCOME.get(row["outcome"], "classified"),
        "duration_ms": row["duration_ms"],
        "thread_id": row["thread_id"],
        "clarification_question": row["clarification_question"],
        "error_class": row["error_class"],
    }


# ---------------------------------------------------------------------------- the routes
#
# NB `/history/export.csv` is declared BEFORE `/history/{id}`. Starlette matches in
# registration order and `{id}` happily swallows the literal segment "export.csv", so the
# other order silently turns the export button into a 404.


@router.get("/history", response_model=HistoryPage)
async def list_history(
    user: Annotated[CurrentUser, Depends(current_user)],
    q: Annotated[str, Query(max_length=200)] = "",
    before: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query()] = _DEFAULT_LIMIT,
) -> HistoryPage:
    """One page of the register, newest first.

    `limit` is clamped rather than 422'd: it is a display preference, and a hand-edited URL
    asking for 5,000 rows should get 100 of them, not an error the SPA renders as a red bar.
    """
    limit = max(1, min(limit, _MAX_LIMIT))
    where, params = _scope(user.id, q.strip(), before)

    # limit + 1: the extra row is how `next_before` learns there IS a next page without a
    # second COUNT query. It is trimmed off before anything is serialized.
    params.append(limit + 1)
    rows = await get_pool().fetch(
        f"""
        WITH page AS (
            SELECT c.id, c.created_at, c.input_text, c.outcome, c.duration_ms, c.thread_id,
                   c.clarification_question, c.error_class
            FROM classification c
            WHERE {where}
            ORDER BY c.created_at DESC, c.id DESC
            LIMIT ${len(params)}
        )
        SELECT p.id, p.created_at, p.input_text, p.outcome, p.duration_ms, p.thread_id,
               p.clarification_question, p.error_class,
               cc.code, cc.description, cc.full_path, cc.is_primary
        FROM page p
        LEFT JOIN classification_code cc ON cc.classification_id = p.id
        ORDER BY p.created_at DESC, p.id DESC, cc.position
        """,
        *params,
    )

    # One statement, one round trip: the join fans each classification out into one row per
    # code, and the ordering guarantees the fan-out arrives contiguously, so regrouping is a
    # dictionary lookup rather than a second query per row.
    entries: list[HistoryEntry] = []
    by_id: dict[str, HistoryEntry] = {}
    for row in rows:
        entry = by_id.get(row["id"])
        if entry is None:
            entry = HistoryEntry(**_entry_fields(row))
            by_id[row["id"]] = entry
            entries.append(entry)
        if row["code"] is not None:  # LEFT JOIN: a pending or errored row has no codes
            entry.codes.append(_code(row))

    if len(entries) <= limit:
        return HistoryPage(items=entries, next_before=None)

    page = entries[:limit]
    return HistoryPage(items=page, next_before=_encode_cursor(page[-1].created_at, page[-1].id))


@router.get("/history/export.csv")
async def export_history_csv(
    user: Annotated[CurrentUser, Depends(current_user)],
    q: Annotated[str, Query(max_length=200)] = "",
) -> StreamingResponse:
    """The whole filtered register as CSV, streamed.

    Unpaginated on purpose — that is what an export is for — which is also why it must never
    be assembled in memory first. See `_csv_stream`.
    """
    where, params = _scope(user.id, q.strip(), None)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    return StreamingResponse(
        _csv_stream(where, params),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="uktzed-history-{stamp}.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/history/{entry_id}", response_model=HistoryDetail)
async def get_history_entry(
    entry_id: str,
    user: Annotated[CurrentUser, Depends(current_user)],
) -> HistoryDetail:
    """One classification, with the run parameters and the tool calls behind it."""
    row = await get_pool().fetchrow(
        """
        SELECT c.id, c.created_at, c.input_text, c.outcome, c.duration_ms, c.thread_id,
               c.clarification_question, c.error_class,
               c.model, c.prompt_version, c.dataset_sha256,
               c.tokens_in, c.tokens_out, c.cost_usd
        FROM classification c
        WHERE c.id = $1 AND c.user_id = $2
        """,
        entry_id,
        user.id,
    )
    if row is None:
        # Unowned and non-existent are the same answer. See the module docstring.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Класифікацію не знайдено.")

    code_rows = await get_pool().fetch(
        """
        SELECT cc.code, cc.description, cc.full_path, cc.is_primary
        FROM classification_code cc
        JOIN classification c ON c.id = cc.classification_id
        WHERE cc.classification_id = $1 AND c.user_id = $2
        ORDER BY cc.position
        """,
        entry_id,
        user.id,
    )
    tool_rows = await get_pool().fetch(
        """
        SELECT tc.name, tc.arguments, tc.summary, tc.duration_ms, tc.ok, tc.error
        FROM classification_tool_call tc
        JOIN classification c ON c.id = tc.classification_id
        WHERE tc.classification_id = $1 AND c.user_id = $2
        ORDER BY tc.position
        """,
        entry_id,
        user.id,
    )

    return HistoryDetail(
        **_entry_fields(row),
        codes=[_code(code_row) for code_row in code_rows],
        model=row["model"],
        prompt_version=row["prompt_version"],
        dataset_sha256=row["dataset_sha256"],
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        # NUMERIC(10,6) arrives as `decimal.Decimal`, which `json.dumps` cannot serialize.
        cost_usd=float(row["cost_usd"]) if row["cost_usd"] is not None else None,
        tool_calls=[
            ToolCall(
                name=tool_row["name"],
                arguments=_arguments(tool_row["arguments"]),
                summary=tool_row["summary"],
                duration_ms=tool_row["duration_ms"],
                ok=bool(tool_row["ok"]),
                error=tool_row["error"],
            )
            for tool_row in tool_rows
        ],
    )


# ---------------------------------------------------------------------------- the export

# Ukrainian, because the file is opened by the person who ran the classifications. The status
# column mirrors OUTCOME_LABEL in web/src/lib/history.ts.
_CSV_HEADER: Final = (
    "ідентифікатор",
    "створено",
    "статус",
    "запит",
    "коди",
    "повний шлях",
    "уточнення",
    "помилка",
    "тривалість (мс)",
    "модель",
    "вартість (USD)",
    "розмова",
)

_CSV_OUTCOME: Final[dict[str, str]] = {
    "classified": "класифіковано",
    "clarification": "уточнення",
    "error": "помилка",
    "pending": "виконується",
}


def _iso(value: Any) -> str:
    return value.isoformat(timespec="seconds") if isinstance(value, datetime) else str(value or "")


async def _csv_stream(where: str, params: list[Any]) -> AsyncIterator[bytes]:
    """The export, one classification per row, written as the database feeds it.

    A `StreamingResponse` wrapped around `await pool.fetch()` would be theatre: the whole
    result set would already be in memory before the first byte left. So the rows come off a
    SERVER-SIDE cursor inside a transaction, and only the codes of the classification
    currently being written are held.

    That is also why the codes are JOINed rather than aggregated: the join streams, and with
    the rows ordered `(created_at DESC, id DESC, position)` every classification's codes
    arrive contiguously, so they can be joined into one cell as they pass. One row per
    classification, never one row per code — a multi-code answer keeps all its codes in the
    "коди" cell, so a row count in Excel is a classification count.

    The leading U+FEFF is not decoration. Excel on Windows reads a CSV without a BOM in the
    system codepage, and every Ukrainian character in the file becomes mojibake.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)  # RFC 4180 CRLF line endings, which is what text/csv means

    def drain() -> bytes:
        chunk = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        return chunk.encode("utf-8")

    yield "﻿".encode()
    writer.writerow(_CSV_HEADER)
    yield drain()

    sql = f"""
        SELECT c.id, c.created_at, c.outcome, c.input_text, c.clarification_question,
               c.error_class, c.duration_ms, c.model, c.cost_usd, c.thread_id,
               cc.code, cc.full_path
        FROM classification c
        LEFT JOIN classification_code cc ON cc.classification_id = c.id
        WHERE {where}
        ORDER BY c.created_at DESC, c.id DESC, cc.position
    """

    current: Any = None
    codes: list[str] = []
    paths: list[str] = []

    async with acquire() as conn, conn.transaction():
        async for row in conn.cursor(sql, *params):
            if current is not None and row["id"] != current["id"]:
                writer.writerow(_csv_row(current, codes, paths))
                yield drain()
                codes, paths = [], []
            current = row
            if row["code"] is not None:
                codes.append(row["code"])
                paths.append(row["full_path"])

        if current is not None:  # the last group has no successor to flush it
            writer.writerow(_csv_row(current, codes, paths))
            yield drain()


def _csv_row(row: Any, codes: list[str], paths: list[str]) -> list[str]:
    outcome = _OUTCOME.get(row["outcome"], "classified")
    return [
        row["id"],
        _iso(row["created_at"]),
        _CSV_OUTCOME[outcome],
        row["input_text"],
        " ".join(codes),
        " | ".join(paths),
        row["clarification_question"] or "",
        row["error_class"] or "",
        "" if row["duration_ms"] is None else str(row["duration_ms"]),
        row["model"] or "",
        "" if row["cost_usd"] is None else f"{float(row['cost_usd']):.6f}",
        row["thread_id"] or "",
    ]
