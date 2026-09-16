"""The three history endpoints, and the contract the SPA already ships against.

`web/src/pages/HistoryPage.tsx` and `HistoryDetailPage.tsx` were written before this API
existed, so the field names in `web/src/lib/history.ts` are not documentation — they are the
wire format, snake_case included, with no mapping layer to absorb a rename. The last test in
this file reads that TypeScript file and compares its key sets against the JSON these routes
actually return. It is the one that stops the frontend breaking silently.

Two techniques are borrowed from the existing suite:

* `tests/test_store_isolation.py`'s FakePool — an asyncpg-shaped double over real SQLite that
  EXECUTES the route's real SQL. A dict double would only ever test the double and would keep
  passing after someone deleted `AND user_id = $2`; here the predicates are evaluated by an
  actual SQL engine, and the structural guard in `_run` fails any statement that touches a
  `classification*` table without mentioning `user_id` at all.
* `tests/test_http_contract.py`'s TestClient-over-a-stub-pool. No Postgres and no network.

The app under test is assembled here rather than imported from `app.main`: `main` mounts the
SPA at "/" last, and Starlette matches in registration order, so a router included after the
import would be shadowed by that mount. `SessionMiddleware` is present because `current_user`
reads `request.session` — without it an anonymous request is a 500, not the 401 it must be.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import app.db as db
from app.auth.deps import CurrentUser, current_user
from app.records.routes import _CSV_HEADER, _decode_cursor, _encode_cursor, router

ROOT = Path(__file__).resolve().parents[1]
HISTORY_TS = ROOT / "web" / "src" / "lib" / "history.ts"

ALICE_ID = 1
BOB_ID = 2

# Mirrors migration 0004_records, with PG types swapped for SQLite ones. `id` is TEXT because
# it is a `secrets.token_urlsafe` value, never a sequence — the detail route is reachable by
# id from an authenticated session, and a BIGSERIAL there is an enumerable register.
_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE app_user (
    id       INTEGER PRIMARY KEY,
    username TEXT    NOT NULL UNIQUE
);

CREATE TABLE classification (
    id                     TEXT    PRIMARY KEY,
    user_id                INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    thread_id              TEXT,
    created_at             TEXT    NOT NULL,
    finished_at            TEXT,
    input_text             TEXT    NOT NULL,
    outcome                TEXT    NOT NULL,
    clarification_question TEXT,
    error_class            TEXT,
    duration_ms            INTEGER,
    ttfb_ms                INTEGER,
    model                  TEXT    NOT NULL,
    prompt_version         TEXT    NOT NULL,
    prompt_sha256          TEXT    NOT NULL,
    dataset_sha256         TEXT,
    turns                  INTEGER,
    repairs                INTEGER NOT NULL DEFAULT 0,
    tokens_in              INTEGER,
    tokens_cached          INTEGER,
    tokens_out             INTEGER,
    cost_usd               NUMERIC
);

CREATE TABLE classification_code (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    classification_id TEXT    NOT NULL REFERENCES classification(id) ON DELETE CASCADE,
    position          INTEGER NOT NULL,
    code              TEXT    NOT NULL,
    description       TEXT    NOT NULL,
    full_path         TEXT    NOT NULL,
    is_primary        INTEGER NOT NULL DEFAULT 0,
    UNIQUE (classification_id, position)
);

CREATE TABLE classification_tool_call (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    classification_id TEXT    NOT NULL REFERENCES classification(id) ON DELETE CASCADE,
    position          INTEGER NOT NULL,
    name              TEXT    NOT NULL,
    arguments         TEXT,
    summary           TEXT,
    duration_ms       INTEGER,
    ok                INTEGER NOT NULL DEFAULT 1,
    error             TEXT,
    UNIQUE (classification_id, position)
);

INSERT INTO app_user (id, username) VALUES (1, 'alice'), (2, 'bob');
"""

_PARAM = re.compile(r"\$(\d+)")
_CAST = re.compile(r"::\w+")


def _translate(sql: str, args: tuple[Any, ...]) -> tuple[str, list[Any]]:
    """asyncpg dialect → sqlite3 dialect.

    `$n` may repeat (the search clause uses the same pattern for the text and for the code),
    so parameters are collected in the order the placeholders appear rather than by index.
    ILIKE becomes LIKE, which SQLite folds for ASCII only — Cyrillic case folding is
    Postgres's job and is not asserted here.
    """
    collected: list[Any] = []

    def _sub(match: re.Match[str]) -> str:
        value = args[int(match.group(1)) - 1]
        # sqlite3 has no datetime adapter since 3.12; the column is ISO TEXT either way, and
        # a fixed-format ISO string orders exactly like a timestamptz.
        collected.append(value.isoformat() if isinstance(value, datetime) else value)
        return "?"

    return _CAST.sub("", _PARAM.sub(_sub, sql)).replace("ILIKE", "LIKE"), collected


def _row(row: sqlite3.Row) -> dict[str, Any]:
    """asyncpg hands back typed values; SQLite hands back the TEXT it stored."""
    out = dict(row)
    for key, value in out.items():
        if key.endswith("_at") and isinstance(value, str):
            out[key] = datetime.fromisoformat(value)
    return out


class FakePool:
    """The asyncpg surface the history routes use, over an in-memory SQLite database."""

    def __init__(self) -> None:
        # check_same_thread=False: TestClient runs the ASGI app on a worker thread while the
        # test seeds from the main one. Access is still strictly sequential — the portal
        # blocks the test thread for the duration of each request.
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def _run(self, sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        # The structural invariant, enforced on every statement the routes issue: anything
        # touching a classification table MUST constrain user_id. This is what stops an
        # unscoped query being added later under a test that does not happen to cover it.
        if "classification" in sql and "user_id" not in sql:
            raise AssertionError(f"unscoped query — no user_id predicate:\n{sql}")
        self.statements.append((sql, args))
        translated, params = _translate(sql, args)
        return [_row(row) for row in self._conn.execute(translated, params).fetchall()]

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return self._run(sql, args)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        rows = self._run(sql, args)
        return rows[0] if rows else None

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self)

    # -- seeding -----------------------------------------------------------------------
    # Straight to the connection: the INSERT side belongs to the recorder, not to these
    # routes, so the tests only need the rows to exist.

    def seed(
        self,
        entry_id: str,
        *,
        user_id: int = ALICE_ID,
        created_at: datetime | None = None,
        minute: int = 0,
        input_text: str = "плівка ПВХ 0.2 мм",
        outcome: str = "classified",
        codes: tuple[tuple[str, str, str, bool], ...] = (),
        tool_calls: tuple[tuple[str, str | None, str | None, int, bool, str | None], ...] = (),
        **columns: Any,
    ) -> None:
        row: dict[str, Any] = {
            "id": entry_id,
            "user_id": user_id,
            "created_at": (created_at or datetime(2026, 9, 16, 12, minute, tzinfo=UTC)).isoformat(),
            "input_text": input_text,
            "outcome": outcome,
            "model": "gpt-5.6-terra",
            "prompt_version": "2026-09-16.1",
            "prompt_sha256": "0" * 64,
            **columns,
        }
        self._conn.execute(
            f"INSERT INTO classification ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
            list(row.values()),
        )
        for position, (code, description, full_path, is_primary) in enumerate(codes):
            self._conn.execute(
                "INSERT INTO classification_code"
                " (classification_id, position, code, description, full_path, is_primary)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (entry_id, position, code, description, full_path, int(is_primary)),
            )
        for position, (name, arguments, summary, duration_ms, ok, error) in enumerate(tool_calls):
            self._conn.execute(
                "INSERT INTO classification_tool_call"
                " (classification_id, position, name, arguments, summary, duration_ms, ok, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (entry_id, position, name, arguments, summary, duration_ms, int(ok), error),
            )
        self._conn.commit()


class _FakeAcquire:
    """`pool.acquire()` — an async context manager, as asyncpg's PoolAcquireContext is."""

    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._pool)

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeConn:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    def cursor(self, sql: str, *args: Any) -> _FakeCursor:
        """A server-side cursor is streamed by asyncpg; this double reads eagerly and yields
        one row at a time. What is under test is the code path — one row per classification,
        codes folded in as they pass — not the memory profile of the driver."""
        return _FakeCursor(self._pool._run(sql, args))


class _FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        for row in self._rows:
            yield row


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> FakePool:
    fake = FakePool()
    # The routes reach the pool through app.db, exactly as app/auth/deps.py does.
    monkeypatch.setattr(db, "_pool", fake)
    return fake


@pytest.fixture
def client(pool: FakePool) -> Iterator[TestClient]:
    application = FastAPI()
    application.add_middleware(SessionMiddleware, secret_key="history-tests")
    application.include_router(router)
    with TestClient(application, base_url="http://testserver") as test_client:
        yield test_client


def _as(client: TestClient, user_id: int) -> None:
    """Become a user without paying argon2 for a login on every test.

    The 401 test below deliberately does NOT call this, so the real dependency still runs at
    least once and the routes are proved to be behind it.
    """
    app = cast(FastAPI, client.app)
    app.dependency_overrides[current_user] = lambda: CurrentUser(
        id=user_id, username=f"user{user_id}", display_name=f"User {user_id}"
    )


CODE_FILM = ("3919101200", "інші", "Розділ VII > 39 > 3919 > плівки самоклейні", True)
CODE_ALT = ("3920101100", "інші", "Розділ VII > 39 > 3920 > плити, листи", False)
CODE_KETTLE = ("8516710000", "чайники", "Розділ XVI > 85 > 8516 > електрочайники", True)


# --------------------------------------------------------------------------------------
# the boundary
# --------------------------------------------------------------------------------------


def test_every_route_is_behind_the_session(client: TestClient) -> None:
    """No override, no cookie: all three routes answer 401 before touching the database."""
    assert client.get("/api/history").status_code == 401
    assert client.get("/api/history/anything").status_code == 401
    assert client.get("/api/history/export.csv").status_code == 401


def test_user_b_cannot_read_user_a(client: TestClient, pool: FakePool) -> None:
    pool.seed("cls_alice", user_id=ALICE_ID, codes=(CODE_FILM,))
    pool.seed("cls_bob", user_id=BOB_ID, minute=5, input_text="чайник електричний")

    _as(client, BOB_ID)
    response = client.get("/api/history/cls_alice")

    # 404, NEVER 403: a 403 confirms the id exists, and web/src/lib/api.ts additionally
    # treats 403 as a dead session and would log Bob out for following Alice's link.
    assert response.status_code == 404
    assert response.json()["detail"]

    assert [item["id"] for item in client.get("/api/history").json()["items"]] == ["cls_bob"]
    body = client.get("/api/history/export.csv").content.decode("utf-8-sig")
    assert "cls_alice" not in body
    assert "cls_bob" in body

    # …and the mirror image, so the 404 is the predicate and not a broken query.
    _as(client, ALICE_ID)
    assert client.get("/api/history/cls_alice").status_code == 200


def test_search_cannot_reach_across_users(client: TestClient, pool: FakePool) -> None:
    pool.seed("cls_alice", user_id=ALICE_ID, codes=(CODE_FILM,))
    _as(client, BOB_ID)

    assert client.get("/api/history", params={"q": "3919"}).json()["items"] == []
    assert client.get("/api/history", params={"q": "плівка"}).json()["items"] == []


def test_every_statement_is_scoped_to_the_user(client: TestClient, pool: FakePool) -> None:
    """The FakePool raises on any unscoped statement; this pins that all three routes were
    actually exercised against that guard, including the two child queries of the detail
    view, which repeat the predicate instead of trusting the id they were given."""
    pool.seed("cls_alice", codes=(CODE_FILM,), tool_calls=(("search", "{}", None, 12, True, None),))
    _as(client, ALICE_ID)

    client.get("/api/history", params={"q": "плівка"})
    client.get("/api/history/cls_alice")
    client.get("/api/history/export.csv")

    touching = [sql for sql, _ in pool.statements if "classification" in sql]
    assert len(touching) >= 5
    assert all("user_id" in sql for sql in touching)


# --------------------------------------------------------------------------------------
# keyset pagination
# --------------------------------------------------------------------------------------


def test_the_keyset_cursor_round_trips(client: TestClient, pool: FakePool) -> None:
    for minute in range(5):
        pool.seed(f"cls_{minute}", minute=minute, input_text=f"товар {minute}")
    _as(client, ALICE_ID)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(3):
        params = {"limit": 2} | ({"before": cursor} if cursor else {})
        page = client.get("/api/history", params=params).json()
        seen += [item["id"] for item in page["items"]]
        cursor = page["next_before"]

    # Newest first, every row exactly once, and the SPA's "Показати ще" is gone on the last
    # page because next_before is null rather than a cursor onto an empty page.
    assert seen == ["cls_4", "cls_3", "cls_2", "cls_1", "cls_0"]
    assert cursor is None


def test_the_cursor_carries_both_halves_of_the_sort_key(client: TestClient, pool: FakePool) -> None:
    for minute in range(3):
        pool.seed(f"cls_{minute}", minute=minute)
    _as(client, ALICE_ID)

    page = client.get("/api/history", params={"limit": 1}).json()
    last = page["items"][-1]

    created_at, ident = _decode_cursor(page["next_before"])
    assert ident == last["id"]
    assert created_at == datetime.fromisoformat(last["created_at"])
    assert _encode_cursor(created_at, ident) == page["next_before"]


def test_rows_sharing_a_timestamp_still_paginate(client: TestClient, pool: FakePool) -> None:
    """The reason the cursor is not a bare timestamp: a tie at the page boundary would
    otherwise either repeat the row or drop it."""
    same = datetime(2026, 9, 16, 12, 30, tzinfo=UTC)
    pool.seed("cls_a", created_at=same)
    pool.seed("cls_b", created_at=same)
    pool.seed("cls_c", created_at=datetime(2026, 9, 16, 12, 0, tzinfo=UTC))
    _as(client, ALICE_ID)

    first = client.get("/api/history", params={"limit": 1}).json()
    second = client.get("/api/history", params={"limit": 1, "before": first["next_before"]}).json()
    third = client.get("/api/history", params={"limit": 1, "before": second["next_before"]}).json()

    assert [first["items"][0]["id"], second["items"][0]["id"], third["items"][0]["id"]] == [
        "cls_b",
        "cls_a",
        "cls_c",
    ]
    assert third["next_before"] is None


def test_limit_is_clamped_not_rejected(client: TestClient, pool: FakePool) -> None:
    for minute in range(3):
        pool.seed(f"cls_{minute}", minute=minute)
    _as(client, ALICE_ID)

    # A hand-edited URL gets a page, not a 422 the SPA renders as a red bar. The LIMIT the
    # database sees is the clamp + 1, the extra row being how next_before is decided.
    assert client.get("/api/history", params={"limit": 5000}).status_code == 200
    assert pool.statements[-1][1][-1] == 101

    assert client.get("/api/history", params={"limit": 0}).status_code == 200
    assert pool.statements[-1][1][-1] == 2

    assert client.get("/api/history", params={"limit": -3}).status_code == 200
    assert pool.statements[-1][1][-1] == 2

    default = client.get("/api/history")
    assert default.status_code == 200
    assert pool.statements[-1][1][-1] == 21
    assert len(default.json()["items"]) == 3


def test_a_corrupt_cursor_is_a_400(client: TestClient, pool: FakePool) -> None:
    _as(client, ALICE_ID)
    # Valid base64, valid UTF-8, no id half — and plain junk.
    assert client.get("/api/history", params={"before": "bm9wZQ"}).status_code == 400
    assert client.get("/api/history", params={"before": "!!!!"}).status_code == 400


# --------------------------------------------------------------------------------------
# search, codes and the outcome vocabulary
# --------------------------------------------------------------------------------------


def test_search_matches_the_text_and_the_codes(client: TestClient, pool: FakePool) -> None:
    pool.seed("cls_film", minute=1, input_text="плівка ПВХ 0.2 мм", codes=(CODE_FILM,))
    pool.seed("cls_kettle", minute=2, input_text="чайник електричний", codes=(CODE_KETTLE,))
    _as(client, ALICE_ID)

    def ids(q: str) -> list[str]:
        return [item["id"] for item in client.get("/api/history", params={"q": q}).json()["items"]]

    assert ids("плівка") == ["cls_film"]  # the description the user typed
    assert ids("3919") == ["cls_film"]  # the code they got back
    assert ids("8516") == ["cls_kettle"]
    assert ids("") == ["cls_kettle", "cls_film"]
    assert ids("нічого") == []

    # LIKE metacharacters are escaped, so a search for "%" is a search for "%".
    assert ids("%") == []


def test_list_rows_carry_their_codes_in_order(client: TestClient, pool: FakePool) -> None:
    pool.seed("cls_film", codes=(CODE_FILM, CODE_ALT))
    pool.seed("cls_pending", minute=1, outcome="pending", input_text="ще рахується")
    _as(client, ALICE_ID)

    items = client.get("/api/history").json()["items"]
    pending, film = items

    # The SPA renders CodeList inline on the list page, so the codes travel with the row.
    assert [code["code"] for code in film["codes"]] == ["3919101200", "3920101100"]
    assert [code["is_primary"] for code in film["codes"]] == [True, False]
    assert film["codes"][0]["full_path"].endswith("плівки самоклейні")
    # A row inserted before the model ran is a legitimate row with no codes at all.
    assert pending["codes"] == []
    assert pending["outcome"] == "pending"


def test_the_outcome_vocabulary_is_collapsed(client: TestClient, pool: FakePool) -> None:
    """The column carries the agent's vocabulary too. "conversation" — a turn that finished
    and emitted no codes — is not a value `OUTCOME_LABEL` knows, so it collapses here rather
    than rendering as `undefined` in the badge."""
    pool.seed("cls_chat", minute=3, outcome="conversation", input_text="а що таке УКТЗЕД?")
    pool.seed("cls_ask", minute=2, outcome="clarification", clarification_question="Який матеріал?")
    pool.seed("cls_err", minute=1, outcome="error", error_class="model_timeout")
    pool.seed("cls_wait", minute=0, outcome="pending")
    _as(client, ALICE_ID)

    items = client.get("/api/history").json()["items"]
    assert [item["outcome"] for item in items] == [
        "classified",
        "clarification",
        "error",
        "pending",
    ]
    assert items[1]["clarification_question"] == "Який матеріал?"
    assert items[2]["error_class"] == "model_timeout"


# --------------------------------------------------------------------------------------
# the detail view
# --------------------------------------------------------------------------------------


def test_detail_carries_the_run_parameters_and_the_tool_calls(
    client: TestClient, pool: FakePool
) -> None:
    pool.seed(
        "cls_film",
        thread_id="thr_abc",
        duration_ms=8_400,
        dataset_sha256="5a113fc09ac05028afbd6ed1884a95133b65e7827e61a87d74d5ffb11a38dee3",
        tokens_in=12_000,
        tokens_cached=8_000,
        tokens_out=640,
        cost_usd=0.012345,
        codes=(CODE_FILM,),
        tool_calls=(
            ("search_candidates", '{"query": "плівка"}', "12 кандидатів", 120, True, None),
            ("open_node", '{"code": "3919"}', None, 30, False, "not_found"),
        ),
    )
    _as(client, ALICE_ID)

    detail = client.get("/api/history/cls_film").json()

    assert detail["model"] == "gpt-5.6-terra"
    assert detail["prompt_version"] == "2026-09-16.1"
    assert detail["dataset_sha256"].startswith("5a113fc09ac0")
    assert (detail["tokens_in"], detail["tokens_out"]) == (12_000, 640)
    assert detail["cost_usd"] == pytest.approx(0.012345)
    assert detail["thread_id"] == "thr_abc"
    assert [code["code"] for code in detail["codes"]] == ["3919101200"]

    # Ordered by position, and `arguments` is a JSON object rather than the string asyncpg
    # hands back for a JSONB column (no codec is installed; see app/db.py).
    assert [call["name"] for call in detail["tool_calls"]] == ["search_candidates", "open_node"]
    assert detail["tool_calls"][0]["arguments"] == {"query": "плівка"}
    assert detail["tool_calls"][0]["ok"] is True
    assert detail["tool_calls"][1]["ok"] is False
    assert detail["tool_calls"][1]["error"] == "not_found"


def test_detail_of_a_missing_row_is_a_404(client: TestClient, pool: FakePool) -> None:
    _as(client, ALICE_ID)
    assert client.get("/api/history/cls_nothing").status_code == 404


# --------------------------------------------------------------------------------------
# the export
# --------------------------------------------------------------------------------------


def test_csv_has_a_bom_a_header_and_one_row_per_classification(
    client: TestClient, pool: FakePool
) -> None:
    pool.seed("cls_film", minute=1, codes=(CODE_FILM, CODE_ALT), duration_ms=8_400, cost_usd=0.01)
    pool.seed("cls_err", minute=0, outcome="error", error_class="model_timeout", input_text="?")
    _as(client, ALICE_ID)

    response = client.get("/api/history/export.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert ".csv" in response.headers["content-disposition"]

    # Without U+FEFF, Excel on Windows reads the file in the system codepage and every
    # Ukrainian character in it is mojibake.
    assert response.content.startswith(b"\xef\xbb\xbf")

    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0] == list(_CSV_HEADER)
    assert len(rows) == 3  # header + one row per classification, NOT one row per code

    film = dict(zip(_CSV_HEADER, rows[1], strict=True))
    assert film["ідентифікатор"] == "cls_film"
    assert film["коди"] == "3919101200 3920101100"  # joined, so the row count stays honest
    assert "плівки самоклейні" in film["повний шлях"]
    assert film["статус"] == "класифіковано"

    failure = dict(zip(_CSV_HEADER, rows[2], strict=True))
    assert failure["статус"] == "помилка"
    assert failure["помилка"] == "model_timeout"
    assert failure["коди"] == ""


def test_csv_honours_the_search_and_is_not_shadowed_by_the_id_route(
    client: TestClient, pool: FakePool
) -> None:
    """`/history/{id}` would happily match the literal segment "export.csv" if it were
    registered first; Starlette matches in registration order."""
    pool.seed("cls_film", minute=1, input_text="плівка ПВХ", codes=(CODE_FILM,))
    pool.seed("cls_kettle", minute=0, input_text="чайник електричний", codes=(CODE_KETTLE,))
    _as(client, ALICE_ID)

    response = client.get("/api/history/export.csv", params={"q": "чайник"})
    assert response.headers["content-type"].startswith("text/csv")

    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert [row[0] for row in rows[1:]] == ["cls_kettle"]


def test_csv_streams_rather_than_materialising(client: TestClient, pool: FakePool) -> None:
    """A StreamingResponse over `await pool.fetch()` would be theatre. The route holds a
    server-side cursor instead, so the response has no Content-Length and the first bytes
    (BOM + header) are on the wire before the last row is read."""
    for minute in range(3):
        pool.seed(f"cls_{minute}", minute=minute)
    _as(client, ALICE_ID)

    with client.stream("GET", "/api/history/export.csv") as response:
        assert "content-length" not in response.headers
        chunks = list(response.iter_bytes())

    assert chunks[0].startswith(b"\xef\xbb\xbf")
    assert len(b"".join(chunks).splitlines()) == 4


# --------------------------------------------------------------------------------------
# THE contract test
# --------------------------------------------------------------------------------------


def _ts_block(source: str, name: str) -> str:
    start = source.index(f"export type {name} = ")
    opened = source.index("{", start)
    depth = 0
    for index in range(opened, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opened : index + 1]
    raise AssertionError(f"unterminated type {name} in {HISTORY_TS}")


def _ts_fields(name: str) -> set[str]:
    """The declared keys of a type in web/src/lib/history.ts, comments stripped."""
    source = HISTORY_TS.read_text(encoding="utf-8")
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return set(re.findall(r"^\s*(\w+)\??:", _ts_block(source, name), re.MULTILINE))


def test_the_json_keys_are_exactly_what_the_spa_declares(
    client: TestClient, pool: FakePool
) -> None:
    """The test that stops the frontend breaking silently.

    There is no camelCase mapping layer between these routes and `web/src/lib/history.ts` —
    the TypeScript names ARE the wire format — so a rename on either side is invisible until
    a field renders as "—" in production. This reads the type file and compares.
    """
    pool.seed(
        "cls_film",
        thread_id="thr_abc",
        duration_ms=8_400,
        dataset_sha256="a" * 64,
        tokens_in=12_000,
        tokens_out=640,
        cost_usd=0.012345,
        codes=(CODE_FILM,),
        tool_calls=(
            ("search_candidates", '{"query": "плівка"}', "12 кандидатів", 120, True, None),
        ),
    )
    _as(client, ALICE_ID)

    page = client.get("/api/history").json()
    detail = client.get("/api/history/cls_film").json()

    assert set(page) == _ts_fields("HistoryPage")
    assert set(page["items"][0]) == _ts_fields("HistoryEntry")
    assert set(page["items"][0]["codes"][0]) == _ts_fields("HistoryCode")
    # HistoryDetail is an intersection: HistoryEntry & { … }.
    assert set(detail) == _ts_fields("HistoryEntry") | _ts_fields("HistoryDetail")
    assert set(detail["tool_calls"][0]) == _ts_fields("ToolCall")


def test_the_outcome_values_are_exactly_what_the_spa_declares(
    client: TestClient, pool: FakePool
) -> None:
    """`OUTCOME_LABEL` and `OUTCOME_STYLE` are Records keyed by this union; a value outside
    it renders an unstyled badge with `undefined` in it."""
    source = HISTORY_TS.read_text(encoding="utf-8")
    declared = set(
        re.findall(r'"([a-z]+)"', re.search(r"export type Outcome = ([^;]+);", source).group(1))
    )

    pool.seed("cls_ok", minute=3, outcome="classified")
    pool.seed("cls_chat", minute=2, outcome="conversation")
    pool.seed("cls_ask", minute=1, outcome="clarification")
    pool.seed("cls_err", minute=0, outcome="error")
    pool.seed("cls_wait", minute=4, outcome="pending")
    _as(client, ALICE_ID)

    returned = {item["outcome"] for item in client.get("/api/history").json()["items"]}
    assert returned == declared
