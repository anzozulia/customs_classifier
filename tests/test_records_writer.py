"""THE test for the record: a turn that crashes must still be a row.

v1 wrote a classification only when the turn succeeded. 1,403 classifications later it had
ZERO input/output pairs, no token counts, no latency, and no trace at all of the 29 turns
that failed — the failure this milestone exists to make impossible. So the assertions below
are about *when* rows exist as much as what is in them: pending before the model runs,
closed after, and still there when the turn dies.

It runs WITHOUT Postgres, reusing the technique from `tests/test_store_isolation.py`:
`FakePool` is an asyncpg-shaped double over real SQLite that EXECUTES the writer's real SQL
(`$n` → `?`, `::jsonb` and `now()` translated away), against a schema that mirrors migration
0004. A hand-rolled dict double would only ever test the double; here the UNIQUE constraints,
the foreign keys and the transaction are evaluated by an actual SQL engine, which is what
makes `test_close_turn_is_one_transaction` mean anything at all.

The pool is installed by monkeypatching `app.db._pool`, so `get_pool()` and `acquire()` —
including the real `async with … .transaction()` — are the ones under test.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from decimal import Decimal
from typing import Any, cast

import asyncpg
import pytest

import app.db as app_db
from app.agent.provenance import CodeEvidence, ToolCallRecord, TurnLedger
from app.context import RequestContext
from app.records.pricing import MODEL_PRICES, price_usd
from app.records.writer import (
    RecordedCode,
    begin_turn,
    codes_from_ledger,
    fail_turn,
    finish_turn,
)

ALICE_ID = 1
MODEL = "gpt-5.6-terra"
PROMPT_VERSION = "2026-09-16.1"
PROMPT_SHA = "b" * 64
DATASET_SHA = "a" * 64
FULL_PATH = "Пластмаси та вироби з них / Плити, листи, плівки / самоклейні / інші"

# Mirrors migrations/versions/0004_records.py, with PG types swapped for SQLite ones.
# `cost_usd` is TEXT here and NUMERIC(10,6) in Postgres on purpose: SQLite's NUMERIC affinity
# would coerce a Decimal bound as text into a float, which is the precise failure the real
# column type exists to prevent — the mirror must not hide it.
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
    created_at             TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
    cost_usd               TEXT,
    CHECK (outcome IN ('pending', 'classified', 'clarification', 'error', 'conversation'))
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

INSERT INTO app_user (id, username) VALUES (1, 'alice');
"""

_PARAM = re.compile(r"\$(\d+)")


def _translate(sql: str, args: tuple[Any, ...]) -> tuple[str, list[Any]]:
    """asyncpg dialect → sqlite3 dialect. Decimals are bound as text, never as floats."""
    collected: list[Any] = []

    def _sub(match: re.Match[str]) -> str:
        value = args[int(match.group(1)) - 1]
        collected.append(str(value) if isinstance(value, Decimal) else value)
        return "?"

    sql = _PARAM.sub(_sub, sql)
    return sql.replace("::jsonb", "").replace("now()", "CURRENT_TIMESTAMP"), collected


class FakeDB:
    """One in-memory SQLite connection, in explicit-transaction mode.

    `isolation_level = None` turns off sqlite3's implicit transaction management so that the
    BEGIN / COMMIT / ROLLBACK issued by `FakeConnection.transaction()` are the real thing —
    without it a failed statement would leave the earlier writes of the same transaction
    committed, and the atomicity test would pass for the wrong reason.
    """

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.isolation_level = None
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

    def run(self, sql: str, args: tuple[Any, ...]) -> list[sqlite3.Row]:
        translated, params = _translate(sql, args)
        return self.conn.execute(translated, params).fetchall()


class _Transaction:
    def __init__(self, db: FakeDB) -> None:
        self._db = db

    async def __aenter__(self) -> _Transaction:
        self._db.conn.execute("BEGIN")
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self._db.conn.execute("ROLLBACK" if exc_type is not None else "COMMIT")
        return False


class _Acquire:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConnection:
        return self._conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeConnection:
    """The asyncpg Connection surface `_close_turn` uses."""

    def __init__(self, db: FakeDB) -> None:
        self._db = db

    def transaction(self) -> _Transaction:
        return _Transaction(self._db)

    async def execute(self, sql: str, *args: Any) -> str:
        self._db.run(sql, args)
        return "OK"

    async def fetchval(self, sql: str, *args: Any) -> Any:
        rows = self._db.run(sql, args)
        return rows[0][0] if rows else None


class FakePool:
    """The asyncpg Pool surface: the statement methods, plus `acquire()`."""

    def __init__(self) -> None:
        self.db = FakeDB()

    def acquire(self) -> _Acquire:
        return _Acquire(FakeConnection(self.db))

    async def execute(self, sql: str, *args: Any) -> str:
        self.db.run(sql, args)
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return self.db.run(sql, args)

    async def fetchrow(self, sql: str, *args: Any) -> sqlite3.Row | None:
        rows = self.db.run(sql, args)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        rows = self.db.run(sql, args)
        return rows[0][0] if rows else None


class DeadPool:
    """A pool whose every path fails — an outage, mid-turn."""

    async def execute(self, sql: str, *args: Any) -> str:
        raise ConnectionError("connection reset by peer")

    def acquire(self) -> Any:
        raise ConnectionError("pool is closing")


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> FakePool:
    fake = FakePool()
    # The writer reaches the database through app.db.get_pool()/acquire(), so the double goes
    # in at the module global those read — the real acquire() and transaction handling run.
    monkeypatch.setattr(app_db, "_pool", cast(asyncpg.Pool, fake))
    return fake


@pytest.fixture
def dead_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_db, "_pool", cast(asyncpg.Pool, DeadPool()))


@pytest.fixture
def alice() -> RequestContext:
    return RequestContext(user_id=ALICE_ID, request_id="req-alice")


def _ledger() -> TurnLedger:
    """A ledger shaped like a real turn: one navigation call that surfaced a code, then a
    rejected `emit_classification`, then the accepted one."""
    ledger = TurnLedger()

    walk = ledger.begin_call("open_category", {"category_code": "391910"})
    ledger.record_codes(
        [
            CodeEvidence(
                code="3919101200",
                description="інші",
                full_path=FULL_PATH,
                is_terminal=True,
                tool_call_index=walk.index,
            )
        ]
    )
    ledger.end_call(walk, digest="ok:12 codes")

    rejected = ledger.begin_call("emit_classification", {"codes": ["9999999999"]})
    ledger.end_call(rejected, error="provenance_violation")

    accepted = ledger.begin_call("emit_classification", {"codes": ["3919101200"]})
    ledger.end_call(accepted, digest="ok:1:mismatch=0")
    return ledger


async def _begin(alice: RequestContext, *, input_text: str = "плівка самоклейна") -> str:
    return await begin_turn(
        alice,
        input_text=input_text,
        thread_id="thr_alice",
        model=MODEL,
        prompt_version=PROMPT_VERSION,
        prompt_sha256=PROMPT_SHA,
        dataset_sha256=DATASET_SHA,
    )


async def _finish(classification_id: str, ledger: TurnLedger, **overrides: Any) -> None:
    kwargs: dict[str, Any] = {
        "outcome": "result",
        "codes": codes_from_ledger(ledger, ["3919101200"]),
        "clarification_question": None,
        "tool_calls": ledger.calls,
        "turns": 3,
        "repairs": 1,
        "tokens_in": 10_000,
        "tokens_cached": 4_000,
        "tokens_out": 2_000,
        "ttfb_ms": 120,
        "duration_ms": 8_400,
    }
    kwargs.update(overrides)
    await finish_turn(classification_id, **kwargs)


async def _row(pool: FakePool, classification_id: str) -> sqlite3.Row:
    row = await pool.fetchrow("SELECT * FROM classification WHERE id = $1", classification_id)
    assert row is not None, "the classification row is missing"
    return row


async def _codes(pool: FakePool, classification_id: str) -> list[sqlite3.Row]:
    return await pool.fetch(
        "SELECT * FROM classification_code WHERE classification_id = $1 ORDER BY position",
        classification_id,
    )


async def _tool_calls(pool: FakePool, classification_id: str) -> list[sqlite3.Row]:
    return await pool.fetch(
        "SELECT * FROM classification_tool_call WHERE classification_id = $1 ORDER BY position",
        classification_id,
    )


# --------------------------------------------------------------------------------------
# The row exists before the answer does
# --------------------------------------------------------------------------------------


async def test_begin_turn_writes_a_pending_row_before_the_model_runs(
    pool: FakePool, alice: RequestContext
) -> None:
    """The whole milestone in one assertion: the record is INSERTed first, not last."""
    classification_id = await _begin(alice)

    assert classification_id.startswith("cls_")
    assert len(classification_id) >= len("cls_") + 24

    row = await _row(pool, classification_id)
    assert row["outcome"] == "pending"
    assert row["finished_at"] is None
    assert row["created_at"] is not None
    assert row["user_id"] == ALICE_ID
    assert row["thread_id"] == "thr_alice"
    assert row["input_text"] == "плівка самоклейна"
    # Provenance of the ANSWER, captured before there is one.
    assert row["model"] == MODEL
    assert row["prompt_version"] == PROMPT_VERSION
    assert row["prompt_sha256"] == PROMPT_SHA
    assert row["dataset_sha256"] == DATASET_SHA
    assert row["repairs"] == 0
    # Nothing is known yet, and the schema says so rather than inventing zeroes.
    assert row["tokens_in"] is None and row["cost_usd"] is None and row["duration_ms"] is None


async def test_ids_are_not_enumerable(pool: FakePool, alice: RequestContext) -> None:
    """The id is a `/history/:id` path segment. A sequence would make one user's history an
    enumerable neighbourhood of every other user's."""
    ids = {await _begin(alice) for _ in range(50)}
    assert len(ids) == 50


# --------------------------------------------------------------------------------------
# …and is closed afterwards
# --------------------------------------------------------------------------------------


async def test_finish_turn_closes_the_row(pool: FakePool, alice: RequestContext) -> None:
    classification_id = await _begin(alice)
    await _finish(classification_id, _ledger())

    row = await _row(pool, classification_id)
    # `result` (agent) → `classified` (stored). The API collapse is the routes' job.
    assert row["outcome"] == "classified"
    assert row["finished_at"] is not None
    assert row["error_class"] is None
    assert row["turns"] == 3
    assert row["repairs"] == 1
    assert (row["tokens_in"], row["tokens_cached"], row["tokens_out"]) == (10_000, 4_000, 2_000)
    assert (row["ttfb_ms"], row["duration_ms"]) == (120, 8_400)
    # 6,000 fresh @ $2 + 4,000 cached @ $0.20 + 2,000 out @ $12, per 1M tokens.
    assert row["cost_usd"] == "0.036800"


async def test_finish_turn_writes_codes_and_tool_calls(
    pool: FakePool, alice: RequestContext
) -> None:
    ledger = _ledger()
    classification_id = await _begin(alice)
    await _finish(
        classification_id,
        ledger,
        codes=codes_from_ledger(ledger, ["3919101200"], alternatives=["3919101200"]),
    )

    codes = await _codes(pool, classification_id)
    # The alternative is the same code as the answer, so it collapses: one row, primary.
    assert [c["position"] for c in codes] == [0]
    assert codes[0]["code"] == "3919101200"
    assert codes[0]["is_primary"] == 1

    calls = await _tool_calls(pool, classification_id)
    # position IS ToolCallRecord.index — the ledger's numbering, which CodeEvidence points at.
    assert [c["position"] for c in calls] == [0, 1, 2]
    assert [c["name"] for c in calls] == [
        "open_category",
        "emit_classification",
        "emit_classification",
    ]
    assert [bool(c["ok"]) for c in calls] == [True, False, True]
    assert calls[1]["error"] == "provenance_violation"
    assert calls[1]["summary"] is None
    assert calls[0]["summary"] == "ok:12 codes"  # ToolCallRecord.result_digest
    assert all(c["duration_ms"] is not None for c in calls)
    # Arguments round-trip as JSON, with Ukrainian and code strings intact.
    assert json.loads(calls[0]["arguments"]) == {"category_code": "391910"}


async def test_codes_are_resolved_from_the_ledger_never_from_the_model(
    pool: FakePool, alice: RequestContext, caplog: pytest.LogCaptureFixture
) -> None:
    """D25: the description and full_path stored are the database's, and a code this turn
    never surfaced is dropped rather than stored with invented text."""
    ledger = _ledger()
    with caplog.at_level(logging.ERROR, logger="uktzed.records"):
        resolved = codes_from_ledger(ledger, ["3919101200", "8517130000"])

    assert [c.code for c in resolved] == ["3919101200"]
    assert resolved[0].description == "інші"
    assert resolved[0].full_path == FULL_PATH
    assert any("8517130000" in record.getMessage() for record in caplog.records)

    classification_id = await _begin(alice)
    await _finish(classification_id, ledger, codes=resolved)
    stored = await _codes(pool, classification_id)
    assert [c["full_path"] for c in stored] == [FULL_PATH]


async def test_alternatives_are_recorded_as_not_primary(
    pool: FakePool, alice: RequestContext
) -> None:
    ledger = _ledger()
    ledger.record_codes(
        [
            CodeEvidence(
                code="3919109000",
                description="інші",
                full_path=f"{FULL_PATH} (інші)",
                is_terminal=True,
                tool_call_index=0,
            )
        ]
    )
    classification_id = await _begin(alice)
    await _finish(
        classification_id,
        ledger,
        codes=codes_from_ledger(ledger, ["3919101200"], alternatives=["3919109000"]),
    )

    rows = await _codes(pool, classification_id)
    assert [(r["code"], r["position"], bool(r["is_primary"])) for r in rows] == [
        ("3919101200", 0, True),
        ("3919109000", 1, False),
    ]


async def test_clarification_is_its_own_outcome(pool: FakePool, alice: RequestContext) -> None:
    classification_id = await _begin(alice)
    await _finish(
        classification_id,
        _ledger(),
        outcome="clarification",
        codes=[],
        clarification_question="Яка основа: папір чи пластмаса?",
    )

    row = await _row(pool, classification_id)
    assert row["outcome"] == "clarification"
    assert row["clarification_question"] == "Яка основа: папір чи пластмаса?"
    assert await _codes(pool, classification_id) == []


async def test_conversation_is_stored_losslessly(pool: FakePool, alice: RequestContext) -> None:
    """The agent's third branch survives the write. The four-value API vocabulary has no
    `conversation`, but collapsing it HERE would delete the only evidence of how often the
    agent chats instead of classifying (29 of v1's 1,403 turns)."""
    classification_id = await _begin(alice)
    await _finish(classification_id, _ledger(), outcome="conversation", codes=[])

    assert (await _row(pool, classification_id))["outcome"] == "conversation"


async def test_an_outcome_outside_the_vocabulary_does_not_lose_the_row(
    pool: FakePool, alice: RequestContext, caplog: pytest.LogCaptureFixture
) -> None:
    """Storing an unmapped value raw would trip the CHECK and take the whole record with it."""
    classification_id = await _begin(alice)
    with caplog.at_level(logging.ERROR, logger="uktzed.records"):
        await _finish(classification_id, _ledger(), outcome="уточнення", codes=[])

    assert (await _row(pool, classification_id))["outcome"] == "error"
    assert any(classification_id in record.getMessage() for record in caplog.records)


# --------------------------------------------------------------------------------------
# A turn that crashes is still a row
# --------------------------------------------------------------------------------------


async def test_a_failed_turn_is_still_a_row(pool: FakePool, alice: RequestContext) -> None:
    """v1's defining hole: 29 failed turns, zero records. A failure still burned tokens and
    still walked the tariff, and all of it is kept."""
    ledger = _ledger()
    classification_id = await _begin(alice)
    await fail_turn(
        classification_id,
        error_class="upstream_5xx",
        tool_calls=ledger.calls,
        turns=2,
        repairs=1,
        tokens_in=5_000,
        tokens_cached=0,
        tokens_out=300,
        ttfb_ms=None,
        duration_ms=41_000,
    )

    row = await _row(pool, classification_id)
    assert row["outcome"] == "error"
    assert row["error_class"] == "upstream_5xx"
    assert row["finished_at"] is not None
    assert row["input_text"] == "плівка самоклейна"  # what was asked survives the failure
    assert (row["tokens_in"], row["tokens_out"]) == (5_000, 300)
    assert row["ttfb_ms"] is None  # nothing ever reached the screen
    assert row["duration_ms"] == 41_000
    assert row["cost_usd"] == "0.013600"  # 5,000 @ $2 + 300 @ $12 per 1M
    # The tool trace of a failed turn is kept in full; the codes are not — whatever the model
    # was about to emit was never shown to anyone.
    assert len(await _tool_calls(pool, classification_id)) == 3
    assert await _codes(pool, classification_id) == []


async def test_an_abandoned_turn_stays_pending(pool: FakePool, alice: RequestContext) -> None:
    """Neither terminal call ever ran — the process died. `outcome = 'pending' AND
    finished_at IS NULL` is the query that finds it."""
    await _begin(alice)

    stuck = await pool.fetch(
        "SELECT id FROM classification WHERE outcome = 'pending' AND finished_at IS NULL"
    )
    assert len(stuck) == 1


# --------------------------------------------------------------------------------------
# Persistence never breaks the stream — and never fails silently either
# --------------------------------------------------------------------------------------


async def test_a_dead_pool_never_breaks_the_turn(
    dead_pool: None, alice: RequestContext, caplog: pytest.LogCaptureFixture
) -> None:
    """The user's answer must not depend on the audit trail. But v1 had 15 fail-open blocks
    that impersonated empty state, so every one of these has to leave an ERROR behind that
    names the classification."""
    with caplog.at_level(logging.ERROR, logger="uktzed.records"):
        classification_id = await _begin(alice)
        await _finish(classification_id, _ledger())
        await fail_turn(
            classification_id,
            error_class="internal",
            tool_calls=[],
            turns=1,
            repairs=0,
            tokens_in=None,
            tokens_cached=None,
            tokens_out=None,
            ttfb_ms=None,
            duration_ms=10,
        )

    assert classification_id.startswith("cls_")
    logged = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(logged) == 3
    assert all(classification_id in r.getMessage() for r in logged)
    # `logger.exception`, not `logger.error`: the traceback is why the write failed.
    assert all(r.exc_info is not None for r in logged)


async def test_closing_a_record_that_was_never_opened_is_logged(
    pool: FakePool, caplog: pytest.LogCaptureFixture
) -> None:
    """begin_turn returns its id even when the INSERT failed, so this is the second half of
    that failure: nothing to close, said out loud, with the id that ties the two together."""
    with caplog.at_level(logging.ERROR, logger="uktzed.records"):
        await _finish("cls_never_inserted", _ledger())

    assert any("cls_never_inserted" in r.getMessage() for r in caplog.records)


async def test_close_turn_is_one_transaction(
    pool: FakePool, alice: RequestContext, caplog: pytest.LogCaptureFixture
) -> None:
    """The parent row and both child tables go in together or not at all.

    Two tool calls sharing a position violate `UNIQUE (classification_id, position)` on the
    second INSERT — after the parent UPDATE and the code INSERT have already run. A record
    that said `classified` while carrying half a tool trace would be a record nobody could
    trust, so the whole thing rolls back and the row stays `pending`.
    """
    ledger = _ledger()
    duplicated = [
        ToolCallRecord(index=0, tool_name="open_category", args={}, started_at=0.0),
        ToolCallRecord(index=0, tool_name="expand", args={}, started_at=0.0),
    ]
    classification_id = await _begin(alice)
    with caplog.at_level(logging.ERROR, logger="uktzed.records"):
        await _finish(classification_id, ledger, tool_calls=duplicated)

    row = await _row(pool, classification_id)
    assert row["outcome"] == "pending"
    assert row["finished_at"] is None
    assert await _codes(pool, classification_id) == []
    assert await _tool_calls(pool, classification_id) == []
    assert any(classification_id in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------------------


def test_price_usd_bills_cached_tokens_as_a_subset_of_input() -> None:
    """10,000 input of which 4,000 were cache hits, plus 2,000 output, on gpt-5.6-terra:

        6,000 x $2.00/1M  = $0.012000
        4,000 x $0.20/1M  = $0.000800
        2,000 x $12.00/1M = $0.024000
                            $0.036800

    Billing all 10,000 at the input rate would give $0.040000 — ~9% too high on this turn,
    and far more on a long tariff walk where the prompt cache is warm.
    """
    terra = price_usd("gpt-5.6-terra", 10_000, 4_000, 2_000)
    luna = price_usd("gpt-5.6-luna", 10_000, 4_000, 2_000)

    assert terra == Decimal("0.036800")
    assert luna == Decimal("0.003680")
    # Exactly a tenth of its big sibling, to the micro-dollar.
    assert luna is not None and terra == luna * 10


def test_price_usd_is_none_for_an_unknown_model() -> None:
    """A wrong cost is worse than no cost: the SPA renders None as «—», and a plausible
    number has nowhere to surface as wrong. A dated snapshot is a different model id, so it
    is unknown until someone prices it — inheriting the base price would be a guess."""
    assert price_usd("gpt-4.1", 1_000, 0, 1_000) is None
    assert price_usd("gpt-5.6-terra-2026-01-15", 1_000, 0, 1_000) is None
    assert price_usd(None, 1_000, 0, 1_000) is None


def test_price_usd_handles_a_turn_that_never_called_the_model() -> None:
    """A crash before the first request has no usage at all. That is a true zero, not an
    unknown — only an unpriced MODEL yields None."""
    assert price_usd("gpt-5.6-terra", None, None, None) == Decimal("0")
    assert price_usd("gpt-5.6-terra", 0, 0, 0) == Decimal("0.000000")


def test_price_usd_never_bills_negative_input(caplog: pytest.LogCaptureFixture) -> None:
    """If a provider ever reported cached tokens as a sibling of input rather than a subset,
    the subtraction would go negative and refund the customer. It clamps, loudly, high."""
    with caplog.at_level(logging.WARNING, logger="uktzed.records"):
        cost = price_usd("gpt-5.6-terra", 1_000, 4_000, 0)

    # 1,000 full-rate + 4,000 cached, i.e. the pessimistic reading of a nonsensical usage.
    assert cost == Decimal("0.002800")
    assert cost > Decimal("0")
    assert any("exceed input tokens" in r.getMessage() for r in caplog.records)


def test_every_price_is_a_decimal() -> None:
    """Money is never a float — `0.1 + 0.2` is where cost reports go to die."""
    for price in MODEL_PRICES.values():
        assert isinstance(price.input_per_mtok, Decimal)
        assert isinstance(price.cached_input_per_mtok, Decimal)
        assert isinstance(price.output_per_mtok, Decimal)
        # <= not <: a model may publish no cached rate at all (gpt-5.5-pro does not), and
        # cached input is then billed at the full input rate. A cached rate ABOVE the
        # input rate would still be a transcription error worth catching.
        assert price.cached_input_per_mtok <= price.input_per_mtok


def test_recorded_code_defaults_to_primary() -> None:
    """The common case is one code, and it is the answer."""
    assert RecordedCode(code="3919101200", description="інші", full_path=FULL_PATH).is_primary
