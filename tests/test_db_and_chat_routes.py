"""The pool, and the one rule the streaming endpoint exists to enforce.

Two small modules that nothing else in the suite covers end to end, and both of them fail in
the same expensive way — silently, at the seam between a process and its dependencies.

**`app/db.py`.** One pool for the whole process, created in the lifespan. The three things
that can go wrong are a DSN in SQLAlchemy spelling (`DATABASE_URL` is shared with the
migration runner, which wants `+asyncpg`; asyncpg does not), a `get_pool()` before the
lifespan ran, and a `close_pool()` on an already-closed pool. The middle one is why
`PoolNotInitialisedError` exists at all: returning `None` there would turn a wiring bug into
an `AttributeError` several frames away, in whichever request happened to be first.

**`app/chat/routes.py`.** Once `StreamingResponse` has put its headers on the wire, an
exception inside the generator can no longer become an HTTP status — Starlette raises
`RuntimeError: Caught handled exception, but response already started` and the browser sees
an empty stream. The measured cost is ~5 silent full re-runs over ~25 seconds, five times the
model bill for one user action. So everything after `process()` has to come back as an
in-stream `{"type":"error"}` event on a 200, and the tests below drive real failures through
the real generator to prove it.

The technique is `tests/test_http_contract.py`'s: a `TestClient` over a stub pool, with the
router assembled under the middleware it runs under in production rather than imported from
`app.main` (which mounts the SPA at "/" and needs a full application to boot). The session
cookie is minted the way `tests/test_admin_api.py` mints it, so the REAL `current_user` runs
on every request here — `/api/config`'s "never 401" promise is not a promise if the thing
being swallowed is a stubbed dependency rather than the actual 401.

No Postgres, no network, no OpenAI: `ChatKitServer.process` is a stub that returns a scripted
`StreamingResult`, which is all `/chatkit` ever touches of it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from chatkit.server import NonStreamingResult, StreamingResult
from chatkit.store import NotFoundError
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Before anything reads them: `get_settings()` and `allowed_origins()` are both lru_cached,
# and `require_same_origin` compares the Origin header against PUBLIC_BASE_URL.
os.environ["SESSION_SECRET"] = "test-secret-not-used-anywhere-real"
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ["DATABASE_URL"] = "postgresql://unused:unused@127.0.0.1:5432/unused"

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402

import app.db as db  # noqa: E402
import app.runtime_settings as runtime_settings  # noqa: E402
from app.auth.deps import allowed_origins  # noqa: E402
from app.chat.routes import router  # noqa: E402
from app.context import RequestContext  # noqa: E402
from app.db import PoolNotInitialisedError  # noqa: E402
from app.settings import get_settings  # noqa: E402

get_settings.cache_clear()
allowed_origins.cache_clear()

ORIGIN = {"Origin": "http://testserver"}
SESSION_SECRET = "chat-routes-session-secret"
SESSION_COOKIE = "uktzed_session"  # the name app/main.py configures
USER_ID = 11


# --------------------------------------------------------------------------------------
# app/db.py — one pool, and the two calls that happen when there is not one
# --------------------------------------------------------------------------------------


class _RecordingPool:
    """A pool that counts its own closes."""

    def __init__(self) -> None:
        self.closes = 0

    async def close(self) -> None:
        self.closes += 1


def test_normalise_dsn_strips_a_sqlalchemy_driver() -> None:
    """`DATABASE_URL` is shared with alembic, which speaks `postgresql+asyncpg://`; asyncpg
    speaks libpq URLs only. Normalising here is what keeps that one variable one variable —
    two env vars that must be kept in sync is the configuration bug, not the fix."""
    assert db.normalise_dsn("postgresql+asyncpg://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
    assert db.normalise_dsn("postgres+psycopg://u:p@h/db") == "postgres://u:p@h/db"
    # Already libpq: untouched, byte for byte.
    assert db.normalise_dsn("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


def test_normalise_dsn_only_rewrites_the_scheme() -> None:
    """The pattern is anchored. A `+` anywhere else — a password, a query parameter — is part
    of the DSN and rewriting it would produce credentials that silently do not work."""
    dsn = "postgresql://user:pa+ss@host/db?application_name=uktzed+v2"

    assert db.normalise_dsn(dsn) == dsn


async def test_init_pool_normalises_the_dsn_and_returns_the_same_pool_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idempotent by contract: a second `init_pool` must not open a second pool. The bound
    that matters is `command_timeout` — v1 had none anywhere, so a wedged query held a
    connection until the driver's 600 s default."""
    created: list[tuple[str, dict[str, Any]]] = []
    pool = _RecordingPool()

    async def _create_pool(dsn: str, **kwargs: Any) -> _RecordingPool:
        created.append((dsn, kwargs))
        return pool

    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setattr(asyncpg, "create_pool", _create_pool)

    first = await db.init_pool("postgresql+asyncpg://u:p@h:5432/db")
    second = await db.init_pool("postgresql+asyncpg://somewhere-else/db")

    assert first is pool and second is pool
    assert [dsn for dsn, _ in created] == ["postgresql://u:p@h:5432/db"]
    assert created[0][1]["command_timeout"] == 30.0
    assert created[0][1]["max_size"] == 10
    assert db.get_pool() is pool


def test_get_pool_before_init_pool_raises_instead_of_returning_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wiring bug, said out loud where it happened. `None` here would surface as an
    `AttributeError` on `.fetchrow` several frames away, inside whichever request was first."""
    monkeypatch.setattr(db, "_pool", None)

    with pytest.raises(PoolNotInitialisedError) as raised:
        db.get_pool()

    assert isinstance(raised.value, RuntimeError)
    assert "init_pool" in str(raised.value), "the message has to name the missing call"


async def test_acquire_before_init_pool_raises_the_same_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`acquire()` goes through `get_pool()`, so the transaction path fails the same way
    rather than entering a context manager around nothing."""
    monkeypatch.setattr(db, "_pool", None)

    with pytest.raises(PoolNotInitialisedError):
        async with db.acquire():
            pytest.fail("acquire() must not yield when there is no pool")


async def test_acquire_hands_out_a_pooled_connection_and_gives_it_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`acquire()` exists for multi-statement work that must share a transaction. The release
    is the half that matters: v1 opened a connection at 15 call sites and pooled nothing, and
    a leak was one `except:` away."""
    released: list[bool] = []
    connection = object()

    class _Acquire:
        async def __aenter__(self) -> object:
            return connection

        async def __aexit__(self, *exc: Any) -> bool:
            released.append(True)
            return False

    class _Pool:
        def acquire(self) -> _Acquire:
            return _Acquire()

    monkeypatch.setattr(db, "_pool", _Pool())

    async with db.acquire() as borrowed:
        assert borrowed is connection

    assert released == [True]


async def test_close_pool_is_safe_to_call_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lifespan's `finally` can run after a startup that already failed, and a second
    `close()` on an asyncpg pool is an error. The global is forgotten on the first call, so
    the second is a no-op — and `get_pool()` afterwards is the wiring error again, not a
    closed pool handed out to a request."""
    pool = _RecordingPool()
    monkeypatch.setattr(db, "_pool", pool)

    await db.close_pool()
    await db.close_pool()

    assert pool.closes == 1
    with pytest.raises(PoolNotInitialisedError):
        db.get_pool()


async def test_close_pool_with_no_pool_at_all_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup died before `init_pool` returned; shutdown still runs."""
    monkeypatch.setattr(db, "_pool", None)

    await db.close_pool()


# --------------------------------------------------------------------------------------
# app/chat/routes.py — the fixtures
# --------------------------------------------------------------------------------------


class _StubPool:
    """The two queries the auth path and `/api/config` issue, and nothing else.

    Matched on SQL fragments rather than exact strings, so reformatting a query cannot turn
    a contract test green by returning None everywhere.
    """

    def __init__(self) -> None:
        self.settings: dict[str, str] = {}
        self.user: dict[str, Any] = {
            "id": USER_ID,
            "username": "anton",
            "display_name": "Антон",
            "is_active": True,
            "session_epoch": 0,
            "kind": "human",
            "is_superuser": False,
            "password_hash": "unused",
        }

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "FROM app_setting" in query:
            if "access_mode" not in self.settings:
                return None
            # asyncpg hands JSONB back as a str — no codec is installed (app/db.py) — so the
            # double quotes here are load-bearing for `_parse_value`.
            return {
                "key": "access_mode",
                "value": json.dumps(self.settings["access_mode"]),
                "updated_at": datetime.now(UTC),
                "updated_by": None,
            }
        if "FROM app_user" in query and "id = $1" in query:
            return dict(self.user) if args[0] == self.user["id"] else None
        return None

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        return []

    async def execute(self, query: str, *args: Any) -> str:
        return "UPDATE 1"


class _StubServer:
    """The `ChatKitServer` surface `/chatkit` touches: `process(body, context)`.

    Every call is recorded, which is how the tests below assert that auth and same-origin ran
    BEFORE the model did — the point of checking them where a real status code is still
    possible is that nothing downstream gets to spend money first.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[bytes, RequestContext]] = []
        self.outcome: Callable[[], Any] = lambda: NonStreamingResult(b"{}")

    async def process(self, payload: bytes, context: RequestContext) -> Any:
        self.calls.append((payload, context))
        return self.outcome()


def _stream(*chunks: bytes, then: BaseException | None = None) -> StreamingResult:
    """A scripted `StreamingResult`: some frames, then optionally a failure mid-stream."""

    async def generate() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk
        if then is not None:
            raise then

    return StreamingResult(generate())


class _Malformed(BaseModel):
    """Stands in for chatkit's tagged request union, whose `ValidationError` escapes."""

    op: str


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[_StubPool]:
    stub = _StubPool()
    # Every consumer — app/auth/deps.py and app/runtime_settings.py — reaches the pool
    # through this module-level global.
    monkeypatch.setattr(db, "_pool", stub)
    runtime_settings.invalidate_cache()
    yield stub
    runtime_settings.invalidate_cache()


@pytest.fixture
def server() -> _StubServer:
    return _StubServer()


@pytest.fixture
def client(pool: _StubPool, server: _StubServer) -> Iterator[TestClient]:
    """The router under the middleware it runs under in production.

    `SessionMiddleware` is not optional: `current_user` reads `request.session`, and without
    it every request is a 500 rather than a 401.
    """
    application = FastAPI()
    application.add_middleware(
        SessionMiddleware, secret_key=SESSION_SECRET, session_cookie=SESSION_COOKIE
    )
    application.include_router(router)
    application.state.server = server
    with TestClient(application, base_url="http://testserver") as test_client:
        yield test_client


def _sign_in(client: TestClient, user_id: int = USER_ID) -> None:
    """Become a user by minting the cookie `SessionMiddleware` itself would mint, so that the
    real `current_user` — signature check, row re-fetch, `is_active`, `session_epoch` — runs
    on every request instead of a dependency override."""
    payload = json.dumps({"uid": user_id, "ep": 0, "iat": int(time.time())})
    signed = TimestampSigner(SESSION_SECRET).sign(base64.b64encode(payload.encode("utf-8")))
    client.cookies.set(SESSION_COOKIE, signed.decode("utf-8"))


def _events(body: str) -> list[dict[str, Any]]:
    """Parse the SSE wire format back: `data: <json>\\n\\n`, and nothing else."""
    frames = [line for line in body.split("\n\n") if line.strip()]
    assert all(frame.startswith("data: ") for frame in frames), body
    return [json.loads(frame.removeprefix("data: ")) for frame in frames]


# --------------------------------------------------------------------------------------
# POST /chatkit — 200 text/event-stream, always
# --------------------------------------------------------------------------------------


def test_a_streaming_turn_is_a_200_event_stream(client: TestClient, server: _StubServer) -> None:
    """The media type is load-bearing: a 200 with any other content type is as
    indistinguishable from a failure, to the ChatKit client, as a 5xx is."""
    _sign_in(client)
    server.outcome = lambda: _stream(b'data: {"type":"thread.created"}\n\n')

    response = client.post("/chatkit", content=b'{"op":"threads.create"}', headers=ORIGIN)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert _events(response.text) == [{"type": "thread.created"}]
    # A proxy that buffers the stream turns a live drill-down back into v1's static string.
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"


def test_a_failure_mid_stream_is_an_error_event_and_never_a_5xx(
    client: TestClient, server: _StubServer, caplog: pytest.LogCaptureFixture
) -> None:
    """THE test for this module.

    The headers are already on the wire when the generator dies, so the status cannot change;
    Starlette would raise `Caught handled exception, but response already started` and the
    browser would get a truncated stream with nothing in it to show. A 5xx — or a truncated
    200 — costs about five silent client re-runs over ~25 seconds. An in-stream error event
    is retried zero times and shows the message verbatim.

    The frames emitted BEFORE the failure are kept: whatever the user already saw stays on
    the screen, with the error appended to it.
    """
    _sign_in(client)
    with caplog.at_level(logging.ERROR, logger="uktzed.chat"):
        server.outcome = lambda: _stream(
            b'data: {"type":"thread.item.added"}\n\n', then=RuntimeError("upstream exploded")
        )
        response = client.post("/chatkit", content=b"{}", headers=ORIGIN)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = _events(response.text)
    assert events[0] == {"type": "thread.item.added"}
    assert events[-1] == {
        "type": "error",
        "code": "custom",
        "message": "Внутрішня помилка. Спробуйте ще раз.",
        "allow_retry": True,  # classify_error() -> 'internal', which a retry can fix
    }
    # The last place anything can be logged at all, so it logs the traceback and the class.
    assert any("error_class=internal" in record.getMessage() for record in caplog.records)
    assert any(record.exc_info is not None for record in caplog.records)


def test_a_missing_thread_is_an_error_event_that_does_not_invite_a_retry(
    client: TestClient, server: _StubServer
) -> None:
    """`store.load_thread` runs INSIDE the SDK's generator, after the headers — so "thread
    not found" arrives on exactly the path that can no longer answer 404. Retrying it would
    fail identically, which is what `allow_retry=False` tells the client."""
    _sign_in(client)
    server.outcome = lambda: _stream(then=NotFoundError("thr_gone"))

    response = client.post("/chatkit", content=b"{}", headers=ORIGIN)

    assert response.status_code == 200
    assert _events(response.text) == [
        {
            "type": "error",
            "code": "custom",
            "message": "Розмову не знайдено.",
            "allow_retry": False,
        }
    ]


def test_a_malformed_body_is_a_400_before_any_streaming_starts(
    client: TestClient, server: _StubServer
) -> None:
    """A client error, answered as one. `process()` validates the tagged union with pydantic
    and lets `ValidationError` escape; unhandled it is a 500 that tells the caller nothing,
    and it happens before the headers, where a real status is still possible."""

    _sign_in(client)

    def _outcome() -> Any:
        # A real pydantic failure, raised where chatkit raises it — not a hand-built one.
        _Malformed.model_validate({})
        raise AssertionError("the fixture stopped producing a ValidationError")

    server.outcome = _outcome
    response = client.post("/chatkit", content=b"{}", headers=ORIGIN)

    assert response.status_code == 400
    assert response.json() == {"detail": "Malformed ChatKit request"}
    assert response.headers["content-type"].startswith("application/json")


def test_a_non_streaming_op_is_answered_as_json(client: TestClient, server: _StubServer) -> None:
    """Ten of ChatKit's operations do not stream. `NonStreamingResult.json` is BYTES, and
    passing it to `JSONResponse` would double-encode it into a quoted string."""
    _sign_in(client)
    server.outcome = lambda: NonStreamingResult(b'{"thread":{"id":"thr_1"}}')

    response = client.post("/chatkit", content=b"{}", headers=ORIGIN)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"thread": {"id": "thr_1"}}


def test_the_turn_context_carries_the_user_the_frame_id_and_the_locale(
    client: TestClient, server: _StubServer
) -> None:
    """`RequestContext.user_id` is the Store's entire authorization input, so it comes from
    the session and never from the body. The frame id is what ties a log line to one browser
    tab, and the locale is the first tag of the header ChatKit sends."""
    _sign_in(client)
    server.outcome = lambda: _stream()

    client.post(
        "/chatkit",
        content=b'{"op":"threads.create"}',
        headers={
            **ORIGIN,
            "chatkit-frame-instance-id": "frame-abc",
            "accept-language": "uk-UA,uk;q=0.9,en;q=0.8",
        },
    )

    payload, context = server.calls[0]
    assert payload == b'{"op":"threads.create"}'
    assert context.user_id == USER_ID
    assert context.request_id == "frame-abc"
    assert context.locale == "uk-UA"


def test_a_request_without_the_chatkit_headers_still_has_an_id_and_a_locale(
    client: TestClient, server: _StubServer
) -> None:
    """Neither header is guaranteed. A missing frame id must still produce a correlatable
    request id — v1's 17 opaque 500s could not be correlated with anything at all."""
    _sign_in(client)
    server.outcome = lambda: _stream()

    client.post("/chatkit", content=b"{}", headers=ORIGIN)

    _, context = server.calls[0]
    assert context.locale == "uk"
    assert uuid.UUID(context.request_id)  # a hex uuid, not an empty string


def test_an_anonymous_post_is_401_and_never_reaches_the_model(
    client: TestClient, server: _StubServer
) -> None:
    """Auth runs before `process()`, which is the only window in which a real status code is
    still possible — and the only window before the endpoint can spend money."""
    response = client.post("/chatkit", content=b"{}", headers=ORIGIN)

    assert response.status_code == 401
    assert server.calls == []


def test_a_cross_origin_post_is_403_and_never_reaches_the_model(
    client: TestClient, server: _StubServer
) -> None:
    """CSRF layer 2, checked in the same window. A browser always sends `Origin` on a POST,
    so its absence is a non-browser client."""
    _sign_in(client)

    missing = client.post("/chatkit", content=b"{}")
    foreign = client.post("/chatkit", content=b"{}", headers={"Origin": "http://evil.example"})

    assert (missing.status_code, foreign.status_code) == (403, 403)
    assert server.calls == []


# --------------------------------------------------------------------------------------
# GET /api/config — public, and the SPA's whole notion of what it is allowed to draw
# --------------------------------------------------------------------------------------


def test_config_never_401s_for_an_anonymous_visitor(client: TestClient) -> None:
    """In private mode `current_user` raises 401 and this route swallows it. It has to:
    /login reads this endpoint too, so a 401 here is a redirect loop on the page whose whole
    job is to get out of one."""
    response = client.get("/api/config")
    body = response.json()

    assert response.status_code == 200
    assert body["user"] is None
    assert body["access_mode"] == "private", "an empty app_setting fails closed"
    assert response.headers["cache-control"] == "no-store"


def test_config_carries_the_domain_key_in_both_spellings(client: TestClient) -> None:
    """The key is read at RUNTIME, not from `import.meta.env`: Vite inlines `VITE_`
    variables at build time, and the key is registered per hostname, so a baked-in one means
    rebuilding the bundle to change domains.

    Both spellings are emitted on purpose — `web/src/lib/config.ts` accepts either, and one
    duplicated public string is cheaper than a deploy-day mismatch that manifests as a
    self-deleting chat iframe with nothing in the server log.
    """
    body = client.get("/api/config").json()
    expected = get_settings().chatkit_domain_key

    assert expected, "a missing domain key unmounts the chat with no server-side signal"
    assert body["domain_key"] == expected
    assert body["domainKey"] == expected
    assert body["locale"] == "uk-UA"
    assert body["chatkit_url"] == "/chatkit", "one URL; the operation is in the POST body"


def test_config_reports_the_runtime_access_mode_not_the_env(
    client: TestClient, pool: _StubPool
) -> None:
    """The mode is read explicitly rather than inferred from `user`: a logged-in human
    short-circuits `current_user` without consulting it, and the SPA needs it in every case
    — it decides the «демо» pill, the «Гість» label and whether /login is a dead end."""
    _sign_in(client)
    pool.settings["access_mode"] = "public"

    body = client.get("/api/config").json()

    assert body["access_mode"] == "public"
    assert body["user"] == {
        "username": "anton",
        "display_name": "Антон",
        "kind": "human",
        "is_superuser": False,
    }

    # The kill switch lands on the very next request: no cache, no restart, no TTL.
    pool.settings["access_mode"] = "private"
    assert client.get("/api/config").json()["access_mode"] == "private"


def test_config_reports_the_session_so_a_login_does_not_land_back_on_login(
    client: TestClient,
) -> None:
    """`AppShell.tsx` redirects to /login whenever `user` is null, so this field is the
    difference between an application and a login page that cannot be left."""
    assert client.get("/api/config").json()["user"] is None

    _sign_in(client)

    assert client.get("/api/config").json()["user"]["username"] == "anton"
