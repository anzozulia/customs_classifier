#!/usr/bin/env python3
# ruff: noqa: RUF001  -- Ukrainian text below. Single-letter Cyrillic words and
# the en dash are correct typography here, not Latin look-alikes; pyproject pins ruff's
# defaults (no allowed-confusables), so the exemption is declared per file.
"""M0 — the risk spike, run against the live API.

Nothing in the ChatKit + Agents SDK integration has ever been run against a real OpenAI API.
This script runs it, with an in-memory Store and a hand-written eight-node tariff, and prints a
PASS/FAIL table for the exit criteria that can be checked from the server side.

    OPENAI_API_KEY=sk-... python3 scripts/spike_m0.py

Checked here:

  * a tool-calling stream completes without an exception inside `stream_agent_response`
    (the 1.6.5 fix is verified from a diff and never from a live tool-calling run);
  * a REJECTED terminal tool produces ANOTHER model turn rather than a dead run — the §6.4
    finding, executed: `StopAtTools` matches the CALL, so only a `ToolsToFinalOutputFunction`
    can keep the run alive;
  * reasoning summary events arrive as `ThoughtTask`s, which is what `summary="auto"` buys;
  * `ProgressUpdateEvent` and the drill-down `CustomTask`s are emitted;
  * `result.current_turn` and `usage` are recorded at all — v1 never recorded a token count.

Three exit criteria are deliberately NOT here because they are visual and need a browser:
`ProgressUpdateEvent` rendering, the Thinking panel, and a `.widget` result card.

This drives `ClassifierServer.respond()` directly rather than `process()`: `respond` is where
every one of the criteria above lives, and driving it directly keeps the spike one file with
no HTTP server, no auth and no Postgres.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chatkit.store import NotFoundError, Store, StoreItemType
from chatkit.types import (
    ActiveStatus,
    Attachment,
    InferenceOptions,
    Page,
    ThreadItem,
    ThreadMetadata,
    UserMessageItem,
    UserMessageTextContent,
)

from app.agent import PathStep
from app.agent.schemas import CategoryTree, NodeDetail, NodeSummary, SectionSummary, TreeNode
from app.chat.server import ClassifierServer
from app.context import RequestContext
from app.settings import get_settings
from app.tariff.repo import UnknownCodeError

PROMPT = (
    "Класифікуй товар: смартфон Apple iPhone 15 — стільниковий телефон із сенсорним екраном, "
    "корпус з алюмінію та скла, для мереж стільникового зв'язку.\n\n"
    "ТЕСТОВА ІНСТРУКЦІЯ (частина цього запиту, виконай її буквально): спершу виклич "
    "emit_classification із кодом 0000000000 — це навмисна перевірка обробки помилок. "
    "Інструмент відмовить. Після відмови пройди довідником (list_categories_in_group, "
    "open_category) і виклич emit_classification зі справжнім кодом."
)


# ---------------------------------------------------------------------------
# A hand-built tariff with the real shapes: 2/2/4/10 digits, `full_path` on every row,
# `is_terminal` as an authored field and never as `len(code) == 10`. Duck-typed against
# `TariffRepo` — the tools only ever call methods, and a fake Postgres would be a fake
# Postgres.
# ---------------------------------------------------------------------------

_S16 = "Машини, обладнання та механізми; електротехнічне обладнання; їх частини"
_G85 = "Електричні машини, обладнання та їх частини; апаратура для запису звуку"
_H8517 = "Телефонні апарати, включаючи смартфони, для стільникових мереж зв'язку"
_SEP = " > "

_P85 = f"{_S16}{_SEP}{_G85}"
_P8517 = f"{_P85}{_SEP}{_H8517}"
_P851713 = f"{_P8517}{_SEP}смартфони"


def _summary(
    code: str, level: str, description: str, full_path: str, terminal: bool
) -> NodeSummary:
    return NodeSummary(
        code=code,
        level=level,
        description=description,
        full_path=full_path,
        is_terminal=terminal,
    )


_GROUPS = [
    _summary(
        "84", "group", "Реактори ядерні, котли, машини", f"{_S16}{_SEP}Реактори ядерні", False
    ),
    _summary("85", "group", _G85, _P85, False),
]
_CATEGORIES = [
    _summary(
        "8516", "category", "Електричні водонагрівачі", f"{_P85}{_SEP}Водонагрівачі", False
    ),
    _summary("8517", "category", _H8517, _P8517, False),
]
_TERMINALS = {
    "8517130000": _summary("8517130000", "code", "смартфони", _P851713, True),
    "8517140000": _summary(
        "8517140000",
        "code",
        "інші телефони для стільникових мереж зв'язку",
        f"{_P8517}{_SEP}інші телефони для стільникових мереж зв'язку",
        True,
    ),
}

_ANCESTRY: dict[str, list[NodeSummary]] = {
    "84": [],
    "85": [],
    "8516": [_GROUPS[1]],
    "8517": [_GROUPS[1]],
    "851713": [_GROUPS[1], _CATEGORIES[1]],
    "8517130000": [_GROUPS[1], _CATEGORIES[1]],
    "8517140000": [_GROUPS[1], _CATEGORIES[1]],
}

_TREE_8517 = [
    TreeNode(
        code="851713",
        description="смартфони",
        is_terminal=False,
        collapsed=False,
        children=[
            TreeNode(
                code="8517130000",
                description="смартфони",
                is_terminal=True,
                collapsed=False,
                children=[],
            )
        ],
    ),
    TreeNode(
        code="8517140000",
        description="інші телефони для стільникових мереж зв'язку",
        is_terminal=True,
        collapsed=False,
        children=[],
    ),
]


def _detail(row: NodeSummary, *, child_count: int, ancestors: list[str], depth: int) -> NodeDetail:
    return NodeDetail(
        code=row.code,
        level=row.level,
        description=row.description,
        full_path=row.full_path,
        is_terminal=row.is_terminal,
        depth=depth,
        child_count=child_count,
        ancestor_codes=ancestors,
        path_is_ambiguous=False,
        is_dead_end=False,
    )


class SpikeTariff:
    """Exactly the `TariffRepo` methods the agent layer calls, and nothing else."""

    async def list_sections(self) -> list[SectionSummary]:
        return [
            SectionSummary(
                code="16",
                description=_S16,
                group_count=2,
                first_group_code="84",
                last_group_code="85",
            )
        ]

    async def list_groups_in_section(self, section_code: str) -> list[NodeSummary]:
        return list(_GROUPS) if section_code == "16" else []

    async def list_categories_in_group(self, group_code: str) -> list[NodeSummary]:
        return list(_CATEGORIES) if group_code == "85" else []

    async def open_category(self, category_code: str) -> CategoryTree:
        if category_code != "8517":
            raise UnknownCodeError(category_code, "category")
        return CategoryTree(
            code="8517",
            level="category",
            description=_H8517,
            full_path=_P8517,
            path=[
                PathStep(level="section", code="16", description=_S16),
                PathStep(level="group", code="85", description=_G85),
                PathStep(level="category", code="8517", description=_H8517),
            ],
            nodes=_TREE_8517,
            terminal_count=2,
            truncated=False,
            is_dead_end=False,
        )

    async def expand(self, prefix: str) -> CategoryTree:
        return await self.open_category(prefix)

    async def resolve(self, code: str) -> NodeDetail | None:
        if code in _TERMINALS:
            return _detail(
                _TERMINALS[code], child_count=0, ancestors=["16", "85", "8517"], depth=4
            )
        for row in (*_GROUPS, *_CATEGORIES):
            if row.code == code:
                return _detail(row, child_count=2, ancestors=["16"], depth=1)
        return None

    async def ancestry(self, code: str) -> list[NodeSummary]:
        return _ANCESTRY.get(code, [])

    async def search_candidates(self, query: str, limit: int = 20) -> list[NodeSummary]:
        return [_CATEGORIES[1], *_TERMINALS.values()][:limit]


# ---------------------------------------------------------------------------
# In-memory Store. All 14 methods, because the ABC will not instantiate otherwise.
# ---------------------------------------------------------------------------


@dataclass
class MemoryStore(Store[RequestContext]):
    threads: dict[str, ThreadMetadata] = field(default_factory=dict)
    items: dict[str, list[ThreadItem]] = field(default_factory=dict)
    attachments: dict[str, Attachment] = field(default_factory=dict)

    def generate_thread_id(self, context: RequestContext) -> str:
        return f"thr_{uuid.uuid4().hex}"

    def generate_item_id(
        self, item_type: StoreItemType, thread: ThreadMetadata, context: RequestContext
    ) -> str:
        return f"{item_type[:3]}_{uuid.uuid4().hex[:16]}"

    async def load_thread(self, thread_id: str, context: RequestContext) -> ThreadMetadata:
        try:
            return self.threads[thread_id]
        except KeyError as exc:
            raise NotFoundError(thread_id) from exc

    async def save_thread(self, thread: ThreadMetadata, context: RequestContext) -> None:
        if hasattr(thread, "items"):
            thread = ThreadMetadata.model_validate(thread.model_dump(exclude={"items"}))
        self.threads[thread.id] = thread

    async def load_threads(
        self, limit: int, after: str | None, order: str, context: RequestContext
    ) -> Page[ThreadMetadata]:
        rows = list(self.threads.values())
        if order != "asc":
            rows.reverse()
        return Page(data=rows[:limit], has_more=len(rows) > limit)

    async def delete_thread(self, thread_id: str, context: RequestContext) -> None:
        self.threads.pop(thread_id, None)
        self.items.pop(thread_id, None)

    async def load_thread_items(
        self,
        thread_id: str,
        after: str | None,
        limit: int,
        order: str,
        context: RequestContext,
    ) -> Page[ThreadItem]:
        rows = list(self.items.get(thread_id, []))
        if order != "asc":
            rows = list(reversed(rows))
        return Page(data=rows[:limit], has_more=len(rows) > limit)

    async def add_thread_item(
        self, thread_id: str, item: ThreadItem, context: RequestContext
    ) -> None:
        self.items.setdefault(thread_id, []).append(item)

    async def save_item(self, thread_id: str, item: ThreadItem, context: RequestContext) -> None:
        rows = self.items.setdefault(thread_id, [])
        for index, existing in enumerate(rows):
            if existing.id == item.id:
                rows[index] = item
                return
        rows.append(item)

    async def load_item(
        self, thread_id: str, item_id: str, context: RequestContext
    ) -> ThreadItem:
        for existing in self.items.get(thread_id, []):
            if existing.id == item_id:
                return existing
        raise NotFoundError(item_id)

    async def delete_thread_item(
        self, thread_id: str, item_id: str, context: RequestContext
    ) -> None:
        rows = self.items.get(thread_id, [])
        self.items[thread_id] = [row for row in rows if row.id != item_id]

    async def save_attachment(self, attachment: Attachment, context: RequestContext) -> None:
        self.attachments[attachment.id] = attachment

    async def load_attachment(self, attachment_id: str, context: RequestContext) -> Attachment:
        try:
            return self.attachments[attachment_id]
        except KeyError as exc:
            raise NotFoundError(attachment_id) from exc

    async def delete_attachment(self, attachment_id: str, context: RequestContext) -> None:
        self.attachments.pop(attachment_id, None)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


class _LogCapture(logging.Handler):
    """The turn summary is a log line on purpose: `usage` is stale until the stream drains, so
    `respond()` reads it in `finally`. Capturing the line is how the spike sees it."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _event_kind(event: Any) -> tuple[str, str]:
    """(event type, inner item/task type) — enough to score the criteria without pattern
    matching a union whose members are Annotated aliases rather than classes."""
    etype = getattr(event, "type", "?")
    item = getattr(event, "item", None)
    update = getattr(event, "update", None)
    inner = getattr(item, "type", None) or getattr(update, "type", None) or ""
    task = getattr(update, "task", None) or getattr(item, "task", None)
    workflow = getattr(item, "workflow", None)
    if task is not None:
        inner = f"{inner}:{getattr(task, 'type', '?')}"
    elif workflow is not None:
        inner = f"{inner}:{getattr(workflow, 'type', '?')}"
    return etype, inner


async def run_spike() -> int:
    settings = get_settings()
    if not (os.environ.get("OPENAI_API_KEY") or settings.openai_api_key):
        print("OPENAI_API_KEY is not set — this spike talks to the live API. Aborting.")
        return 2

    capture = _LogCapture()
    chat_logger = logging.getLogger("uktzed.chat")
    chat_logger.setLevel(logging.INFO)
    chat_logger.addHandler(capture)

    store = MemoryStore()
    tariff = SpikeTariff()
    server = ClassifierServer(store, tariff)
    context = RequestContext(user_id=1, request_id="spike-m0", locale="uk")

    thread = ThreadMetadata(
        id=store.generate_thread_id(context), created_at=datetime.now(), status=ActiveStatus()
    )
    await store.save_thread(thread, context)
    user_message = UserMessageItem(
        id=store.generate_item_id("message", thread, context),
        thread_id=thread.id,
        created_at=datetime.now(),
        content=[UserMessageTextContent(text=PROMPT)],
        inference_options=InferenceOptions(),
    )
    await store.add_thread_item(thread.id, user_message, context)

    print(f"model={settings.model} effort={settings.reasoning_effort} "
          f"window={settings.history_window_items} max_repairs={settings.max_repairs}")
    print("streaming…\n")

    events: list[tuple[str, str]] = []
    stream_error: BaseException | None = None
    started = datetime.now()
    try:
        async for event in server.respond(thread, user_message, context):
            kind = _event_kind(event)
            events.append(kind)
            # Persist what the SDK's own _process_events would persist, so the second turn of
            # a real conversation would see the same thread.
            if kind[0] == "thread.item.done":
                await store.add_thread_item(thread.id, event.item, context)
            print(f"  {kind[0]:<26} {kind[1]}")
    except BaseException as exc:  # the spike reports, it does not propagate
        stream_error = exc

    elapsed = (datetime.now() - started).total_seconds()
    ledger = context.ledger
    tool_names = [call.tool_name for call in ledger.calls]
    rejected = [
        c for c in ledger.calls if c.tool_name == "emit_classification" and c.error
    ]
    accepted = [
        call
        for call in ledger.calls
        if call.tool_name == "emit_classification" and not call.error
    ]
    thoughts = [e for e in events if "thought" in e[1] or "reasoning" in e[1]]
    progress = [e for e in events if e[0] == "progress_update"]
    workflow = [e for e in events if "workflow" in e[0] or "workflow" in e[1]]
    messages = [e for e in events if e[1].startswith("assistant_message")]

    summary = next((line for line in capture.lines if line.startswith("turn done")), "")
    turns = int(m.group(1)) if (m := re.search(r"turns=(\d+)", summary)) else 0
    in_tokens = int(m.group(1)) if (m := re.search(r"in=(\d+)", summary)) else 0
    out_tokens = int(m.group(1)) if (m := re.search(r"out=(\d+)", summary)) else 0

    checks: list[tuple[str, bool, str]] = [
        (
            "tool-calling stream completed without exception",
            # `len(events) > 1` so a stream that produced nothing but the first paint cannot
            # pass by simply not failing.
            stream_error is None and len(events) > 1,
            f"{type(stream_error).__name__}: {stream_error}"
            if stream_error is not None
            else f"{len(events)} events",
        ),
        (
            "navigation tools actually ran",
            any(name not in {"emit_classification", "ask_clarification"} for name in tool_names),
            ", ".join(tool_names) or "none",
        ),
        (
            "ProgressUpdateEvent emitted",
            bool(progress),
            f"{len(progress)} event(s)",
        ),
        (
            "drill-down surfaced as workflow tasks",
            bool(workflow),
            f"{len(workflow)} workflow event(s)",
        ),
        (
            "reasoning summary events appeared",
            bool(thoughts),
            f"{len(thoughts)} thought event(s); needs summary='auto'",
        ),
        (
            # Observational, NOT a gate: this can only fire if the model spontaneously emits
            # a bad code, which it usually does not. Phase 2 forces the rejection and gates
            # on it deterministically. A FAIL here would mean "the model got it right", which
            # is not a defect — so the pass condition is simply "no rejection was left hanging".
            "repair path (opportunistic; phase 2 is the real gate)",
            not rejected or bool(accepted),
            f"rejected={[c.error for c in rejected]} accepted={len(accepted)} "
            + ("(model was right first time — see phase 2)" if not rejected else ""),
        ),
        (
            "a visible item was produced (terminal tools are total)",
            bool(messages),
            f"{len(messages)} assistant message item(s)",
        ),
        (
            "result.current_turn and usage recorded",
            turns > 0 and in_tokens > 0 and out_tokens > 0,
            f"turns={turns} input_tokens={in_tokens} output_tokens={out_tokens}",
        ),
        (
            "multi-turn tool loop ran (>1 model turn)",
            turns >= 2,
            f"current_turn={turns}",
        ),
        (
            "provenance ledger recorded evidence",
            ledger.codes_seen > 0,
            f"{ledger.codes_seen} codes across {len(ledger.calls)} calls",
        ),
    ]

    width = max(len(name) for name, _, _ in checks)
    print("\n" + "=" * (width + 60))
    print(f"{'CHECK'.ljust(width)}  RESULT  DETAIL")
    print("=" * (width + 60))
    failures = 0
    for name, ok, detail in checks:
        failures += 0 if ok else 1
        print(f"{name.ljust(width)}  {'PASS' if ok else 'FAIL':<6}  {detail}")
    print("=" * (width + 60))
    print(f"elapsed {elapsed:.1f}s · events {len(events)} · tool calls {len(ledger.calls)}")
    if summary:
        print(summary)
    print(
        "NOT CHECKED HERE (needs a browser): ProgressUpdateEvent rendering, the Thinking "
        "panel, a .widget result card, StructuredInputItem round-trip."
    )
    if stream_error is not None:
        import traceback

        traceback.print_exception(stream_error)
    return 1 if failures else 0


async def run_forced_rejection() -> int:
    """Phase 2 — prove architecture decision D13 with a DETERMINISTIC rejection.

    Phase 1 can only observe the repair path if the model happens to get it wrong, which it
    usually does not. So we force `TurnLedger.require` to raise exactly once, which is what a
    real stale-carry-over hit looks like from inside `emit_classification`.

    What is under test: `stop_at_tool_names` matches on the tool NAME, not the tool RESULT
    (verified in agents/run_internal/turn_resolution.py), so it would end the run even on a
    rejected emit. `finalize_on_terminal_tool` inspects the Ack instead. If this phase fails,
    the prompt's promise that the model may correct itself is a lie and D13 needs rework.
    """
    from app.agent.provenance import ProvenanceError, TurnLedger

    original_require = TurnLedger.require
    fired = {"n": 0}

    def flaky_require(self, code):  # type: ignore[no-untyped-def]
        evidence = original_require(self, code)
        if fired["n"] == 0:
            fired["n"] += 1
            raise ProvenanceError(f"FORCED TEST REJECTION for {code!r} (spike phase 2)")
        return evidence

    TurnLedger.require = flaky_require  # type: ignore[method-assign]
    try:
        store = MemoryStore()
        server = ClassifierServer(store, SpikeTariff())
        context = RequestContext(user_id=1, request_id="spike-m0-reject", locale="uk")
        thread = ThreadMetadata(
            id=store.generate_thread_id(context), created_at=datetime.now(), status=ActiveStatus()
        )
        await store.save_thread(thread, context)
        message = UserMessageItem(
            id=store.generate_item_id("message", thread, context),
            thread_id=thread.id,
            created_at=datetime.now(),
            content=[UserMessageTextContent(text=PROMPT)],
            inference_options=InferenceOptions(),
        )
        await store.add_thread_item(thread.id, message, context)

        answered = False
        async for event in server.respond(thread, message, context):
            kind = _event_kind(event)
            if kind[0] == "thread.item.done":
                await store.add_thread_item(thread.id, event.item, context)
                if kind[1] == "assistant_message":
                    answered = True
    finally:
        TurnLedger.require = original_require  # type: ignore[method-assign]

    ok = fired["n"] == 1 and answered
    print()
    print("=" * 110)
    print("PHASE 2 — forced terminal-tool rejection (architecture D13)")
    print("=" * 110)
    verdict_fired = "PASS" if fired["n"] == 1 else "FAIL"
    print(f"{'forced rejection fired'.ljust(54)}  {verdict_fired:<6}  n={fired['n']}")
    print(f"{'model repaired and still answered'.ljust(54)}  {'PASS' if answered else 'FAIL':<6}  "
          f"assistant_message={answered}")
    print("=" * 110)
    print(
        "D13 verdict: "
        + (
            "a rejected terminal tool did NOT kill the run."
            if ok
            else "REJECTION KILLED THE RUN — D13 needs rework."
        )
    )
    return 0 if ok else 1


async def main() -> int:
    rc = await run_spike()
    rc |= await run_forced_rejection()
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
