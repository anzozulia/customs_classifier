"""The wiring pass: the seam between `respond()`, the record writer and the ASGI app.

The record writer and the history API were built in parallel against a fixed DDL and a fixed
TypeScript contract, and each is tested in isolation — `tests/test_records_writer.py` drives
`begin_turn`/`finish_turn` against a SQLite mirror of migration 0004, and
`tests/test_history_api.py` assembles its own FastAPI app around the router. Neither of them
can see the two things this file exists to pin, and both of them are exactly where a
milestone like this breaks:

* **`app/chat/server.py` actually calls the writer, in the right order.** The row is opened
  BEFORE `Runner.run_streamed` — that ordering is the whole design, not an implementation
  detail — and closed from the same `finally` that logs the turn summary, with the same
  numbers that line reports. `test_ttfb_and_duration_match_the_log_line` checks the
  "one reading, two sinks" property mechanically by parsing the log line back.

* **`app/main.py` includes the history router, and includes it before the SPA mount.**
  Starlette matches routes in registration order, so a router included after the `"/"` mount
  is unreachable: every `/api/history` request would be answered by the static handler.

No database and no network. `begin_turn`/`finish_turn`/`fail_turn` are recorded rather than
executed, `Runner.run_streamed` and `stream_agent_response` are replaced by a scripted
stream, and everything between them — `build_agent`, `render_system_prompt`, the real
`TurnLedger` and the real `codes_from_ledger` — runs for real.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from agents import MaxTurnsExceeded
from chatkit.errors import CustomStreamError
from chatkit.types import (
    ActiveStatus,
    InferenceOptions,
    Page,
    ProgressUpdateEvent,
    StructuredInputFreeform,
    StructuredInputItem,
    ThreadItemDoneEvent,
    ThreadMetadata,
    ThreadStreamEvent,
    UserMessageItem,
    UserMessageTextContent,
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Same three as tests/test_http_contract.py, and set the same way for the same reason:
# `Settings` is read at import time in `app.main` and `get_settings()` is lru_cached.
os.environ.setdefault("SESSION_SECRET", "test-secret-not-used-anywhere-real")
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")
os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:5432/unused")

import app.chat.server as server_module  # noqa: E402
import app.db as db  # noqa: E402
import app.runtime_settings as runtime_settings  # noqa: E402
from app.agent.provenance import CodeEvidence  # noqa: E402
from app.chat.server import ClassifierServer  # noqa: E402
from app.context import RequestContext  # noqa: E402
from app.records.writer import RecordedCode  # noqa: E402
from app.settings import get_settings  # noqa: E402

THREAD_ID = "thr_wiring"
DATASET_SHA = "5f2b" * 16
CODE = "3919101200"
CODE_DESCRIPTION = "у рулонах завширшки не більш як 20 см"
CODE_PATH = "пластмаси та вироби з них / плити, листи, плівки / самоклейні"


# ---------------------------------------------------------------------------- the doubles


@dataclass
class _TokenDetails:
    cached_tokens: int = 0


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    input_tokens_details: _TokenDetails = field(default_factory=_TokenDetails)


@dataclass
class _ContextWrapper:
    usage: _Usage


class _FakeRun:
    """The four things `respond()` reads off a `RunResultStreaming`."""

    def __init__(self, *, usage: _Usage, turns: int) -> None:
        self.context_wrapper = _ContextWrapper(usage)
        self.current_turn = turns
        self.last_response_id = "resp_wiring"
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _Store:
    """`AgentContext.store` is `SkipValidation`, so only the one method matters here."""

    def __init__(self, items: Sequence[Any]) -> None:
        self._items = list(items)

    async def load_thread_items(self, **_: Any) -> Page:
        return Page(data=list(self._items), has_more=False)


@dataclass(frozen=True)
class _Dataset:
    sha256: str


class _Tariff:
    async def active_dataset(self) -> _Dataset:
        return _Dataset(DATASET_SHA)


class _TariffWithoutDataset:
    """The shape `scripts/spike_m0.py` passes in: the agent's methods and nothing else."""


class _Recorder:
    """Records the writer calls instead of performing them, in call order."""

    def __init__(self) -> None:
        self.trace: list[str] = []
        self.begin: dict[str, Any] = {}
        self.finish: dict[str, Any] = {}
        self.fail: dict[str, Any] = {}
        self.classification_id = "cls_wiring_id"

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _begin(ctx: RequestContext, **kwargs: Any) -> str:
            self.trace.append("begin_turn")
            self.begin = {"ctx": ctx, **kwargs}
            return self.classification_id

        async def _finish(classification_id: str, **kwargs: Any) -> None:
            self.trace.append("finish_turn")
            self.finish = {"classification_id": classification_id, **kwargs}

        async def _fail(classification_id: str, **kwargs: Any) -> None:
            self.trace.append("fail_turn")
            self.fail = {"classification_id": classification_id, **kwargs}

        monkeypatch.setattr(server_module, "begin_turn", _begin)
        monkeypatch.setattr(server_module, "finish_turn", _finish)
        monkeypatch.setattr(server_module, "fail_turn", _fail)


def _thread(status: Any = None) -> ThreadMetadata:
    return ThreadMetadata(
        id=THREAD_ID, created_at=datetime.now(), status=status or ActiveStatus(), title=None
    )


def _user_message(text: str) -> UserMessageItem:
    return UserMessageItem(
        id="msg_wiring",
        thread_id=THREAD_ID,
        created_at=datetime.now(),
        content=[UserMessageTextContent(text=text)],
        inference_options=InferenceOptions(),
    )


def _clarification_event(question: str) -> ThreadItemDoneEvent:
    """What `ask_clarification` puts on the wire — the only place the question text exists."""
    return ThreadItemDoneEvent(
        item=StructuredInputItem(
            id="msg_structured",
            thread_id=THREAD_ID,
            created_at=datetime.now(),
            status="pending",
            inputs=[StructuredInputFreeform(id="in_1", question=question)],
        )
    )


def _script(
    *,
    events: Iterable[ThreadStreamEvent] = (),
    outcome: str | None = None,
    emitted: Sequence[str] = (),
    repairs: int = 0,
    with_evidence: bool = True,
    raises: BaseException | None = None,
) -> Any:
    """A stand-in for `stream_agent_response` that mutates the context the way the tools do.

    The agent context and the turn ledger are the REAL ones — `respond()` builds them — so
    what the writer receives has been through `codes_from_ledger` and the ledger's own
    provenance rules, not through a stub of them.
    """

    async def _stream(agent_ctx: Any, _result: Any) -> AsyncIterator[ThreadStreamEvent]:
        ledger = agent_ctx.ledger
        if with_evidence:
            record = ledger.begin_call("open_category", {"code": CODE[:4]})
            ledger.record_codes(
                [
                    CodeEvidence(
                        code=CODE,
                        description=CODE_DESCRIPTION,
                        full_path=CODE_PATH,
                        is_terminal=True,
                        tool_call_index=record.index,
                    )
                ]
            )
            ledger.end_call(record, digest="ok:1")
        for event in events:
            yield event
        agent_ctx.repairs = repairs
        agent_ctx.emitted_codes = list(emitted)
        if outcome is not None:
            agent_ctx.outcome = outcome
        if raises is not None:
            raise raises

    return _stream


def _server(
    monkeypatch: pytest.MonkeyPatch,
    *,
    items: Sequence[Any],
    stream: Any,
    run: _FakeRun,
    tariff: Any = None,
) -> ClassifierServer:
    server = ClassifierServer(_Store(items), tariff or _Tariff())
    # The catalogue is 21 + ~13 queries against the tariff; the prompt is rendered for real
    # around this string, so `prompt_sha256` on the record is a real digest.
    server._catalogue = "16 Машини та обладнання"

    class _Runner:
        @staticmethod
        def run_streamed(_agent: Any, _input: Any, **_kwargs: Any) -> _FakeRun:
            return run

    monkeypatch.setattr(server_module, "Runner", _Runner)
    monkeypatch.setattr(server_module, "stream_agent_response", stream)
    return server


async def _drain(server: ClassifierServer, message: UserMessageItem | None, **kwargs: Any) -> list:
    context = RequestContext(user_id=7, request_id="req-wiring", locale="uk")
    thread = kwargs.pop("thread", None) or _thread()
    return [event async for event in server.respond(thread, message, context)], context


# ---------------------------------------------------------------------------- the record


async def test_the_record_is_opened_before_the_model_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`begin_turn` before `run_streamed`, with the run parameters of the turn."""
    recorder = _Recorder()
    recorder.install(monkeypatch)
    order: list[str] = []

    server = _server(
        monkeypatch,
        items=[_user_message("плівка поліетиленова самоклейна")],
        stream=_script(outcome="result", emitted=[CODE]),
        run=_FakeRun(usage=_Usage(), turns=1),
    )
    inner = server_module.Runner.run_streamed

    class _OrderedRunner:
        @staticmethod
        def run_streamed(*args: Any, **kwargs: Any) -> _FakeRun:
            order.append("run_streamed")
            return inner(*args, **kwargs)

    monkeypatch.setattr(server_module, "Runner", _OrderedRunner)
    begin = server_module.begin_turn

    async def _ordered_begin(*args: Any, **kwargs: Any) -> str:
        order.append("begin_turn")
        return await begin(*args, **kwargs)

    monkeypatch.setattr(server_module, "begin_turn", _ordered_begin)

    await _drain(server, _user_message("плівка поліетиленова самоклейна"))

    assert order == ["begin_turn", "run_streamed"]
    assert recorder.begin["input_text"] == "плівка поліетиленова самоклейна"
    assert recorder.begin["thread_id"] == THREAD_ID
    assert recorder.begin["model"] == get_settings().model
    assert recorder.begin["dataset_sha256"] == DATASET_SHA
    assert recorder.begin["prompt_version"] == server_module.PROMPT_VERSION
    assert len(recorder.begin["prompt_sha256"]) == 64  # the real rendered-prompt digest
    assert recorder.begin["ctx"].user_id == 7


class _SettingsPool:
    """`app_setting`, the way `app/runtime_settings.py` reads it.

    One statement for all three keys, and JSONB comes back from asyncpg as a `str` because
    no codec is installed (`app/db.py`) — hence `json.dumps`.
    """

    def __init__(self, **values: str) -> None:
        self._values = values

    async def fetch(self, _sql: str, *_args: Any) -> list[dict[str, Any]]:
        return [
            {
                "key": key,
                "value": json.dumps(value),
                "updated_at": datetime.now(),
                "updated_by": None,
            }
            for key, value in self._values.items()
        ]

    async def fetchrow(self, _sql: str, *_args: Any) -> None:
        return None


async def test_the_runtime_model_and_effort_reach_the_agent_and_the_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The admin panel's model select is only real if the next turn actually runs that model.

    `respond()` resolves both values once, from `app/runtime_settings.py`, and feeds the same
    two strings to the agent it builds, to the `classification` row it opens and to the turn
    summary it logs. This asserts all three off ONE override written where a superuser's
    `PUT /api/admin/settings` writes it — not off a patched `get_runtime`, which would only
    prove that a mock was called.
    """
    recorder = _Recorder()
    recorder.install(monkeypatch)
    monkeypatch.setattr(db, "_pool", _SettingsPool(model="gpt-5.6-luna", reasoning_effort="high"))
    runtime_settings.invalidate_cache()
    agents: list[Any] = []

    try:
        server = _server(
            monkeypatch,
            items=[_user_message("мотоцикл 650 см3")],
            stream=_script(outcome="result", emitted=[CODE]),
            run=_FakeRun(usage=_Usage(), turns=1),
        )
        inner = server_module.Runner.run_streamed

        class _CapturingRunner:
            @staticmethod
            def run_streamed(agent: Any, *args: Any, **kwargs: Any) -> _FakeRun:
                agents.append(agent)
                return inner(agent, *args, **kwargs)

        monkeypatch.setattr(server_module, "Runner", _CapturingRunner)

        await _drain(server, _user_message("мотоцикл 650 см3"))
    finally:
        # The snapshot has a 3-second TTL and is a module global; leaving 'luna' in it would
        # fail the very next test in this file, which asserts the environment default.
        runtime_settings.invalidate_cache()

    assert len(agents) == 1
    # The agent that ran, not the one `app/settings.py` would have built.
    assert agents[0].model == "gpt-5.6-luna"
    assert agents[0].model_settings.reasoning.effort == "high"
    # …and the row records the model that ran, which is what `price_usd` is applied to.
    assert recorder.begin["model"] == "gpt-5.6-luna"
    assert get_settings().model != "gpt-5.6-luna", "otherwise this test proves nothing"


async def test_a_finished_turn_closes_the_record_with_ledger_resolved_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    recorder.install(monkeypatch)
    usage = _Usage(input_tokens=1200, output_tokens=340, input_tokens_details=_TokenDetails(768))
    server = _server(
        monkeypatch,
        items=[_user_message("плівка")],
        stream=_script(
            events=[ProgressUpdateEvent(icon="compass", text="…")],
            outcome="result",
            emitted=[CODE],
            repairs=2,
        ),
        run=_FakeRun(usage=usage, turns=4),
    )

    _, context = await _drain(server, _user_message("плівка"))

    assert recorder.trace == ["begin_turn", "finish_turn"]
    finish = recorder.finish
    assert finish["classification_id"] == recorder.classification_id
    # The AGENT's vocabulary is handed over untranslated: mapping it is the writer's job and
    # the stored value must stay lossless (`conversation` is not `classified` in the table).
    assert finish["outcome"] == "result"
    # The description and full_path are the DATABASE's text from the ledger, never the
    # model's — `emitted_codes` carried nothing but the bare code.
    assert finish["codes"] == [
        RecordedCode(code=CODE, description=CODE_DESCRIPTION, full_path=CODE_PATH, is_primary=True)
    ]
    assert finish["tool_calls"] == context.ledger.calls
    assert [call.tool_name for call in finish["tool_calls"]] == ["open_category"]
    assert finish["turns"] == 4
    assert finish["repairs"] == 2
    assert (finish["tokens_in"], finish["tokens_cached"], finish["tokens_out"]) == (
        1200,
        768,
        340,
    )
    assert finish["ttfb_ms"] is not None and finish["duration_ms"] is not None
    assert finish["clarification_question"] is None


async def test_a_code_without_ledger_evidence_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second wall behind `emit_classification`'s own gate: no evidence, no row."""
    recorder = _Recorder()
    recorder.install(monkeypatch)
    server = _server(
        monkeypatch,
        items=[_user_message("плівка")],
        stream=_script(outcome="result", emitted=["8517130000"], with_evidence=False),
        run=_FakeRun(usage=_Usage(), turns=1),
    )

    await _drain(server, _user_message("плівка"))

    assert recorder.finish["outcome"] == "result"
    assert recorder.finish["codes"] == []


async def test_the_clarification_question_is_read_off_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It exists nowhere else: the context carries the outcome, the ledger the option count."""
    recorder = _Recorder()
    recorder.install(monkeypatch)
    question = "Яка основа плівки: папір чи поліетилен?"
    server = _server(
        monkeypatch,
        items=[_user_message("плівка")],
        stream=_script(events=[_clarification_event(question)], outcome="clarification"),
        run=_FakeRun(usage=_Usage(), turns=2),
    )

    events, _ = await _drain(server, _user_message("плівка"))

    # Sniffing the item must not consume it: the user still gets the question.
    assert any(isinstance(getattr(event, "item", None), StructuredInputItem) for event in events)
    assert recorder.finish["outcome"] == "clarification"
    assert recorder.finish["clarification_question"] == question


async def test_a_conversation_turn_is_handed_over_losslessly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`conversation` is the agent's default outcome and is collapsed at the API, not here."""
    recorder = _Recorder()
    recorder.install(monkeypatch)
    server = _server(
        monkeypatch,
        items=[_user_message("добрий день")],
        stream=_script(),
        run=_FakeRun(usage=_Usage(), turns=1),
    )

    await _drain(server, _user_message("добрий день"))

    assert recorder.finish["outcome"] == "conversation"
    assert recorder.finish["codes"] == []


@pytest.mark.parametrize(
    ("exc", "error_class"),
    [
        (ValueError("boom"), "internal"),
        (MaxTurnsExceeded("too many"), "max_turns_exceeded"),
    ],
)
async def test_a_failed_turn_closes_the_record_as_an_error(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException, error_class: str
) -> None:
    """The token bill and the tool trace of a failed turn survive; the codes do not."""
    recorder = _Recorder()
    recorder.install(monkeypatch)
    usage = _Usage(input_tokens=900, output_tokens=12, input_tokens_details=_TokenDetails(64))
    server = _server(
        monkeypatch,
        items=[_user_message("плівка")],
        stream=_script(outcome="result", emitted=[CODE], raises=exc),
        run=_FakeRun(usage=usage, turns=3),
    )

    with pytest.raises(CustomStreamError):
        await _drain(server, _user_message("плівка"))

    assert recorder.trace == ["begin_turn", "fail_turn"]
    fail = recorder.fail
    assert fail["classification_id"] == recorder.classification_id
    assert fail["error_class"] == error_class
    assert fail["turns"] == 3
    assert (fail["tokens_in"], fail["tokens_cached"], fail["tokens_out"]) == (900, 64, 12)
    assert [call.tool_name for call in fail["tool_calls"]] == ["open_category"]
    assert "codes" not in fail  # fail_turn has no codes parameter, by design


async def test_no_record_is_opened_for_a_locked_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """No model call, no row: a `pending` record nothing will ever close is worse than none."""
    from chatkit.types import LockedStatus

    recorder = _Recorder()
    recorder.install(monkeypatch)
    server = _server(
        monkeypatch,
        items=[_user_message("плівка")],
        stream=_script(),
        run=_FakeRun(usage=_Usage(), turns=0),
    )

    events, _ = await _drain(
        server, _user_message("плівка"), thread=_thread(LockedStatus(reason=None))
    )

    assert events == []
    assert recorder.trace == []


async def test_no_record_is_opened_when_there_is_no_model_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    recorder.install(monkeypatch)
    server = _server(monkeypatch, items=[], stream=_script(), run=_FakeRun(usage=_Usage(), turns=0))

    await _drain(server, None)

    assert recorder.trace == []


async def test_input_text_falls_back_to_the_converted_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Answering a clarification produces no `UserMessageItem` — `input_user_message` is None."""
    recorder = _Recorder()
    recorder.install(monkeypatch)
    server = _server(
        monkeypatch,
        items=[_user_message("поліетилен, товщина 0,1 мм")],
        stream=_script(),
        run=_FakeRun(usage=_Usage(), turns=1),
    )

    await _drain(server, None)

    assert recorder.begin["input_text"] == "поліетилен, товщина 0,1 мм"


async def test_a_persistence_failure_does_not_break_the_stream_and_is_not_silent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The REAL writer, with no pool behind it — the M0 spike's exact situation.

    Nothing is recorded here, and that is the test: `get_pool()` raises
    `PoolNotInitialisedError` inside both halves of the lifecycle, the turn streams to the end
    anyway, and both failures reach the log at ERROR carrying the classification id that ties
    them together. v1's fifteen bare `except:` blocks are what this is the opposite of.
    """
    import app.db as db

    monkeypatch.setattr(db, "_pool", None)
    server = _server(
        monkeypatch,
        items=[_user_message("плівка")],
        stream=_script(outcome="result", emitted=[CODE]),
        run=_FakeRun(usage=_Usage(), turns=1),
    )

    with caplog.at_level(logging.ERROR, logger="uktzed.records"):
        events, _ = await _drain(server, _user_message("плівка"))

    assert events  # the user's turn was not shaped by the audit trail failing
    failures = [r for r in caplog.records if r.name == "uktzed.records" and r.exc_info]
    assert [r.getMessage().split(":")[0] for r in failures] == [
        "record begin_turn failed",
        "record close failed",
    ]
    assert all("cls_" in record.getMessage() for record in failures)


async def test_a_tariff_without_a_dataset_records_null_and_keeps_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`scripts/spike_m0.py` passes a stand-in with no `active_dataset()` at all."""
    recorder = _Recorder()
    recorder.install(monkeypatch)
    server = _server(
        monkeypatch,
        items=[_user_message("плівка")],
        stream=_script(outcome="result", emitted=[CODE]),
        run=_FakeRun(usage=_Usage(), turns=1),
        tariff=_TariffWithoutDataset(),
    )

    events, _ = await _drain(server, _user_message("плівка"))

    assert recorder.begin["dataset_sha256"] is None
    assert recorder.trace == ["begin_turn", "finish_turn"]
    assert events  # the first paint still went out


# ---------------------------------------------------------------------------- the log line


async def test_the_turn_done_log_line_survives(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`scripts/spike_m0.py` greps this line for `turns=`, `in=` and `out=`."""
    _Recorder().install(monkeypatch)
    usage = _Usage(input_tokens=1200, output_tokens=340, input_tokens_details=_TokenDetails(768))
    server = _server(
        monkeypatch,
        items=[_user_message("плівка")],
        stream=_script(outcome="result", emitted=[CODE]),
        run=_FakeRun(usage=usage, turns=4),
    )

    with caplog.at_level(logging.INFO, logger="uktzed.chat"):
        await _drain(server, _user_message("плівка"))

    line = next(m for m in caplog.messages if m.startswith("turn done"))
    assert re.search(r"turns=(\d+)", line).group(1) == "4"
    assert re.search(r"\bin=(\d+)", line).group(1) == "1200"
    assert re.search(r"\bout=(\d+)", line).group(1) == "340"
    assert "outcome=result" in line and "error=None" in line


async def test_ttfb_and_duration_match_the_log_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One reading of the clock, two sinks. Recomputing either is how they drift apart."""
    recorder = _Recorder()
    recorder.install(monkeypatch)
    server = _server(
        monkeypatch,
        items=[_user_message("плівка")],
        stream=_script(
            events=[ProgressUpdateEvent(icon="compass", text="…")], outcome="result", emitted=[CODE]
        ),
        run=_FakeRun(usage=_Usage(), turns=1),
    )

    with caplog.at_level(logging.INFO, logger="uktzed.chat"):
        await _drain(server, _user_message("плівка"))

    line = next(m for m in caplog.messages if m.startswith("turn done"))
    assert re.search(r"ttfb_ms=(\S+)", line).group(1) == str(recorder.finish["ttfb_ms"])
    assert re.search(r"latency_ms=(\S+)", line).group(1) == str(recorder.finish["duration_ms"])


# ---------------------------------------------------------------------------- the ASGI app


def test_the_history_router_is_mounted_on_the_app() -> None:
    """All three routes, and `export.csv` declared before `{entry_id}`.

    The order is load-bearing twice over: `{entry_id}` would otherwise swallow the literal
    "export.csv" segment, and the whole router has to precede the `"/"` mount.
    """
    import app.main as main

    paths = list(main.app.openapi()["paths"])
    assert {"/api/history", "/api/history/export.csv", "/api/history/{entry_id}"} <= set(paths)
    assert paths.index("/api/history/export.csv") < paths.index("/api/history/{entry_id}")


def test_history_is_not_swallowed_by_the_spa_mount() -> None:
    """401 from the router, not 404 (or an index.html) from the static handler.

    The request is anonymous on purpose: `current_user` rejects it before it can reach the
    database, so this proves the route is both reachable and behind authentication without
    needing a pool. The client is deliberately NOT used as a context manager — that would run
    the lifespan, which opens a real connection to Postgres.
    """
    from fastapi.testclient import TestClient

    import app.main as main

    client = TestClient(main.app, base_url="http://testserver")
    for path in ("/api/history?limit=20", "/api/history/export.csv", "/api/history/cls_x"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.headers["content-type"].startswith("application/json"), path


async def test_a_client_that_leaves_cancels_the_model_run_and_closes_the_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1 from the deploy audit — the one runtime defect that costs money.

    Closing the tab cancels the REQUEST task. The Agents SDK run is a separate background
    task that `stream_agent_response` only reads from, so before this fix the model kept
    calling tools and generating tokens to completion — every one billed — for a reply
    nobody would see, and the row stayed `pending` with nothing recorded. `CancelledError`
    is a BaseException, which is exactly why the `except Exception` path never caught it.
    """
    recorder = _Recorder()
    recorder.install(monkeypatch)
    usage = _Usage(input_tokens=500, output_tokens=7, input_tokens_details=_TokenDetails(40))
    run = _FakeRun(usage=usage, turns=2)

    async def _hanging_stream(*_args: Any, **_kwargs: Any) -> Any:
        # The model is "still thinking" when the visitor leaves: never yields, never ends.
        await asyncio.Event().wait()
        yield None  # pragma: no cover - makes this an async generator

    server = _server(
        monkeypatch, items=[_user_message("плівка")], stream=_hanging_stream, run=run
    )

    task = asyncio.create_task(_drain(server, _user_message("плівка")))
    await asyncio.sleep(0.01)  # past begin_turn and into the hanging stream
    assert recorder.trace == ["begin_turn"], "cancelled before the run started — test is wrong"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.01)  # let the detached fail_turn task run

    assert run.cancelled is True, "the model run must be cancelled, not just the HTTP request"
    assert recorder.trace == ["begin_turn", "fail_turn"]
    fail = recorder.fail
    assert fail["classification_id"] == recorder.classification_id
    assert fail["error_class"] == "cancelled"
    assert fail["turns"] == 2
    assert (fail["tokens_in"], fail["tokens_cached"], fail["tokens_out"]) == (500, 40, 7)
