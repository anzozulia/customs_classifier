# the en dash are correct typography here, not Latin look-alikes; pyproject pins ruff's
# defaults (no allowed-confusables), so the exemption is declared per file.
"""The provenance gate.

v1's measured failure mode was NOT cold hallucination (~1.3% of fresh answers) but STALE
CARRY-OVER: 36% of continued turns answered from tool output replayed in context from an
earlier turn, potentially about a different product. Nothing in v1 could tell the two apart and
nothing forbade the second.

These tests pin the structural fix: a code is emittable only if THIS turn's tool results
contained it, the repair loop that rejection opens actually runs, and it is bounded.

The tools are exercised through `FunctionTool.__wrapped__`, the callable `function_tool` wraps.
That skips the SDK's JSON-schema validation, which is the right trade here: the gate is the
subject, and the schema is the SDK's own well-tested machinery.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pytest
from agents import RunContextWrapper
from chatkit.types import ActiveStatus, ThreadMetadata

from app.agent.agent import finalize_on_terminal_tool
from app.agent.context import UktzedContext
from app.agent.provenance import CodeEvidence
from app.agent.schemas import Ack, Alternative, NodeDetail
from app.agent.tools_terminal import ClassifiedCode, emit_classification
from app.context import RequestContext
from app.settings import get_settings

# The REAL separator (app.tariff.PATH_SEPARATOR is " > "). This fixture used "\u203a", which
# no full_path in the database contains, so the whole string parsed as ONE segment and the
# renderer was never exercised on a real path shape here.
DB_FULL_PATH = (
    "Машини, обладнання та механізми > Електричні машини, обладнання та їх частини > "
    "Телефонні апарати, включаючи смартфони > смартфони"
)
MODEL_INVENTED_TEXT = "Смартфон преміального сегмента з титановим корпусом"


class _FakeStore:
    """Only the two id generators `AgentContext.generate_id` reaches."""

    def generate_thread_id(self, context: Any) -> str:
        return f"thr_{uuid.uuid4().hex}"

    def generate_item_id(self, item_type: str, thread: Any, context: Any) -> str:
        return f"{item_type[:3]}_{uuid.uuid4().hex[:12]}"


def _row(code: str, *, terminal: bool = True, ambiguous: bool = False) -> NodeDetail:
    return NodeDetail(
        code=code,
        level="code",
        description="смартфони",
        full_path=DB_FULL_PATH,
        is_terminal=terminal,
        depth=4,
        child_count=0 if terminal else 3,
        ancestor_codes=["16", "85", "8517"],
        path_is_ambiguous=ambiguous,
        is_dead_end=False,
    )


class _Repo:
    """Enough of TariffRepo to drive `validate_and_resolve`: it only calls `resolve`."""

    def __init__(self, rows: dict[str, NodeDetail]) -> None:
        self.rows = rows

    async def resolve(self, code: str) -> NodeDetail | None:
        return self.rows.get(code)


def _context(*, repo: Any = None) -> UktzedContext:
    request_context = RequestContext(user_id=7, request_id="test", locale="uk")
    thread = ThreadMetadata(id="thr_test", created_at=datetime.now(), status=ActiveStatus())
    return UktzedContext(
        thread=thread,
        store=_FakeStore(),
        request_context=request_context,
        tariff=repo,
    )


def _wrap(agent_ctx: UktzedContext) -> RunContextWrapper[UktzedContext]:
    return RunContextWrapper(context=agent_ctx)


def _walk_to(agent_ctx: UktzedContext, code: str, *, terminal: bool = True) -> None:
    """Simulate what `open_category` does: record the call AND the evidence it surfaced."""
    ledger = agent_ctx.ledger
    record = ledger.begin_call("open_category", {"category_code": code[:4]})
    ledger.record_codes(
        [
            CodeEvidence(
                code=code,
                description="смартфони",
                full_path=DB_FULL_PATH,
                is_terminal=terminal,
                tool_call_index=record.index,
            )
        ]
    )
    ledger.end_call(record, digest="CategoryTree:1")


async def _emit(agent_ctx: UktzedContext, code: str, **overrides: Any) -> Ack:
    kwargs: dict[str, Any] = {
        "codes": [ClassifiedCode(code=code, full_description=MODEL_INVENTED_TEXT)],
        "confidence": "висока",
        "rationale": "Апарат для стільникового зв'язку з сенсорним екраном.",
        "path": [],
        "alternatives": [],
        "product_summary": "Смартфон.",
        "evidence": "опис користувача",
        "label": "Смартфони",
    }
    kwargs.update(overrides)
    return await emit_classification.__wrapped__(_wrap(agent_ctx), **kwargs)


def _streamed_text(agent_ctx: UktzedContext) -> str:
    """Everything the tool pushed into the AgentContext queue, as one string."""
    chunks: list[str] = []
    queue = agent_ctx._events  # the queue is the only handle on streamed items
    while not queue.empty():
        event = queue.get_nowait()
        item = getattr(event, "item", None)
        for part in getattr(item, "content", None) or []:
            text = getattr(part, "text", None)
            if text:
                chunks.append(text)
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# E4 — the stale-carry-over fix
# ---------------------------------------------------------------------------


async def test_rejects_code_the_ledger_never_saw() -> None:
    agent_ctx = _context()
    _walk_to(agent_ctx, "8517130000")  # a real walk, to a DIFFERENT product

    ack = await _emit(agent_ctx, "8471300000")  # plausible, ten digits, never surfaced

    assert ack.ok is False
    assert ack.error_code == "provenance_violation"
    assert "8471300000" in ack.message
    assert agent_ctx.outcome != "result"
    assert agent_ctx.emitted_codes == []


async def test_rejects_code_from_a_previous_turn_after_reset() -> None:
    """The ledger is reset per turn, which is the whole point: turn 1's code must not be
    emittable on turn 2 about a different product."""
    agent_ctx = _context()
    _walk_to(agent_ctx, "8517130000")
    assert (await _emit(agent_ctx, "8517130000")).ok is True

    agent_ctx.ledger.reset()
    agent_ctx.outcome = "conversation"
    ack = await _emit(agent_ctx, "8517130000")

    assert ack.ok is False
    assert ack.error_code == "provenance_violation"


async def test_accepts_a_code_the_ledger_did_see() -> None:
    agent_ctx = _context()
    _walk_to(agent_ctx, "8517130000")

    ack = await _emit(agent_ctx, "8517130000")

    assert ack.ok is True
    assert ack.error_code is None
    assert agent_ctx.outcome == "result"
    assert agent_ctx.emitted_codes == ["8517130000"]


async def test_answer_text_comes_from_the_database_not_the_model() -> None:
    """D25/E7: `full_path` and `description` are ALWAYS overwritten from the DB."""
    agent_ctx = _context(repo=_Repo({"8517130000": _row("8517130000")}))
    _walk_to(agent_ctx, "8517130000")

    ack = await _emit(agent_ctx, "8517130000")
    rendered = _streamed_text(agent_ctx)

    assert ack.ok is True
    # The invariant is WHOSE TEXT WINS, not how it is laid out. The renderer deliberately
    # drops the section and the chapter (a 10-digit code's legal description begins at the
    # 4-digit heading) and splits the rest into heading + narrowing, so asserting the whole
    # stored string verbatim would pin the layout instead of the guarantee.
    heading, _, narrowing = DB_FULL_PATH.rpartition(" > ")
    heading = heading.rpartition(" > ")[2]
    assert heading in rendered, "the heading must come from the database"
    assert narrowing in rendered, "the narrowing must come from the database"
    assert MODEL_INVENTED_TEXT not in rendered
    assert "8517 13 00 00" in rendered  # format_code, at the render boundary, once


async def test_rejects_a_code_missing_from_the_dataset() -> None:
    """E2 — the ledger says a tool showed it, but the database says it is not there."""
    agent_ctx = _context(repo=_Repo({}))
    _walk_to(agent_ctx, "8517130000")

    ack = await _emit(agent_ctx, "8517130000")

    assert ack.ok is False
    assert ack.error_code == "code_not_in_dataset"


async def test_rejects_an_intermediate_code() -> None:
    """E3 — against the authored column, never against `len(code) == 10`."""
    agent_ctx = _context()
    _walk_to(agent_ctx, "8517130000", terminal=False)

    ack = await _emit(agent_ctx, "8517130000")

    assert ack.ok is False
    assert ack.error_code == "code_not_leaf"


async def test_rejects_a_malformed_code_before_anything_else() -> None:
    agent_ctx = _context()
    _walk_to(agent_ctx, "8517130000")

    ack = await _emit(agent_ctx, "8517 13")

    assert ack.ok is False
    assert ack.error_code == "code_format"


async def test_rejects_a_search_only_hit() -> None:
    """A `search_candidates` hit is a hypothesis. Without this the pre-retrieval shortcut
    degrades into echoing the top hit and the walk stops being a guarantee."""
    agent_ctx = _context()
    ledger = agent_ctx.ledger
    record = ledger.begin_call("search_candidates", {"query": "смартфон", "limit": 5})
    ledger.record_codes(
        [
            CodeEvidence(
                code="8517130000",
                description="смартфони",
                full_path=DB_FULL_PATH,
                is_terminal=True,
                tool_call_index=record.index,
            )
        ]
    )
    ledger.end_call(record, digest="CandidateList:1")
    # A walk happened, but to a different branch entirely.
    _walk_to(agent_ctx, "8471300000")

    ack = await _emit(agent_ctx, "8517130000")

    assert ack.ok is False
    assert ack.error_code == "code_not_navigated"


async def test_alternatives_go_through_the_same_gate() -> None:
    """E5 — an ungrounded alternative is an ungrounded code."""
    agent_ctx = _context()
    _walk_to(agent_ctx, "8517130000")

    ack = await _emit(
        agent_ctx,
        "8517130000",
        alternatives=[
            Alternative(
                code="8471300000",
                description="машина обчислювальна портативна",
                reason="товар не є обчислювальною машиною",
            )
        ],
    )

    assert ack.ok is False
    assert ack.error_code == "provenance_violation"


# ---------------------------------------------------------------------------
# The repair loop: `StopAtTools` matches the CALL, so only the callable can do this.
# ---------------------------------------------------------------------------


@dataclass
class _FakeTool:
    name: str


@dataclass
class _FakeToolResult:
    tool: _FakeTool
    output: Any
    run_item: Any = None


def _rejection() -> list[_FakeToolResult]:
    return [
        _FakeToolResult(
            tool=_FakeTool("emit_classification"),
            output=Ack(ok=False, error_code="provenance_violation", message="ні"),
        )
    ]


async def test_a_rejected_terminal_tool_keeps_the_run_alive() -> None:
    agent_ctx = _context()
    outcome = await finalize_on_terminal_tool(_wrap(agent_ctx), _rejection())

    assert outcome.is_final_output is False
    assert outcome.final_output is None
    assert agent_ctx.repairs == 1


async def test_a_successful_terminal_tool_ends_the_run() -> None:
    agent_ctx = _context()
    results = [
        _FakeToolResult(
            tool=_FakeTool("emit_classification"),
            output=Ack(ok=True, error_code=None, message="ok"),
        )
    ]

    outcome = await finalize_on_terminal_tool(_wrap(agent_ctx), results)

    assert outcome.is_final_output is True
    assert isinstance(outcome.final_output, Ack)
    assert agent_ctx.repairs == 0


async def test_repairs_are_bounded() -> None:
    max_repairs = get_settings().max_repairs
    agent_ctx = _context()

    for attempt in range(1, max_repairs + 1):
        outcome = await finalize_on_terminal_tool(_wrap(agent_ctx), _rejection())
        assert outcome.is_final_output is False, f"gave up on attempt {attempt}"

    final = await finalize_on_terminal_tool(_wrap(agent_ctx), _rejection())

    assert final.is_final_output is True
    assert agent_ctx.repairs == max_repairs + 1
    assert agent_ctx.outcome == "error"
    # Totality: the give-up path is the one place nothing else can render.
    assert "Не вдалося підтвердити код" in _streamed_text(agent_ctx)


async def test_a_non_terminal_tool_never_ends_the_run() -> None:
    agent_ctx = _context()
    results = [_FakeToolResult(tool=_FakeTool("open_category"), output={"коди": []})]

    outcome = await finalize_on_terminal_tool(_wrap(agent_ctx), results)

    assert outcome.is_final_output is False
    assert agent_ctx.repairs == 0


@pytest.mark.parametrize(
    "code", ["", "85171300", "851713000", "851713000a", "85171300001", "0000000000"]
)
async def test_malformed_codes_are_all_rejected(code: str) -> None:
    agent_ctx = _context()
    _walk_to(agent_ctx, "8517130000")

    ack = await _emit(agent_ctx, code)

    assert ack.ok is False
    assert ack.error_code in {"code_format", "provenance_violation"}


async def test_separators_are_normalised_rather_than_rejected() -> None:
    """`XXXX XX XX XX` is how the tariff is printed, so a code that arrives formatted is a
    formatting question, not a wrong answer. v1 made this the model's job, in prose, twice."""
    agent_ctx = _context()
    _walk_to(agent_ctx, "8517130000")

    ack = await _emit(agent_ctx, "8517 13 00 00")

    assert ack.ok is True
    assert agent_ctx.emitted_codes == ["8517130000"]
