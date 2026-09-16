
# the en dash are correct typography here, not Latin look-alikes; pyproject pins ruff's
# defaults (no allowed-confusables), so the exemption is declared per file.
"""`ClassifierServer` — the whole ChatKit integration point.

`respond()` is the only abstract method on `ChatKitServer`, and it is called exactly once per
streaming request. That is what makes the turn ledger a sound provenance boundary: one HTTP
request == one assistant turn == one ledger.

Three decisions in here are load-bearing and easy to get wrong.

**First paint before anything else.** v1's median cold latency was 48.2s and only 7.5% of its
turns finished under 10 seconds, all of it behind one static string. The `ProgressUpdateEvent`
below is on the wire in tens of milliseconds, and from then on the navigation tools push a
`CustomTask` per step into a `Workflow` the user watches fill in. Reasoning summaries become
`ThoughtTask`s for free, but only because `summary="auto"` is set in `model_settings()`.

**A bounded window, rebuilt from the Store, and no `previous_response_id`.** v1 replayed the
whole transcript: max 796 items, 68% of its input token bill, and a 2.6x wall-clock tax at 750
items against 50. Chaining on `previous_response_id` would need `store=True` retention at
OpenAI and silently desyncs from ChatKit-only items, and our payloads are small enough that
the saving would be marginal anyway.

**No `store.add_thread_item` for assistant output.** `_process_events` persists on
`ThreadItemDoneEvent`, in the same loop iteration that transmits it. Writing it ourselves is a
double write and a primary-key violation.

**The classification record opens before the model runs and closes in `finally`.**
`begin_turn()` INSERTs the row `pending` right before `Runner.run_streamed`, and the same
`finally` that logs the turn summary calls `finish_turn()`/`fail_turn()` with the very
numbers it just logged — one set of values, two sinks, no second computation to drift. A turn
that dies in between stays `pending` with a NULL `finished_at`, which is a row you can query
rather than the absence v1 left behind. `app/records/writer.py` swallows and logs its own
failures, so nothing about the audit trail can break the user's stream.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

from agents import (
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    OutputGuardrailTripwireTriggered,
    Runner,
)
from chatkit.agents import stream_agent_response
from chatkit.errors import CustomStreamError
from chatkit.server import ChatKitServer
from chatkit.store import Store
from chatkit.types import (
    FeedbackKind,
    ProgressUpdateEvent,
    StructuredInputItem,
    ThreadMetadata,
    ThreadStreamEvent,
    UserMessageItem,
)

from app.agent.agent import build_agent
from app.agent.context import UktzedContext
from app.agent.prompts import PROMPT_VERSION, render_system_prompt
from app.chat.converter import converter
from app.chat.errors import classify_error, is_retryable, user_message
from app.context import RequestContext
from app.records import begin_turn, codes_from_ledger, fail_turn, finish_turn
from app.settings import get_settings
from app.tariff.catalogue import build_sections_catalogue
from app.tariff.repo import TariffRepo

logger = logging.getLogger("uktzed.chat")

__all__ = ["ClassifierServer"]


class ClassifierServer(ChatKitServer[RequestContext]):
    """`ChatKitServer` performs ZERO authorization — the `Store` is the boundary (§5.3)."""

    def __init__(self, store: Store[RequestContext], tariff: TariffRepo) -> None:
        # attachment_store is omitted deliberately: uploads are off, and an AttachmentStore we
        # do not need is three more methods that would have to be scoped by user_id.
        super().__init__(store)
        self.store = store  # narrows the type for the checker
        self.tariff = tariff
        self._catalogue: str | None = None
        self._dataset_sha: str | None = None

    async def section_catalogue(self) -> str:
        """P2, built once per process.

        `TariffRepo` documents itself as cheap and per-request, and it is — but the catalogue
        behind it is 21 + ~13 queries, it is frozen with the dataset, and a re-ingest means a
        restart in this deployment. So one long-lived repo and one cached string, deliberately,
        instead of rebuilding a constant on every turn.
        """
        if self._catalogue is None:
            self._catalogue = await build_sections_catalogue(self.tariff)
        return self._catalogue

    async def dataset_sha256(self) -> str | None:
        """Which tariff snapshot answered this turn, stamped onto the record.

        Cached like the catalogue and for the same reason: `TariffRepo` memoises the active
        dataset anyway, and a re-ingest means a restart in this deployment. Failure is not
        fatal — `dataset_sha256` is NULLable precisely because a turn can lose the repo — but
        it is loud, and it is caught here rather than inside the record writer so that a
        `TariffRepo` stand-in without this method (the M0 spike ships one) cannot take the
        stream down with an AttributeError.
        """
        if self._dataset_sha is None:
            try:
                self._dataset_sha = (await self.tariff.active_dataset()).sha256
            except Exception:
                logger.warning("active dataset unavailable; recording dataset_sha256=NULL")
                return None
        return self._dataset_sha

    async def respond(
        self,
        thread: ThreadMetadata,
        input_user_message: UserMessageItem | None,
        context: RequestContext,
    ) -> AsyncIterator[ThreadStreamEvent]:
        settings = get_settings()
        started = time.perf_counter()
        first_event_at: float | None = None

        # THE turn boundary. Everything the model is allowed to emit this turn gets recorded
        # into this ledger by the tools, and nothing survives from the previous turn — which
        # is the structural fix for v1's measured 36% stale-carry-over rate.
        ledger = context.ledger
        ledger.reset()

        # The SDK does NOT short-circuit locked or closed threads: it runs respond() and
        # persists the message anyway. Guard it here, first thing.
        if thread.status.type != "active":
            return

        # First paint. Everything after this point is allowed to take seconds.
        yield ProgressUpdateEvent(icon="compass", text="Аналізую опис товару…")

        # `input_user_message` is already persisted and echoed before respond() runs, so this
        # page contains it — do NOT append it again. `desc` + `reversed` is the LATEST N in
        # chronological order; `asc` would give the OLDEST N, which is the bug in OpenAI's own
        # quickstart store.
        page = await self.store.load_thread_items(
            thread_id=thread.id,
            after=None,
            limit=settings.history_window_items,
            order="desc",
            context=context,
        )
        input_items = await converter.to_agent_input(list(reversed(page.data)))
        if not input_items:
            # Reachable when a structured-input answer arrives for an empty thread:
            # `input_user_message` is None on that path and an empty input list is an API error.
            logger.warning("empty model input for thread %s; nothing to respond to", thread.id)
            return

        prompt_text, prompt_sha = render_system_prompt(
            catalogue=await self.section_catalogue(), locale=context.locale
        )

        agent_ctx = UktzedContext(
            thread=thread,
            store=self.store,
            request_context=context,
            tariff=self.tariff,
        )

        # The record exists BEFORE the model does. Everything from here on — a crash, the
        # wall clock, a client that walks away mid-stream — leaves a `pending` row with a
        # NULL `finished_at` behind instead of nothing at all. `begin_turn` never raises and
        # always returns the id, so there is nothing to branch on and the stream below is not
        # shaped by whether persistence worked.
        classification_id = await begin_turn(
            context,
            input_text=_message_text(input_user_message) or _last_user_text(input_items),
            thread_id=thread.id,
            model=settings.model,
            prompt_version=PROMPT_VERSION,
            prompt_sha256=prompt_sha,
            dataset_sha256=await self.dataset_sha256(),
        )

        result = Runner.run_streamed(
            build_agent(prompt_text),
            input_items,
            context=agent_ctx,
            max_turns=settings.max_turns,
        )

        error_class: str | None = None
        clarification_question: str | None = None
        try:
            async for event in stream_agent_response(agent_ctx, result):
                if first_event_at is None:
                    first_event_at = time.perf_counter()
                if clarification_question is None:
                    # Read off the wire, because that is the only place it exists: the agent
                    # context carries the clarification *outcome* but not its text, and the
                    # ledger entry records only the option count. Sniffing the item as it
                    # streams past keeps `ask_clarification` free of record-keeping.
                    clarification_question = _clarification_question(event)
                # A per-event deadline rather than `asyncio.timeout` around the loop:
                # cancelling a suspended async generator from the outside tears the stream
                # down at an arbitrary point. This bounds the gaps; `ModelSettings.timeout`
                # bounds a single stuck model call. Together that is the wall clock v1 never
                # had — it ate the SDK's 600s default twice, silently.
                if time.perf_counter() - started > settings.turn_wall_clock_s:
                    raise TimeoutError(
                        f"turn exceeded {settings.turn_wall_clock_s}s "
                        f"after {result.current_turn} model turns"
                    )
                yield event
        except (InputGuardrailTripwireTriggered, OutputGuardrailTripwireTriggered) as exc:
            # stream_agent_response has already retracted everything it emitted, then re-raised.
            error_class = "guardrail_tripwire"
            raise CustomStreamError("Запит заблоковано.", allow_retry=False) from exc
        except MaxTurnsExceeded as exc:
            error_class = "max_turns_exceeded"
            raise CustomStreamError(
                "Не вдалося завершити класифікацію. Спробуйте уточнити опис товару.",
                allow_retry=True,
            ) from exc
        except Exception as exc:
            # Without this the SDK emits a bare, message-less stream.error and logs the
            # traceback to a logger that has no handler unless LOG_LEVEL is set.
            error_class = classify_error(exc)
            # The run loop is a background task: abandoning the stream does not stop it, and a
            # wall-clock timeout that keeps paying for model calls is not a timeout.
            with suppress(Exception):
                result.cancel()
            logger.exception("turn failed: error_class=%s thread=%s", error_class, thread.id)
            # The message and the retry affordance both follow the CLASS. Offering "Повторити
            # спробу" on a billing exhaustion is worse than useless: the retry is guaranteed to
            # fail and it hides the real cause from whoever has to fix it.
            raise CustomStreamError(
                user_message(error_class), allow_retry=is_retryable(error_class)
            ) from exc
        finally:
            # Usage is STALE until the stream drains, so it is read here and never inside the
            # loop. This is the `finally` and not the success path on purpose: a cancelled or
            # failed turn still produced usage for the calls that completed, and v1's
            # equivalent ran only on success, which made every crashed turn invisible.
            usage = result.context_wrapper.usage
            ttfb_ms = _ms_between(started, first_event_at)
            duration_ms = _ms_since(started)
            logger.info(
                "turn done user=%s thread=%s outcome=%s error=%s model=%s prompt=%s/%s "
                "turns=%s repairs=%s tools=%s codes_seen=%s in=%s cached=%s out=%s "
                "ttfb_ms=%s latency_ms=%s",
                context.user_id,
                thread.id,
                agent_ctx.outcome,
                error_class,
                settings.model,  # from config, NEVER a literal in a log line
                PROMPT_VERSION,
                prompt_sha[:12],
                result.current_turn,
                agent_ctx.repairs,
                len(ledger.calls),
                ledger.codes_seen,
                usage.input_tokens,
                usage.input_tokens_details.cached_tokens,
                usage.output_tokens,
                ttfb_ms,
                duration_ms,
            )

            # Close the record with the numbers that were just logged — the same objects,
            # not a second reading of the same clock. `fail_turn` is the branch whenever an
            # `error_class` was assigned above; everything else is a turn that completed, and
            # `agent_ctx.outcome` is the agent's own three-value vocabulary, which the writer
            # maps onto the stored one. Neither call can raise: both swallow and log.
            #
            # A turn cancelled at the `yield` (the client closed the tab) reaches here with
            # the task already cancelled, so these awaits raise `CancelledError` straight
            # back out and the row is deliberately left `pending` — the state the schema has
            # for exactly this, and the one v1 could not distinguish from "never happened".
            if error_class is None:
                await finish_turn(
                    classification_id,
                    outcome=agent_ctx.outcome,
                    codes=codes_from_ledger(ledger, agent_ctx.emitted_codes),
                    clarification_question=clarification_question,
                    tool_calls=ledger.calls,
                    turns=result.current_turn,
                    repairs=agent_ctx.repairs,
                    tokens_in=usage.input_tokens,
                    tokens_cached=usage.input_tokens_details.cached_tokens,
                    tokens_out=usage.output_tokens,
                    ttfb_ms=ttfb_ms,
                    duration_ms=duration_ms,
                )
            else:
                await fail_turn(
                    classification_id,
                    error_class=error_class,
                    tool_calls=ledger.calls,
                    turns=result.current_turn,
                    repairs=agent_ctx.repairs,
                    tokens_in=usage.input_tokens,
                    tokens_cached=usage.input_tokens_details.cached_tokens,
                    tokens_out=usage.output_tokens,
                    ttfb_ms=ttfb_ms,
                    duration_ms=duration_ms,
                )

        # Mutating `thread` is enough: `_process_events` deep-compares it after every event and
        # does save_thread + ThreadUpdatedEvent for free. A model call for the title would be a
        # second model call on every first turn; the heuristic is free and good enough.
        if not thread.title:
            thread.title = _title_from(input_items) or "Класифікація"
        if result.last_response_id:
            # `ThreadMetadata.metadata` is dropped by `_to_thread_response` and never reaches
            # the client (measured), so this is server-private correlation with the dashboard.
            thread.metadata["last_response_id"] = result.last_response_id

    async def add_feedback(
        self,
        thread_id: str,
        item_ids: list[str],
        feedback: FeedbackKind,
        context: RequestContext,
    ) -> None:
        # DANGER: items.feedback does NOT go through the Store. ChatKitServer hands you a
        # client-supplied thread_id and item_ids with no ownership lookup at all, so the Store
        # is not the isolation boundary here — this method is. v2 ships with
        # threadItemActions.feedback = false, which is what makes this unreachable today.
        raise NotImplementedError("Enable only together with an explicit ownership check")


def _last_user_text(input_items: list[Any]) -> str:
    """The last user text in the model input, whole.

    Also the fallback for `input_text` on the structured-input path: answering a clarification
    produces no `UserMessageItem` at all, so the only place that turn's input exists is the
    converted history, where `structured_input_to_input` has already rendered the answer.
    """
    for item in reversed(input_items):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        for part in item.get("content") or []:
            text = (part or {}).get("text") if isinstance(part, dict) else None
            if text:
                return text.strip()
    return ""


def _title_from(input_items: list[Any]) -> str:
    """Last user text, trimmed. Cheap, deterministic, and no second model call."""
    return _last_user_text(input_items)[:60]


def _message_text(message: UserMessageItem | None) -> str:
    """This turn's user input, as typed. Empty on the structured-input path.

    Both content variants carry `.text` — a tag is an interactive label the user put into the
    message — so both contribute and the record holds what was actually sent.
    """
    if message is None:
        return ""
    parts = ((getattr(part, "text", "") or "").strip() for part in message.content)
    return "\n".join(part for part in parts if part)


def _clarification_question(event: Any) -> str | None:
    """The question `ask_clarification` just put on screen, or None for any other event."""
    item = getattr(event, "item", None)
    if not isinstance(item, StructuredInputItem):
        return None
    for structured in item.inputs:
        question = (getattr(structured, "question", "") or "").strip()
        if question:
            return question
    return None


def _ms_since(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _ms_between(t0: float, t1: float | None) -> int | None:
    return int((t1 - t0) * 1000) if t1 is not None else None
