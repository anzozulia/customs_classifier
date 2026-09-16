# ruff: noqa: RUF001  -- Ukrainian text below. Single-letter Cyrillic words and
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
    ThreadMetadata,
    ThreadStreamEvent,
    UserMessageItem,
)

from app.agent.agent import build_agent
from app.agent.context import UktzedContext
from app.agent.prompts import PROMPT_VERSION, render_system_prompt
from app.chat.converter import converter
from app.chat.errors import classify_error
from app.context import RequestContext
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

        result = Runner.run_streamed(
            build_agent(prompt_text),
            input_items,
            context=agent_ctx,
            max_turns=settings.max_turns,
        )

        error_class: str | None = None
        try:
            async for event in stream_agent_response(agent_ctx, result):
                if first_event_at is None:
                    first_event_at = time.perf_counter()
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
            raise CustomStreamError(
                "Внутрішня помилка. Спробуйте ще раз.", allow_retry=True
            ) from exc
        finally:
            # Usage is STALE until the stream drains, so it is read here and never inside the
            # loop. This is the `finally` and not the success path on purpose: a cancelled or
            # failed turn still produced usage for the calls that completed, and v1's
            # equivalent ran only on success, which made every crashed turn invisible.
            usage = result.context_wrapper.usage
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
                _ms_between(started, first_event_at),
                _ms_since(started),
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


def _title_from(input_items: list[Any]) -> str:
    """Last user text, trimmed. Cheap, deterministic, and no second model call."""
    for item in reversed(input_items):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        for part in item.get("content") or []:
            text = (part or {}).get("text") if isinstance(part, dict) else None
            if text:
                return text.strip()[:60]
    return ""


def _ms_since(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _ms_between(t0: float, t1: float | None) -> int | None:
    return int((t1 - t0) * 1000) if t1 is not None else None
