"""The record writer — three calls, one row, and a turn that cannot disappear.

THE WHOLE POINT, in one sentence: `begin_turn()` INSERTs the row with `outcome = 'pending'`
BEFORE the model runs. v1 wrote a record only when a turn succeeded, so a turn that crashed,
timed out or was cancelled left nothing at all behind, and its 1,403 classifications produced
ZERO input/output pairs to learn from. Here the row exists first and is *closed* afterwards:

    id = await begin_turn(ctx, input_text=…, thread_id=…, model=…, …)   # outcome='pending'
    …the model runs…
    await finish_turn(id, outcome=agent_ctx.outcome, codes=…, …)        # or:
    await fail_turn(id, error_class=classify_error(exc), …)

A turn that dies between those calls stays `pending` with a NULL `finished_at`, which is a
row you can find, not an absence you cannot.

**Three outcome vocabularies, and the lossless one is stored.**

| agent (`app.agent.Outcome`) | stored (`classification.outcome`) | API (`web/src/lib/history.ts`) |
|---|---|---|
| `result`                    | `classified`                      | `classified`                   |
| `clarification`             | `clarification`                   | `clarification`                |
| `conversation`              | `conversation`                    | `classified` (collapsed)       |
| `error`                     | `error`                           | `error`                        |
| —                           | `pending` (set by `begin_turn`)   | `pending`                      |

`conversation` — the agent answered in prose without reaching a terminal tool, 29 of v1's
1,403 turns — is stored as itself and collapsed to the contract's four-value vocabulary at the
API boundary (`app/records/routes.py` owns that collapse). Collapsing it here would delete the
only evidence of how often the agent chats instead of classifying.

**A write here must never break the user's stream.** Every public call is wrapped and returns
normally on failure. That is emphatically NOT v1's fail-open, though: v1 had 15 bare
`except:` blocks that returned empty state and left no trace, so an outage looked exactly like
a user with no history. Each handler here logs at ERROR, with the classification id, through
`logger.exception` — the turn survives, and the failure is in the log with the id needed to
find the half-written row. (`asyncio.CancelledError` is a `BaseException` in 3.13 and so is
deliberately NOT caught: a cancelled task must stay cancelled.)

**Codes come from the ledger, never from the model.** `codes_from_ledger()` resolves every
emitted code through `TurnLedger.evidence_for()` and records the DATABASE's description and
full_path, denormalised onto the row. A code with no evidence this turn is dropped and logged;
it should be unreachable, because `emit_classification` already refuses it (E4), and this is
the second wall behind that one.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import chain
from typing import Any, Final

from app.agent.provenance import ToolCallRecord, TurnLedger
from app.context import RequestContext
from app.db import acquire, get_pool
from app.records.pricing import price_usd

logger = logging.getLogger("uktzed.records")

__all__ = [
    "DB_OUTCOME",
    "RecordedCode",
    "begin_turn",
    "codes_from_ledger",
    "fail_turn",
    "finish_turn",
    "new_classification_id",
]

DB_OUTCOME: Final[dict[str, str]] = {
    "result": "classified",
    "clarification": "clarification",
    "conversation": "conversation",
    "error": "error",
}
"""Agent vocabulary → stored vocabulary. See the module docstring for the full table."""


@dataclass(frozen=True, slots=True)
class RecordedCode:
    """One code as it will be stored: the database's text, not the model's.

    `is_primary=False` is a considered-and-rejected alternative; the SPA labels those
    «альтернатива» and renders them under the same list.
    """

    code: str
    description: str
    full_path: str
    is_primary: bool = True


def new_classification_id() -> str:
    """`cls_` + 24 URL-safe characters (18 bytes, 144 bits).

    Not a sequence: this id is the `/history/:id` path segment, and a BIGSERIAL would make
    one user's history an enumerable neighbourhood of every other user's.
    """
    return f"cls_{secrets.token_urlsafe(18)}"


def codes_from_ledger(
    ledger: TurnLedger, codes: Sequence[str], *, alternatives: Sequence[str] = ()
) -> list[RecordedCode]:
    """Resolve emitted codes against THIS turn's provenance ledger.

    The description and full_path stored on the record are the ones the tools actually showed
    the model this turn — never `ClassifiedCode.full_description`, which is the model's belief
    and is checked-then-discarded by `emit_classification` (E7). Duplicates collapse (a code
    listed as both an answer and an alternative stays an answer, once), and a code the ledger
    never saw is dropped with an ERROR rather than stored with invented text.
    """
    resolved: list[RecordedCode] = []
    seen: set[str] = set()
    for code, is_primary in chain(
        ((code, True) for code in codes), ((code, False) for code in alternatives)
    ):
        if code in seen:
            continue
        evidence = ledger.evidence_for(code)
        if evidence is None:
            logger.error(
                "code %r was emitted with no ledger evidence this turn; not recorded", code
            )
            continue
        seen.add(code)
        resolved.append(
            RecordedCode(
                code=evidence.code,
                description=evidence.description,
                full_path=evidence.full_path,
                is_primary=is_primary,
            )
        )
    return resolved


_INSERT_PENDING: Final = """
INSERT INTO classification (
    id, user_id, thread_id, input_text, outcome,
    model, prompt_version, prompt_sha256, dataset_sha256
)
VALUES ($1, $2, $3, $4, 'pending', $5, $6, $7, $8)
"""

_SELECT_MODEL: Final = "SELECT model FROM classification WHERE id = $1"

_CLOSE_TURN: Final = """
UPDATE classification
   SET outcome                = $2,
       error_class            = $3,
       clarification_question = $4,
       finished_at            = now(),
       turns                  = $5,
       repairs                = $6,
       tokens_in              = $7,
       tokens_cached          = $8,
       tokens_out             = $9,
       cost_usd               = $10,
       ttfb_ms                = $11,
       duration_ms            = $12
 WHERE id = $1
"""

_INSERT_CODE: Final = """
INSERT INTO classification_code
    (classification_id, position, code, description, full_path, is_primary)
VALUES ($1, $2, $3, $4, $5, $6)
"""

_INSERT_TOOL_CALL: Final = """
INSERT INTO classification_tool_call
    (classification_id, position, name, arguments, summary, duration_ms, ok, error)
VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8)
"""


async def begin_turn(
    ctx: RequestContext,
    *,
    input_text: str,
    thread_id: str | None,
    model: str,
    prompt_version: str,
    prompt_sha256: str,
    dataset_sha256: str | None,
) -> str:
    """Open the record, `pending`, before the model is called. Returns the classification id.

    The id is returned even when the INSERT fails, so the caller has nothing to branch on and
    the stream is never shaped by whether the audit trail worked. The follow-up
    `finish_turn`/`fail_turn` then logs its own ERROR against that same id, which is what ties
    the two halves of the failure together in the log.
    """
    classification_id = new_classification_id()
    try:
        await get_pool().execute(
            _INSERT_PENDING,
            classification_id,
            ctx.user_id,
            thread_id,
            input_text,
            model,
            prompt_version,
            prompt_sha256,
            dataset_sha256,
        )
    except Exception:
        logger.exception(
            "record begin_turn failed: classification=%s user=%s thread=%s",
            classification_id,
            ctx.user_id,
            thread_id,
        )
    return classification_id


async def finish_turn(
    classification_id: str,
    *,
    outcome: str,
    codes: Sequence[RecordedCode],
    clarification_question: str | None,
    tool_calls: Sequence[ToolCallRecord],
    turns: int | None,
    repairs: int,
    tokens_in: int | None,
    tokens_cached: int | None,
    tokens_out: int | None,
    ttfb_ms: int | None,
    duration_ms: int | None,
) -> None:
    """Close a turn that produced an answer, a question, or prose.

    `outcome` is the AGENT's vocabulary (`app.agent.Outcome`) and is mapped to the stored one
    here. The parent row and both child tables are written in ONE transaction: a record with
    `outcome = 'classified'` and no codes would be a lie, and half a tool trace is worse than
    none for reading back what the model did.
    """
    stored = DB_OUTCOME.get(outcome)
    if stored is None:
        # An outcome outside the agent's own Literal is a bug upstream; storing it raw would
        # trip the CHECK and cost the entire row, so record the turn as a failure and say so.
        logger.error(
            "unknown agent outcome %r for classification %s; recorded as 'error'",
            outcome,
            classification_id,
        )
        stored = "error"
    await _close_turn(
        classification_id,
        outcome=stored,
        error_class=None,
        clarification_question=clarification_question,
        codes=codes,
        tool_calls=tool_calls,
        turns=turns,
        repairs=repairs,
        tokens_in=tokens_in,
        tokens_cached=tokens_cached,
        tokens_out=tokens_out,
        ttfb_ms=ttfb_ms,
        duration_ms=duration_ms,
    )


async def fail_turn(
    classification_id: str,
    *,
    error_class: str,
    tool_calls: Sequence[ToolCallRecord],
    turns: int | None,
    repairs: int,
    tokens_in: int | None,
    tokens_cached: int | None,
    tokens_out: int | None,
    ttfb_ms: int | None,
    duration_ms: int | None,
) -> None:
    """Close a turn that failed. The row, the tool trace and the token bill all survive.

    A failed turn still burned tokens and still walked the tariff, and `error_class` (the
    closed vocabulary from `app/chat/errors.py`) is what makes `GROUP BY error_class` over
    this table the failure report v1 could not produce. No codes: whatever the model was about
    to emit was never shown to anyone.
    """
    await _close_turn(
        classification_id,
        outcome="error",
        error_class=error_class,
        clarification_question=None,
        codes=(),
        tool_calls=tool_calls,
        turns=turns,
        repairs=repairs,
        tokens_in=tokens_in,
        tokens_cached=tokens_cached,
        tokens_out=tokens_out,
        ttfb_ms=ttfb_ms,
        duration_ms=duration_ms,
    )


async def _close_turn(
    classification_id: str,
    *,
    outcome: str,
    error_class: str | None,
    clarification_question: str | None,
    codes: Sequence[RecordedCode],
    tool_calls: Sequence[ToolCallRecord],
    turns: int | None,
    repairs: int,
    tokens_in: int | None,
    tokens_cached: int | None,
    tokens_out: int | None,
    ttfb_ms: int | None,
    duration_ms: int | None,
) -> None:
    """The one transaction both terminal calls share."""
    try:
        async with acquire() as conn, conn.transaction():
            # The model id is read back from the row rather than re-derived: pricing has to
            # use the model that ANSWERED, and settings could have been reloaded since.
            model = await conn.fetchval(_SELECT_MODEL, classification_id)
            if model is None:
                # begin_turn's INSERT failed, or something deleted the row mid-turn. Nothing
                # to close — and the child INSERTs would violate the foreign key.
                logger.error(
                    "no pending record %s to close (outcome=%s error_class=%s)",
                    classification_id,
                    outcome,
                    error_class,
                )
                return

            await conn.execute(
                _CLOSE_TURN,
                classification_id,
                outcome,
                error_class,
                clarification_question,
                turns,
                repairs or 0,
                tokens_in,
                tokens_cached,
                tokens_out,
                price_usd(model, tokens_in, tokens_cached, tokens_out),
                ttfb_ms,
                duration_ms,
            )

            for position, code in enumerate(codes):
                await conn.execute(
                    _INSERT_CODE,
                    classification_id,
                    position,
                    code.code,
                    code.description,
                    code.full_path,
                    code.is_primary,
                )

            for call in tool_calls:
                # position IS `ToolCallRecord.index`, not the loop counter: the ledger's own
                # numbering is what `CodeEvidence.tool_call_index` points at, so renumbering
                # here would silently break the link between a code and the call that found it.
                await conn.execute(
                    _INSERT_TOOL_CALL,
                    classification_id,
                    call.index,
                    call.tool_name,
                    _json_arguments(call.args),
                    call.result_digest,
                    call.duration_ms,
                    call.error is None,
                    call.error,
                )
    except Exception:
        logger.exception(
            "record close failed: classification=%s outcome=%s codes=%s tools=%s",
            classification_id,
            outcome,
            len(codes),
            len(tool_calls),
        )


def _json_arguments(args: dict[str, Any] | None) -> str | None:
    """Tool arguments for the JSONB column.

    A string, because `app/db.py` installs no jsonb codec on purpose (asyncpg then hands JSONB
    back as `str`, which is what pydantic's `model_validate_json` wants). `default=str` keeps
    an unexpected value — a datetime, a pydantic model — from turning the whole record write
    into a failure; the audit trail is not worth losing over an unserialisable argument.
    """
    if not args:
        return None
    try:
        return json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        logger.exception("tool arguments are not JSON-serialisable; stored as NULL")
        return None
