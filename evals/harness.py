"""Run one golden case through the product, and write down exactly what happened.

THE RULE THIS FILE OBEYS: what the eval measures must be what ships. So this drives
`ClassifierServer.respond()` — the real entry point, the real `build_agent()`, the real
rendered prompt, the real tools over a real `TariffRepo` against a real Postgres, and the real
terminal-tool gate with its provenance ledger. There is no second classification path in here
to drift away from the first one. `scripts/spike_m0.py` already proved the shape (drive
`respond()` headlessly, iterate the stream, read the turn summary); this is that shape pointed
at the real tariff and run over a list of cases.

WHAT IS AND IS NOT FAKED

* **The tariff is real.** `TariffRepo` over the asyncpg pool, the active dataset, the same SQL
  the product runs. Groundedness would mean nothing against a stub.
* **Postgres is real**, so `begin_turn`/`finish_turn` write a `classification` row per case
  exactly as they do in production. The eval turns land in the same metrics table, which is
  the point: `thread_id` is nullable and unconstrained precisely so an eval run can write
  there. A failed write never fails a case — the writer swallows and logs (see its docstring).
* **The Store is in memory.** It is the ONE thing swapped, and not because it is hard: writing
  eval threads through `PgStore` would inject 40 synthetic conversations into a real user's
  sidebar. The Store is not part of what an eval measures — `tests/test_store_isolation.py`
  owns it — and one turn per case never reads anything back out of it.

WHERE THE NUMBERS COME FROM

`respond()` owns its `UktzedContext`, so the harness cannot reach inside for the outcome or
the token count. It does not need to: the turn's own instrumentation already publishes both.

* The **turn ledger** (`RequestContext.ledger`) is shared with the run and carries every tool
  call, its arguments, its duration and its error — the same object `app/records/writer.py`
  persists from. The emitted codes are read off the accepted `emit_classification` call.
* The **turn summary** — `uktzed.chat`'s single `turn done …` INFO line, emitted in
  `respond()`'s `finally` once usage has settled — carries outcome, turns, repairs, tokens and
  latency. It is keyed by `thread=`, which is unique per case, so a capture handler can match
  it under concurrency. The M0 spike parses the same line for the same reason: usage is stale
  until the stream drains, so this is the only place those numbers exist at the right time.

Concurrency is a bounded semaphore (default 4) so a 50-case run is minutes, not an hour. The
bound matters: each case is one streaming turn holding a model connection and a pool
connection, and the pool is sized from it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

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

from app.agent.prompts import PROMPT_VERSION, render_system_prompt
from app.agent.provenance import ToolCallRecord, TurnLedger
from app.chat.errors import classify_error
from app.chat.server import ClassifierServer
from app.context import RequestContext
from app.db import close_pool, init_pool
from app.records.pricing import price_usd
from app.settings import get_settings
from app.tariff.repo import TariffRepo, normalize_code
from app.tariff.validate import validate_and_resolve
from evals.scoring import Case, EmittedCode, Observation, ToolCall

__all__ = ["EvalStore", "Harness", "HarnessOptions", "RunMeta", "open_harness"]

logger = logging.getLogger("uktzed.evals")

DEFAULT_CONCURRENCY = 4
"""Four streaming turns at once. Enough to turn an hour into minutes, low enough that the
model provider's rate limiter is not what the eval ends up measuring."""


@dataclass(frozen=True, slots=True)
class HarnessOptions:
    concurrency: int = DEFAULT_CONCURRENCY
    user_id: int = 1
    """Owner of the `classification` rows. `user_id` is a FOREIGN KEY, so a user that does not
    exist means the record write is skipped and logged — the case itself still runs."""
    locale: str = "uk"
    database_url: str | None = None


@dataclass(frozen=True, slots=True)
class RunMeta:
    """Which system produced the run — read from the live objects, not from a log line."""

    model: str
    prompt_version: str
    prompt_sha256: str
    dataset_sha256: str | None


# --------------------------------------------------------------------------------------
# The in-memory Store — the one faked collaborator, and the reason is in the module docstring
# --------------------------------------------------------------------------------------


@dataclass
class EvalStore(Store[RequestContext]):
    """All 14 `Store` methods, in dicts. Shaped after `scripts/spike_m0.py`'s MemoryStore.

    No `user_id` scoping, deliberately: isolation is `PgStore`'s job and is pinned by
    `tests/test_store_isolation.py`. Every eval case gets its own thread id, so there is
    nothing here for one case to read out of another.
    """

    threads: dict[str, ThreadMetadata] = field(default_factory=dict)
    items: dict[str, list[ThreadItem]] = field(default_factory=dict)
    attachments: dict[str, Attachment] = field(default_factory=dict)

    def generate_thread_id(self, context: RequestContext) -> str:
        return f"thr_eval_{uuid.uuid4().hex[:16]}"

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
        # `_process_events` hands back a full `Thread` (ThreadMetadata + items) after the first
        # mutation; storing that would keep a copy of the whole item page on the thread row.
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
        self, thread_id: str, after: str | None, limit: int, order: str, context: RequestContext
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

    async def load_item(self, thread_id: str, item_id: str, context: RequestContext) -> ThreadItem:
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


# --------------------------------------------------------------------------------------
# The turn summary, captured off the product's own logger
# --------------------------------------------------------------------------------------

_FIELD_RE = re.compile(r"(\w+)=(\S+)")
_SUMMARY_PREFIX = "turn done"


class _TurnLog(logging.Handler):
    """Captures `uktzed.chat`'s `turn done …` line and files it under its `thread=` id.

    Parsed as loose `key=value` pairs rather than against the format string: a field that
    moves or disappears then reads as `None` instead of raising, and a missing token count is
    worth far less than a crashed eval run.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.summaries: dict[str, dict[str, str]] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # a broken log record must never break the run
            return
        if not message.startswith(_SUMMARY_PREFIX):
            return
        fields = dict(_FIELD_RE.findall(message))
        thread_id = fields.get("thread")
        if thread_id:
            self.summaries[thread_id] = fields

    def take(self, thread_id: str) -> dict[str, str]:
        return self.summaries.pop(thread_id, {})


def _int(fields: dict[str, str], key: str) -> int | None:
    value = fields.get(key)
    if value is None or value == "None":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _text(fields: dict[str, str], key: str) -> str | None:
    value = fields.get(key)
    return None if value is None or value == "None" else value


# --------------------------------------------------------------------------------------
# The harness
# --------------------------------------------------------------------------------------


class Harness:
    """Runs golden cases against one live server. Construct it with `open_harness`."""

    def __init__(
        self,
        *,
        server: ClassifierServer,
        store: EvalStore,
        repo: TariffRepo,
        turn_log: _TurnLog,
        options: HarnessOptions,
    ) -> None:
        self._server = server
        self._store = store
        self._repo = repo
        self._log = turn_log
        self._options = options
        self._semaphore = asyncio.Semaphore(max(1, options.concurrency))

    async def meta(self) -> RunMeta:
        """Model, prompt and dataset identity — read off the live objects.

        Not parsed out of a log line: this is what a baseline is compared against, and a
        baseline whose provenance is a substring of a print statement is v1's problem again.
        """
        settings = get_settings()
        catalogue = await self._server.section_catalogue()
        _, prompt_sha = render_system_prompt(catalogue=catalogue, locale=self._options.locale)
        return RunMeta(
            model=settings.model,
            prompt_version=PROMPT_VERSION,
            prompt_sha256=prompt_sha,
            dataset_sha256=await self._server.dataset_sha256(),
        )

    async def run_case(self, case: Case) -> Observation:
        """One case, one turn, one observation. Never raises: a failed case is a data point."""
        async with self._semaphore:
            return await self._run(case)

    async def run_many(
        self,
        cases: Sequence[Case],
        *,
        on_done: Callable[[Case, Observation], None] | None = None,
    ) -> list[Observation]:
        """Every case, at most `concurrency` at a time, results in the order given."""

        async def one(case: Case) -> Observation:
            observation = await self.run_case(case)
            if on_done is not None:
                on_done(case, observation)
            return observation

        return list(await asyncio.gather(*(one(case) for case in cases)))

    # -- internals ---------------------------------------------------------------------

    async def _run(self, case: Case) -> Observation:
        context = RequestContext(
            user_id=self._options.user_id,
            request_id=f"eval-{case.id}",
            locale=self._options.locale,
        )
        thread = ThreadMetadata(
            id=self._store.generate_thread_id(context),
            created_at=datetime.now(),
            status=ActiveStatus(),
        )
        await self._store.save_thread(thread, context)
        message = UserMessageItem(
            id=self._store.generate_item_id("message", thread, context),
            thread_id=thread.id,
            created_at=datetime.now(),
            content=[UserMessageTextContent(text=case.input)],
            inference_options=InferenceOptions(),
        )
        await self._store.add_thread_item(thread.id, message, context)

        failure: BaseException | None = None
        started = time.perf_counter()
        try:
            async for event in self._server.respond(thread, message, context):
                # What the SDK's own `_process_events` would persist. One turn per case never
                # reads it back, but a stored transcript is what makes a case reproducible by
                # hand afterwards.
                if getattr(event, "type", "") == "thread.item.done":
                    await self._store.add_thread_item(thread.id, event.item, context)
        except Exception as exc:
            # A case that fails is a measurement, not a crash: `error_rate` is a metric and
            # one bad case must not take the other 39 down with it.
            failure = exc
            logger.warning("case %s failed: %s: %s", case.id, type(exc).__name__, exc)
        wall_ms = int((time.perf_counter() - started) * 1000)

        # Free the transcript as soon as it is scored: a 50-case run holds 50 threads of
        # tariff payloads otherwise, and nothing reads them again.
        with suppress(Exception):
            await self._store.delete_thread(thread.id, context)

        return await self._observe(
            case=case,
            fields=self._log.take(thread.id),
            ledger=context.ledger,
            wall_ms=wall_ms,
            failure=failure,
        )

    async def _observe(
        self,
        *,
        case: Case,
        fields: dict[str, str],
        ledger: TurnLedger,
        wall_ms: int,
        failure: BaseException | None,
    ) -> Observation:
        """Turn the ledger plus the turn summary into one scored-ready observation."""
        error_class = _text(fields, "error")
        if failure is not None and error_class is None:
            # The stream died before `respond()` could label it — classify it the way the
            # product would, through the same closed vocabulary.
            error_class = classify_error(failure)

        # The writer stores a failed turn as `outcome = 'error'`; mirror that here so an eval
        # row and a `classification` row say the same thing about the same turn.
        outcome = "error" if error_class else (_text(fields, "outcome") or _outcome(ledger))

        model = _text(fields, "model") or get_settings().model
        prompt = (_text(fields, "prompt") or "").split("/", 1)
        tokens_in = _int(fields, "in") or 0
        tokens_cached = _int(fields, "cached") or 0
        tokens_out = _int(fields, "out") or 0

        return Observation(
            case_id=case.id,
            outcome=outcome,
            codes=await self._resolve_codes(_emitted_codes(ledger)),
            turns=_int(fields, "turns") or 0,
            repairs=_int(fields, "repairs") or 0,
            tool_calls=tuple(_tool_call(call) for call in ledger.calls),
            tokens_in=tokens_in,
            tokens_cached=tokens_cached,
            tokens_out=tokens_out,
            cost_usd=price_usd(model, tokens_in, tokens_cached, tokens_out),
            latency_ms=_int(fields, "latency_ms") or wall_ms,
            ttfb_ms=_int(fields, "ttfb_ms"),
            wall_ms=wall_ms,
            error_class=error_class,
            model=model,
            prompt_version=prompt[0],
            prompt_sha256=prompt[1] if len(prompt) > 1 else "",
            codes_seen=_int(fields, "codes_seen") or 0,
        )

    async def _resolve_codes(self, codes: Sequence[str]) -> tuple[EmittedCode, ...]:
        """The groundedness lookup: every emitted code, against the active dataset.

        `validate_and_resolve` is the SAME function `emit_classification` gates on, so this
        asks the gate's own question a second time, in SQL, after the fact. A lookup that
        itself fails is recorded as `lookup_failed` and therefore counts as ungrounded — an
        unanswerable question about a customs code is not a pass.
        """
        resolved: list[EmittedCode] = []
        for code in codes:
            try:
                status, detail = await validate_and_resolve(self._repo, code)
            except Exception:
                logger.exception("groundedness lookup failed for %s", code)
                resolved.append(EmittedCode(code=code, status="lookup_failed", is_terminal=False))
                continue
            resolved.append(
                EmittedCode(
                    code=code,
                    status=status.value,
                    is_terminal=bool(detail and detail.is_terminal),
                    full_path=detail.full_path if detail else "",
                )
            )
        return tuple(resolved)


def _tool_call(call: ToolCallRecord) -> ToolCall:
    return ToolCall(
        name=call.tool_name,
        args=dict(call.args or {}),
        duration_ms=call.duration_ms,
        error=call.error,
    )


def _emitted_codes(ledger: TurnLedger) -> tuple[str, ...]:
    """The codes the user was actually shown, in the order the answer lists them.

    Read off the ACCEPTED `emit_classification` call: a rejected one (a repair) emitted
    nothing to anybody, and the model may have called it several times before one passed. The
    ledger is the same source `codes_from_ledger()` uses for the stored record.
    """
    for call in reversed(ledger.calls):
        if call.tool_name == "emit_classification" and call.error is None:
            raw: Iterable[Any] = call.args.get("codes") or ()
            return tuple(normalize_code(str(code)) for code in raw if str(code).strip())
    return ()


def _outcome(ledger: TurnLedger) -> str:
    """Fallback outcome when the turn summary never arrived (it always should).

    Derived exactly the way the product decides it: which terminal tool succeeded, if any.
    """
    for call in reversed(ledger.calls):
        if call.error is not None:
            continue
        if call.tool_name == "emit_classification":
            return "result"
        if call.tool_name == "ask_clarification":
            return "clarification"
    return "conversation"


@asynccontextmanager
async def open_harness(
    options: HarnessOptions | None = None,
) -> AsyncIterator[Harness]:
    """Bring up the real dependencies once, run cases, tear them down.

    The section catalogue is built here rather than lazily inside the first case: it is 21+13
    queries and it is frozen with the dataset, so paying for it inside case #1's clock would
    put a fixed startup cost into one case's latency and nowhere else.
    """
    resolved = options or HarnessOptions()
    settings = get_settings()

    handler = _TurnLog()
    chat_logger = logging.getLogger("uktzed.chat")
    previous_level = chat_logger.level
    chat_logger.setLevel(logging.INFO)
    chat_logger.addHandler(handler)

    try:
        # The pool must outnumber the in-flight turns: every tool call is a query, and a turn
        # that waits for a connection is latency the eval would report as model latency. Inside
        # the `try` so that a database that will not come up still restores the logger.
        pool = await init_pool(
            resolved.database_url or settings.database_url,
            max_size=max(10, resolved.concurrency * 2),
        )
        repo = TariffRepo(pool)
        store = EvalStore()
        server = ClassifierServer(store, repo)
        await server.section_catalogue()
        yield Harness(server=server, store=store, repo=repo, turn_log=handler, options=resolved)
    finally:
        chat_logger.removeHandler(handler)
        chat_logger.setLevel(previous_level)
        await close_pool()
