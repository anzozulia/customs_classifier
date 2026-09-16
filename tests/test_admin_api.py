"""The hidden panel: the door, the rails, and the two numbers the author actually relies on.

This app is about to be demonstrated to a wide audience with no login and — by the author's
explicit decision — with no spend caps and no rate limits. That makes exactly two things
load-bearing, and this file exists for them:

* **The door.** Every `/api/admin/*` route must answer 404 to an anonymous caller, to an
  ordinary human and to a guest. 404 rather than 403, because a 403 confirms the path
  exists. `test_every_route_is_hidden` is parametrised over the router's OWN route table, so
  a route added later without the door fails this file rather than shipping.
* **The rails.** There is no shell behind this panel during a demo. A superuser who removes
  their own flag, or disables their own account, or demotes the last remaining admin, has
  locked themselves out of the switch that stops the spending — unrecoverably, short of
  `docker compose exec`. Each of those is a 409 here, and the last-superuser rail is
  additionally asserted against the SQL that enforces it.

Two techniques are borrowed from the existing suite:

* `tests/test_store_isolation.py` / `tests/test_history_api.py`'s **FakePool** — an
  asyncpg-shaped double over real SQLite that EXECUTES the routes' real SQL. That matters
  more here than anywhere: the last-superuser rail and the guest rail live INSIDE `UPDATE`
  statements (one statement, so two admins disabling each other concurrently cannot both
  win), and a dict double would only ever test the double.
* `tests/test_http_contract.py`'s **TestClient over a stub pool**. No Postgres, no network,
  and no OpenAI.

`app.runtime_settings` is exercised for real rather than mocked: its `app_setting` upsert
and its 3-second TTL cache both run against the same SQLite, which is what lets
`test_switching_to_private_writes_the_row_and_drops_the_cache` assert that the kill switch
takes effect immediately instead of asserting that a mock was called.

`tests/test_history_api.py`'s FakePool refuses any statement touching `classification`
without a `user_id` predicate. That guard is deliberately NOT copied here: the admin API is
the one place in this application that legitimately reads ACROSS users, its boundary is
`require_superuser` rather than `user_id`, and that boundary is what the first section
below asserts on every route.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Coroutine, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pytest

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
from app.admin.routes import _SET_ACTIVE, _SET_SUPERUSER, router  # noqa: E402
from app.agent.prompts import PROMPT_VERSION  # noqa: E402
from app.auth.deps import allowed_origins  # noqa: E402
from app.auth.guest import GUEST_PASSWORD_SENTINEL  # noqa: E402
from app.auth.passwords import verify_password  # noqa: E402
from app.records.pricing import MODEL_PRICES  # noqa: E402
from app.settings import get_settings  # noqa: E402

get_settings.cache_clear()
allowed_origins.cache_clear()

ORIGIN = {"Origin": "http://testserver"}
SESSION_SECRET = "admin-tests-session-secret"
SESSION_COOKIE = "uktzed_session"  # the name app/main.py configures
DATASET_SHA = "5a113fc09ac05028afbd6ed1884a95133b65e7827e61a87d74d5ffb11a38dee3"

# A password long enough for the routes' own min_length, reused everywhere.
PASSWORD = "correct-horse-battery"

ROOT_ID = 1  # the acting superuser in every test below
SPARE_ID = 2  # a second superuser, so the last-superuser rail is not always in the way
NORMAL_ID = 3  # an ordinary human: logs in, sees no panel
GUEST_A_ID = 4
GUEST_B_ID = 5
DISABLED_ID = 6  # a superuser whose row is is_active = false

# Mirrors migrations 0001 + 0004 + 0005, with PG types swapped for SQLite ones. CITEXT
# becomes `COLLATE NOCASE`, which is what makes the duplicate-username test meaningful;
# JSONB becomes TEXT, which is what asyncpg hands back for it anyway (see app/db.py).
_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE app_user (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT    NOT NULL,
    display_name  TEXT    NOT NULL DEFAULT '',
    is_active     INTEGER NOT NULL DEFAULT 1,
    is_reviewer   INTEGER NOT NULL DEFAULT 0,
    session_epoch INTEGER NOT NULL DEFAULT 0,
    -- `server_default=sa.func.now()` in 0001; the INSERT in POST /users relies on it.
    created_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TEXT,
    kind          TEXT    NOT NULL DEFAULT 'human' CHECK (kind IN ('human', 'guest')),
    is_superuser  INTEGER NOT NULL DEFAULT 0,
    last_seen_at  TEXT
);

CREATE TABLE classification (
    id             TEXT    PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    thread_id      TEXT,
    created_at     TEXT    NOT NULL,
    finished_at    TEXT,
    input_text     TEXT    NOT NULL,
    outcome        TEXT    NOT NULL,
    error_class    TEXT,
    duration_ms    INTEGER,
    model          TEXT    NOT NULL,
    prompt_version TEXT    NOT NULL,
    prompt_sha256  TEXT    NOT NULL,
    dataset_sha256 TEXT,
    tokens_in      INTEGER,
    tokens_cached  INTEGER,
    tokens_out     INTEGER,
    cost_usd       NUMERIC
);

CREATE TABLE app_setting (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by INTEGER REFERENCES app_user(id) ON DELETE SET NULL
);
"""

_PARAM = re.compile(r"\$(\d+)")
_CAST = re.compile(r"::\w+")


def _translate(sql: str, args: tuple[Any, ...]) -> tuple[str, list[Any]]:
    """asyncpg dialect → sqlite3 dialect.

    `$n` repeats and appears out of order (the users list uses `$1` twice; the guarded
    UPDATEs use `$1` and `$2` three times each), so parameters are collected in the order
    the placeholders appear rather than by index. `now()` is Postgres spelling for what
    SQLite calls CURRENT_TIMESTAMP — `app/runtime_settings.set_runtime` is the statement
    that needs it.
    """
    collected: list[Any] = []

    def _sub(match: re.Match[str]) -> str:
        value = args[int(match.group(1)) - 1]
        # sqlite3 has no datetime adapter since 3.12; the column is ISO TEXT either way, and
        # a fixed-format ISO string orders exactly like a timestamptz.
        collected.append(value.isoformat() if isinstance(value, datetime) else value)
        return "?"

    translated = _CAST.sub("", _PARAM.sub(_sub, sql)).replace("now()", "CURRENT_TIMESTAMP")
    return translated, collected


def _row(row: sqlite3.Row) -> dict[str, Any]:
    """asyncpg hands back typed values; SQLite hands back the TEXT it stored."""
    out = dict(row)
    for key, value in out.items():
        if key.endswith("_at") and isinstance(value, str):
            out[key] = datetime.fromisoformat(value)
    return out


class FakePool:
    """The asyncpg surface the admin routes, `require_superuser` and `runtime_settings` use."""

    def __init__(self) -> None:
        # check_same_thread=False: TestClient runs the ASGI app on a worker thread while the
        # test seeds from the main one. Access is still strictly sequential — the portal
        # blocks the test thread for the duration of each request.
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def _run(self, sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        self.statements.append((sql, args))
        translated, params = _translate(sql, args)
        try:
            cursor = self._conn.execute(translated, params)
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc):
                # The routes catch asyncpg's class, not sqlite3's. Translating the dialect's
                # exception is the same job as translating its placeholders.
                raise asyncpg.UniqueViolationError(str(exc)) from exc
            raise
        rows = cursor.fetchall()  # drain before commit: RETURNING leaves the cursor open
        self._conn.commit()
        return [_row(row) for row in rows]

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return self._run(sql, args)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        rows = self._run(sql, args)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        rows = self._run(sql, args)
        return next(iter(rows[0].values())) if rows else None

    async def execute(self, sql: str, *args: Any) -> str:
        self._run(sql, args)
        return "OK"

    # -- test-side helpers, straight to the connection ------------------------------------

    def query(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        return [_row(row) for row in self._conn.execute(sql, params).fetchall()]

    def seed_user(
        self,
        user_id: int,
        username: str,
        *,
        kind: str = "human",
        is_active: bool = True,
        is_superuser: bool = False,
        display_name: str | None = None,
        created_at: datetime | None = None,
        last_seen_at: datetime | None = None,
        last_login_at: datetime | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO app_user (id, username, password_hash, display_name, is_active,"
            " created_at, last_login_at, kind, is_superuser, last_seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                username,
                GUEST_PASSWORD_SENTINEL if kind == "guest" else f"$argon2-not-checked-{user_id}",
                display_name or username,
                int(is_active),
                (created_at or datetime(2026, 1, 1, tzinfo=UTC)).isoformat(),
                last_login_at.isoformat() if last_login_at else None,
                kind,
                int(is_superuser),
                last_seen_at.isoformat() if last_seen_at else None,
            ),
        )
        self._conn.commit()

    def seed_classification(
        self,
        entry_id: str,
        *,
        user_id: int,
        created_at: datetime,
        outcome: str = "classified",
        tokens_in: int = 1000,
        tokens_out: int = 200,
        cost_usd: float | None = 0.01,
    ) -> None:
        self._conn.execute(
            "INSERT INTO classification (id, user_id, created_at, input_text, outcome,"
            " model, prompt_version, prompt_sha256, tokens_in, tokens_out, cost_usd)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry_id,
                user_id,
                created_at.isoformat(),
                "чайник електричний",
                outcome,
                "gpt-5.6-terra",
                PROMPT_VERSION,
                "0" * 64,
                tokens_in,
                tokens_out,
                cost_usd,
            ),
        )
        self._conn.commit()


class _StubServer:
    """The two cached values `_live_prompt` reads off `app.state.server`.

    The real object is `app/chat/server.py::ClassifierServer`, built in the app lifespan; it
    memoises the section catalogue and the active dataset for the life of the process, which
    is why the admin overview can report the live prompt digest without a query.
    """

    async def section_catalogue(self) -> str:
        return "Розділ 01 (групи 01–05): Живі тварини"

    async def dataset_sha256(self) -> str:
        return DATASET_SHA


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Await one coroutine from a synchronous test.

    The tests are synchronous because `TestClient` drives the ASGI app through its own
    portal thread; this is for the handful of direct `runtime_settings` calls that assert
    what the panel's write actually did to the database and to the process cache.
    """
    return asyncio.run(coro)


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakePool]:
    fake = FakePool()
    # Every consumer — the routes, app/auth/deps.py and app/runtime_settings.py — reaches
    # the pool through this module-level global.
    monkeypatch.setattr(db, "_pool", fake)

    # The TTL cache is a module global with a 3-second life, and these tests run in
    # milliseconds; without this a setting written by one test would leak into the next.
    runtime_settings.invalidate_cache()

    fake.seed_user(ROOT_ID, "anton", is_superuser=True, display_name="Антон")
    fake.seed_user(SPARE_ID, "spare", is_superuser=True)
    fake.seed_user(NORMAL_ID, "olena", display_name="Олена")
    fake.seed_user(
        GUEST_A_ID,
        "guest_aaaaaaaa",
        kind="guest",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    fake.seed_user(
        GUEST_B_ID,
        "guest_bbbbbbbb",
        kind="guest",
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 9, 10, tzinfo=UTC),
    )
    fake.seed_user(DISABLED_ID, "ghost", is_superuser=True, is_active=False)

    yield fake
    runtime_settings.invalidate_cache()


@pytest.fixture
def client(pool: FakePool) -> Iterator[TestClient]:
    """The router under the middleware it runs under in production.

    Assembled here rather than imported from `app.main`: that module mounts the SPA at "/"
    last and Starlette matches in registration order. `SessionMiddleware` is not optional —
    `require_superuser` reads `request.session`, and without it every request is a 500.
    """
    application = FastAPI()
    application.add_middleware(
        SessionMiddleware, secret_key=SESSION_SECRET, session_cookie=SESSION_COOKIE
    )
    application.include_router(router)
    application.state.server = _StubServer()
    with TestClient(application, base_url="http://testserver") as test_client:
        yield test_client


def _as(client: TestClient, user_id: int, *, epoch: int = 0) -> None:
    """Become a user by minting the cookie `SessionMiddleware` itself would mint.

    Deliberately NOT a `dependency_overrides[require_superuser]`: the door is the thing
    under test, so the real dependency has to run on every request here — signature check,
    row re-fetch, `is_active`, `session_epoch`, `kind` and `is_superuser` included.
    """
    payload = json.dumps({"uid": user_id, "ep": epoch, "iat": int(time.time())})
    signed = TimestampSigner(SESSION_SECRET).sign(base64.b64encode(payload.encode("utf-8")))
    client.cookies.set(SESSION_COOKIE, signed.decode("utf-8"))


def _anonymous(client: TestClient) -> None:
    client.cookies.clear()


# --------------------------------------------------------------------------------------
# the door
# --------------------------------------------------------------------------------------

#: (method, path, body). One entry per route; `test_the_route_table_is_covered` proves this
#: list is the router's whole surface, so a new route cannot skip the section below.
ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/api/admin/overview", None),
    ("PUT", "/api/admin/settings", {"key": "access_mode", "value": "public"}),
    ("GET", "/api/admin/users", None),
    ("POST", "/api/admin/users", {"username": "intruder", "password": PASSWORD}),
    ("POST", f"/api/admin/users/{NORMAL_ID}/password", {"password": PASSWORD}),
    ("POST", f"/api/admin/users/{NORMAL_ID}/active", {"is_active": False}),
    ("POST", f"/api/admin/users/{NORMAL_ID}/superuser", {"is_superuser": True}),
    ("GET", "/api/admin/models", None),
]

MUTATING = [route for route in ROUTES if route[0] != "GET"]

_ID_SEGMENT = re.compile(r"/users/\d+/")


def _call(client: TestClient, method: str, path: str, body: dict[str, Any] | None) -> Any:
    return client.request(method, path, json=body, headers=ORIGIN)


def _put_setting(client: TestClient, key: str, value: str) -> Any:
    return client.put("/api/admin/settings", json={"key": key, "value": value}, headers=ORIGIN)


def test_the_route_table_is_covered() -> None:
    """ROUTES is the router's whole surface, so "every route" below means every route."""
    declared = {
        (method, route.path)
        for route in router.routes
        for method in getattr(route, "methods", set())
    }
    covered = {(method, _ID_SEGMENT.sub("/users/{user_id}/", path)) for method, path, _ in ROUTES}
    assert declared == covered


@pytest.mark.parametrize(
    ("who", "user_id"),
    [
        ("anonymous", None),
        ("an ordinary human", NORMAL_ID),
        ("a guest", GUEST_A_ID),
        ("a deactivated superuser", DISABLED_ID),
    ],
)
@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=lambda v: str(v)[:40])
def test_every_route_is_hidden(
    client: TestClient,
    pool: FakePool,
    who: str,
    user_id: int | None,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """404, never 403 and never 401.

    A 403 says "this path exists and you may not have it", which is exactly the sentence a
    hidden panel must not say. The body matches the one `app/main.py` already returns for a
    Store NotFoundError, so the two are identical on the wire as well as in status.
    """
    if user_id is None:
        _anonymous(client)
    else:
        _as(client, user_id)

    response = _call(client, method, path, body)

    assert response.status_code == 404, f"{who} got {response.status_code} from {method} {path}"
    assert response.json() == {"detail": "Not found"}


def test_probing_the_panel_changes_nothing(client: TestClient, pool: FakePool) -> None:
    """The door runs before anything else, so a probe neither writes nor mints.

    `require_superuser` resolves the session WITHOUT the guest-minting side effect that
    `current_user` has, precisely so that an anonymous probe of a hidden URL cannot be used
    to fill `app_user`.
    """
    before = pool.query("SELECT count(*) AS n FROM app_user")[0]["n"]

    for method, path, body in ROUTES:
        _anonymous(client)
        assert _call(client, method, path, body).status_code == 404
        _as(client, GUEST_A_ID)
        assert _call(client, method, path, body).status_code == 404

    assert pool.query("SELECT count(*) AS n FROM app_user")[0]["n"] == before
    assert pool.query("SELECT count(*) AS n FROM app_setting")[0]["n"] == 0
    assert pool.query("SELECT username FROM app_user WHERE username = 'intruder'") == []


def test_a_revoked_session_epoch_closes_the_door(client: TestClient, pool: FakePool) -> None:
    """The cookie is signed, not encrypted, and it is revocable: `require_superuser`
    re-reads the row on every request, so a password change (which bumps `session_epoch`)
    shuts the panel on the very next click."""
    _as(client, ROOT_ID, epoch=0)
    assert client.get("/api/admin/overview").status_code == 200

    pool.query("UPDATE app_user SET session_epoch = 1 WHERE id = ?", ROOT_ID)
    assert client.get("/api/admin/overview").status_code == 404


def test_a_superuser_gets_through_every_route(client: TestClient) -> None:
    """The mirror image: the 404s above are the door, not a broken router."""
    _as(client, ROOT_ID)
    for method, path, body in ROUTES:
        response = _call(client, method, path, body)
        assert response.status_code < 400, f"{method} {path} -> {response.status_code}"


@pytest.mark.parametrize(("method", "path", "body"), MUTATING, ids=lambda v: str(v)[:40])
def test_mutating_routes_require_the_same_origin_header(
    client: TestClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    """CSRF layer 2, declared once on the router so no route can forget it.

    `require_same_origin` returns immediately for GET/HEAD/OPTIONS, which is why the read
    routes are not in this list.
    """
    _as(client, ROOT_ID)
    assert client.request(method, path, json=body).status_code == 403


# --------------------------------------------------------------------------------------
# GET /overview — the author's only spend visibility
# --------------------------------------------------------------------------------------

OVERVIEW_KEYS = {
    "access_mode",
    "model",
    "reasoning_effort",
    "prompt_version",
    "prompt_sha256",
    "dataset_sha256",
    "usage",
}
WINDOW_KEYS = {
    "since",
    "classifications",
    "cost_usd",
    "runs_unpriced",
    "tokens_in",
    "tokens_cached",
    "tokens_out",
    "outcomes",
    "users",
    "human_users",
    "guest_users",
    "human_classifications",
    "guest_classifications",
}
OUTCOME_KEYS = {"classified", "clarification", "error", "pending"}


def test_the_overview_key_set_is_stable(client: TestClient) -> None:
    """The panel is written against this shape; a rename here is a blank dashboard there."""
    _as(client, ROOT_ID)
    body = client.get("/api/admin/overview").json()

    assert set(body) == OVERVIEW_KEYS
    assert set(body["usage"]) == {"today", "last_7d", "all_time"}
    for name, window in body["usage"].items():
        assert set(window) == WINDOW_KEYS, name
        assert set(window["outcomes"]) == OUTCOME_KEYS, name


def test_the_overview_reports_the_live_prompt_and_a_closed_default_mode(
    client: TestClient,
) -> None:
    """`access_mode` on an empty `app_setting` is 'private'.

    That is `app/runtime_settings.py` failing closed, and it is the right direction: the
    failure mode of the other one is "the demo is open to the internet and nobody noticed".
    """
    _as(client, ROOT_ID)
    body = client.get("/api/admin/overview").json()

    assert body["access_mode"] == "private"
    assert body["model"] == get_settings().model
    assert body["reasoning_effort"] == get_settings().reasoning_effort
    # Read from app/agent/prompts, never a literal: v1 shipped a banner naming a model and a
    # prompt it did not run, and its logs lied for two months.
    assert body["prompt_version"] == PROMPT_VERSION
    assert re.fullmatch(r"[0-9a-f]{64}", body["prompt_sha256"])
    assert body["dataset_sha256"] == DATASET_SHA


def test_the_overview_survives_a_missing_tariff(client: TestClient) -> None:
    """The usage dashboard is why the author opens this page. A tariff that is mid-ingest
    must not take it down — `/readyz` is where that is supposed to be loud."""
    client.app.state.server = None  # type: ignore[union-attr]
    _as(client, ROOT_ID)
    body = client.get("/api/admin/overview").json()

    assert body["prompt_sha256"] is None
    assert body["dataset_sha256"] is None
    assert body["usage"]["all_time"]["classifications"] == 0


def test_usage_aggregates_windows_outcomes_money_and_the_guest_split(
    client: TestClient, pool: FakePool
) -> None:
    """The numbers themselves, computed by SQL over the register.

    The register is the right source precisely because it records FAILED turns too: a cost
    estimate built from successes would quietly under-report exactly the population that
    burns money without producing an answer.

    Window boundaries: `last_7d` and `all_time` are relative to "now" and so are asserted
    exactly. `today` is 00:00 UTC, which a run straddling midnight would move under the
    test's feet, so its expectation is derived from the boundary the route itself reports —
    which also asserts that `since` is that boundary.
    """
    now = datetime.now(UTC)
    seeds = [
        # (id, user, age, outcome, tokens_in, tokens_out, cost)
        ("cls_1", ROOT_ID, timedelta(minutes=10), "classified", 1000, 200, 0.01),
        ("cls_2", GUEST_A_ID, timedelta(hours=1), "conversation", 500, 100, 0.005),
        ("cls_3", GUEST_A_ID, timedelta(days=1, hours=1), "clarification", 300, 50, 0.002),
        ("cls_4", GUEST_B_ID, timedelta(days=3), "error", 100, 0, None),
        ("cls_5", GUEST_B_ID, timedelta(days=3), "pending", 0, 0, None),
        ("cls_6", NORMAL_ID, timedelta(days=30), "classified", 900, 90, 0.5),
    ]
    for entry_id, user_id, age, outcome, t_in, t_out, cost in seeds:
        pool.seed_classification(
            entry_id,
            user_id=user_id,
            created_at=now - age,
            outcome=outcome,
            tokens_in=t_in,
            tokens_out=t_out,
            cost_usd=cost,
        )

    _as(client, ROOT_ID)
    usage = client.get("/api/admin/overview").json()["usage"]

    all_time = usage["all_time"]
    assert all_time["since"] is None
    assert all_time["classifications"] == 6
    assert all_time["cost_usd"] == pytest.approx(0.517)
    assert all_time["tokens_in"] == 2800
    assert all_time["tokens_out"] == 440
    # 'conversation' is stored losslessly and collapsed into 'classified' at the HTTP
    # boundary, exactly as app/records/routes.py does for a user's own history.
    assert all_time["outcomes"] == {
        "classified": 3,
        "clarification": 1,
        "error": 1,
        "pending": 1,
    }
    assert all_time["users"] == 4
    assert all_time["human_users"] == 2
    assert all_time["guest_users"] == 2
    assert all_time["human_classifications"] == 2
    assert all_time["guest_classifications"] == 4

    week = usage["last_7d"]
    assert week["classifications"] == 5  # cls_6 is 30 days old
    assert week["cost_usd"] == pytest.approx(0.017)
    assert week["users"] == 3
    assert week["human_users"] == 1
    assert week["guest_users"] == 2
    assert week["outcomes"]["classified"] == 2

    today = usage["today"]
    since = datetime.fromisoformat(today["since"])
    assert (since.hour, since.minute, since.second, since.microsecond) == (0, 0, 0, 0)
    assert since <= now
    in_window = [s for s in seeds if now - s[2] >= since]
    assert today["classifications"] == len(in_window)
    assert today["users"] == len({s[1] for s in in_window})
    # "Distinct users active today" is this number, and it is the one the author reads to
    # know how wide the demo actually went.
    assert today["human_users"] + today["guest_users"] == today["users"]


def test_cost_is_a_float_with_six_decimals(client: TestClient, pool: FakePool) -> None:
    """Money on the wire matches `/api/history`: a JSON number, at NUMERIC(10,6) precision.

    A row whose model is not in `app/records/pricing.py` stores `cost_usd = NULL` and
    contributes nothing rather than a guess — the same rule the history API applies.
    """
    now = datetime.now(UTC)
    pool.seed_classification("cls_a", user_id=ROOT_ID, created_at=now, cost_usd=0.000001)
    pool.seed_classification("cls_b", user_id=ROOT_ID, created_at=now, cost_usd=0.000002)
    pool.seed_classification("cls_c", user_id=ROOT_ID, created_at=now, cost_usd=None)

    _as(client, ROOT_ID)
    all_time = client.get("/api/admin/overview").json()["usage"]["all_time"]

    assert isinstance(all_time["cost_usd"], float)
    assert all_time["cost_usd"] == 0.000003
    assert all_time["classifications"] == 3
    # …and the NULL is COUNTED, not just skipped. Without this number the total above reads
    # as "what the demo cost" when it is really "what the demo cost, minus one turn".
    assert all_time["runs_unpriced"] == 1


# --------------------------------------------------------------------------------------
# PUT /settings — the kill switch
# --------------------------------------------------------------------------------------


def test_switching_to_private_writes_the_row_and_drops_the_cache(
    client: TestClient, pool: FakePool
) -> None:
    """The switch is the only lever this deployment has, so it has to be instant.

    `runtime_settings` serves `model` and `reasoning_effort` from a 3-second TTL cache. This
    test WARMS that cache with 'public' first and then reads back through the cached
    accessor a few milliseconds later: without `invalidate_cache()` inside `set_runtime`,
    the second read would still say 'public' and this assertion would fail.

    Guest enforcement itself lives in `app/auth/deps.py::current_user` and is tested with
    the identity layer; what is asserted here is that the value it reads has already
    changed by the time this response is written.
    """
    _as(client, ROOT_ID)

    assert _put_setting(client, "access_mode", "public").status_code == 200
    assert _run(runtime_settings.get_runtime("access_mode")) == "public"  # warms the cache

    response = _put_setting(client, "access_mode", "private")

    assert response.status_code == 200
    # The response is read back out of the database, not echoed from the request.
    assert response.json() == {
        "key": "access_mode",
        "value": "private",
        "source": "db",
        "updated_at": response.json()["updated_at"],
        "updated_by": ROOT_ID,
    }
    assert response.json()["updated_at"] is not None

    # Written…
    rows = pool.query("SELECT key, value, updated_by FROM app_setting")
    assert rows == [{"key": "access_mode", "value": '"private"', "updated_by": ROOT_ID}]
    # …and the cache is gone, so both accessors already agree.
    assert _run(runtime_settings.get_runtime("access_mode")) == "private"
    assert _run(runtime_settings.get_access_mode()) == "private"
    assert client.get("/api/admin/overview").json()["access_mode"] == "private"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("access_mode", "maybe"),  # not in the closed vocabulary
        ("access_mode", ""),  # empty
        ("reasoning_effort", "ultra"),
        ("model", "gpt-5.6-terra; DROP TABLE app_user"),
        ("spend_cap_usd", "50"),  # there are no caps: the key does not exist
        ("is_superuser", "true"),  # not a runtime setting, and never will be
    ],
)
def test_put_settings_rejects_a_bad_value(
    client: TestClient, pool: FakePool, key: str, value: str
) -> None:
    """Validation belongs to `set_runtime`, which the read path also uses — one definition
    of "legal", not two. A rejection is a 400 carrying that module's message."""
    _as(client, ROOT_ID)
    response = _put_setting(client, key, value)

    assert response.status_code == 400
    assert key in response.json()["detail"] or "unknown" in response.json()["detail"]
    assert pool.query("SELECT * FROM app_setting") == []


def test_model_and_reasoning_effort_are_settable(client: TestClient) -> None:
    _as(client, ROOT_ID)
    assert _put_setting(client, "model", "gpt-5.6-luna").status_code == 200
    assert _put_setting(client, "reasoning_effort", "high").status_code == 200

    body = client.get("/api/admin/overview").json()
    assert body["model"] == "gpt-5.6-luna"
    assert body["reasoning_effort"] == "high"


# --------------------------------------------------------------------------------------
# GET /users
# --------------------------------------------------------------------------------------

USER_KEYS = {
    "id",
    "username",
    "display_name",
    "kind",
    "is_active",
    "is_superuser",
    "created_at",
    "last_login_at",
    "last_seen_at",
    "classifications",
}


def test_users_lists_humans_first_then_guests_by_recency(
    client: TestClient, pool: FakePool
) -> None:
    """Guests will outnumber humans by whatever the demo's traffic turns out to be, so the
    handful of real accounts must not be buried under them."""
    _as(client, ROOT_ID)
    rows = client.get("/api/admin/users").json()

    assert set(rows[0]) == USER_KEYS
    assert [r["id"] for r in rows] == [
        ROOT_ID,
        SPARE_ID,
        NORMAL_ID,
        DISABLED_ID,
        GUEST_B_ID,  # last seen 2026-09-10
        GUEST_A_ID,  # last seen 2026-09-01
    ]
    assert [r["kind"] for r in rows[:4]] == ["human"] * 4
    assert rows[0]["display_name"] == "Антон"
    assert rows[0]["is_superuser"] is True
    assert rows[3]["is_active"] is False


def test_users_can_be_filtered_and_limited(client: TestClient, pool: FakePool) -> None:
    _as(client, ROOT_ID)

    guests = client.get("/api/admin/users", params={"kind": "guest"}).json()
    assert [r["id"] for r in guests] == [GUEST_B_ID, GUEST_A_ID]

    humans = client.get("/api/admin/users", params={"kind": "human"}).json()
    assert {r["kind"] for r in humans} == {"human"}
    assert len(humans) == 4

    assert len(client.get("/api/admin/users", params={"limit": 2}).json()) == 2
    assert client.get("/api/admin/users", params={"kind": "robot"}).status_code == 422
    assert client.get("/api/admin/users", params={"limit": 0}).status_code == 422


def test_users_carry_their_classification_count(client: TestClient, pool: FakePool) -> None:
    now = datetime.now(UTC)
    pool.seed_classification("cls_1", user_id=GUEST_A_ID, created_at=now)
    pool.seed_classification("cls_2", user_id=GUEST_A_ID, created_at=now)

    _as(client, ROOT_ID)
    by_id = {r["id"]: r for r in client.get("/api/admin/users").json()}

    assert by_id[GUEST_A_ID]["classifications"] == 2
    assert by_id[GUEST_B_ID]["classifications"] == 0
    assert by_id[ROOT_ID]["classifications"] == 0


# --------------------------------------------------------------------------------------
# POST /users — creating a human
# --------------------------------------------------------------------------------------


def test_creating_a_user_hashes_the_password_with_argon2(
    client: TestClient, pool: FakePool
) -> None:
    _as(client, ROOT_ID)
    response = client.post(
        "/api/admin/users",
        json={"username": "  nova  ", "password": PASSWORD, "display_name": "Нова"},
        headers=ORIGIN,
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == USER_KEYS
    assert body["username"] == "nova"  # stripped
    assert body["display_name"] == "Нова"
    assert body["kind"] == "human"
    assert body["is_active"] is True
    assert body["is_superuser"] is False
    assert body["classifications"] == 0

    stored = pool.query("SELECT password_hash FROM app_user WHERE username = 'nova'")[0]
    assert stored["password_hash"].startswith("$argon2")
    assert verify_password(stored["password_hash"], PASSWORD)


def test_creating_a_user_can_grant_the_panel_and_defaults_the_display_name(
    client: TestClient,
) -> None:
    _as(client, ROOT_ID)
    body = client.post(
        "/api/admin/users",
        json={"username": "second", "password": PASSWORD, "is_superuser": True},
        headers=ORIGIN,
    ).json()

    assert body["is_superuser"] is True
    assert body["display_name"] == "second"


def test_a_duplicate_username_is_a_409_case_insensitively(client: TestClient) -> None:
    """`username` is CITEXT, so the collision the database reports is case-insensitive."""
    _as(client, ROOT_ID)
    response = client.post(
        "/api/admin/users",
        json={"username": "ANTON", "password": PASSWORD},
        headers=ORIGIN,
    )

    assert response.status_code == 409
    assert "уже існує" in response.json()["detail"]


def test_a_short_password_is_refused(client: TestClient) -> None:
    _as(client, ROOT_ID)
    response = client.post(
        "/api/admin/users", json={"username": "tiny", "password": "short"}, headers=ORIGIN
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------------------
# the safety rails
# --------------------------------------------------------------------------------------


def test_a_superuser_cannot_remove_their_own_flag(client: TestClient, pool: FakePool) -> None:
    """The unrecoverable one. There is no shell behind this panel during a demo: an admin
    who drops their own flag cannot grant it back, because granting it is this route."""
    _as(client, ROOT_ID)
    response = client.post(
        f"/api/admin/users/{ROOT_ID}/superuser", json={"is_superuser": False}, headers=ORIGIN
    )

    assert response.status_code == 409
    assert "суперкористувача" in response.json()["detail"]
    assert pool.query("SELECT is_superuser FROM app_user WHERE id = ?", ROOT_ID)[0]["is_superuser"]
    # …and the panel is still open.
    assert client.get("/api/admin/overview").status_code == 200


def test_a_superuser_cannot_deactivate_themselves(client: TestClient, pool: FakePool) -> None:
    _as(client, ROOT_ID)
    response = client.post(
        f"/api/admin/users/{ROOT_ID}/active", json={"is_active": False}, headers=ORIGIN
    )

    assert response.status_code == 409
    assert "власний" in response.json()["detail"]
    assert pool.query("SELECT is_active FROM app_user WHERE id = ?", ROOT_ID)[0]["is_active"]
    assert client.get("/api/admin/overview").status_code == 200


def test_a_guest_can_never_be_made_a_superuser(client: TestClient, pool: FakePool) -> None:
    _as(client, ROOT_ID)
    response = client.post(
        f"/api/admin/users/{GUEST_A_ID}/superuser", json={"is_superuser": True}, headers=ORIGIN
    )

    assert response.status_code == 409
    assert "Гостьовий" in response.json()["detail"]
    row = pool.query("SELECT is_superuser FROM app_user WHERE id = ?", GUEST_A_ID)[0]
    assert not row["is_superuser"]


def test_a_guest_can_never_be_given_a_password(client: TestClient, pool: FakePool) -> None:
    """A guest's `password_hash` is a deliberately invalid sentinel, which is what makes
    "a guest can never log in" a property of the row rather than a rule to remember.
    Writing a real argon2 hash there would quietly convert a guest into an account."""
    _as(client, ROOT_ID)
    response = client.post(
        f"/api/admin/users/{GUEST_A_ID}/password", json={"password": PASSWORD}, headers=ORIGIN
    )

    assert response.status_code == 409
    assert "Гостьовий" in response.json()["detail"]
    stored = pool.query("SELECT password_hash FROM app_user WHERE id = ?", GUEST_A_ID)[0]
    assert stored["password_hash"] == GUEST_PASSWORD_SENTINEL


def test_the_last_active_superuser_cannot_be_demoted_or_disabled(pool: FakePool) -> None:
    """The rail asserted against the SQL that enforces it, because that is where it lives.

    Over HTTP this case is reached only through the two self-rails above — the actor IS an
    active superuser, so any OTHER target always has them as a spare. The guard is kept
    anyway, and is a single `UPDATE` rather than a `SELECT count(*)` then an `UPDATE`,
    because the race it exists for is two superusers demoting each other at the same moment:
    both would read "there is another one", both would write, and the demo would end with
    nobody able to reach the panel. Here the statements are driven directly, which is the
    only way to put the database into that state.
    """
    pool.query("UPDATE app_user SET is_superuser = 0 WHERE id = ?", SPARE_ID)
    # ROOT is now the only active superuser ('ghost' holds the flag but is_active = 0).

    assert _run(pool.fetchval(_SET_SUPERUSER, ROOT_ID, False)) is None
    assert _run(pool.fetchval(_SET_ACTIVE, ROOT_ID, False)) is None
    survivor = pool.query("SELECT is_superuser, is_active FROM app_user WHERE id = ?", ROOT_ID)[0]
    assert survivor["is_superuser"] and survivor["is_active"]

    # Give the flag back to the spare, and the same two statements now succeed.
    pool.query("UPDATE app_user SET is_superuser = 1 WHERE id = ?", SPARE_ID)
    assert _run(pool.fetchval(_SET_SUPERUSER, ROOT_ID, False)) == ROOT_ID
    assert _run(pool.fetchval(_SET_ACTIVE, ROOT_ID, False)) == ROOT_ID


def test_a_second_superuser_can_be_demoted_and_disabled(client: TestClient, pool: FakePool) -> None:
    """The mirror image: the rails are the invariant, not "nothing can ever be changed"."""
    _as(client, ROOT_ID)

    demoted = client.post(
        f"/api/admin/users/{SPARE_ID}/superuser", json={"is_superuser": False}, headers=ORIGIN
    )
    assert demoted.status_code == 200
    assert demoted.json()["is_superuser"] is False

    disabled = client.post(
        f"/api/admin/users/{SPARE_ID}/active", json={"is_active": False}, headers=ORIGIN
    )
    assert disabled.status_code == 200
    assert disabled.json()["is_active"] is False


def test_disabling_revokes_sessions_and_re_enabling_does_not(
    client: TestClient, pool: FakePool
) -> None:
    """`session_epoch + 1` is what turns "disabled" from advisory into enforced. Re-enabling
    deliberately does not bump it: that would log a user out of a session nobody ended."""
    _as(client, ROOT_ID)

    def epoch(user_id: int) -> int:
        return pool.query("SELECT session_epoch FROM app_user WHERE id = ?", user_id)[0][
            "session_epoch"
        ]

    before = epoch(GUEST_A_ID)
    off = client.post(
        f"/api/admin/users/{GUEST_A_ID}/active", json={"is_active": False}, headers=ORIGIN
    )
    assert off.status_code == 200
    assert epoch(GUEST_A_ID) == before + 1

    on = client.post(
        f"/api/admin/users/{GUEST_A_ID}/active", json={"is_active": True}, headers=ORIGIN
    )
    assert on.status_code == 200
    assert epoch(GUEST_A_ID) == before + 1


def test_setting_a_password_revokes_that_user_s_sessions(
    client: TestClient, pool: FakePool
) -> None:
    _as(client, ROOT_ID)
    before = pool.query("SELECT session_epoch FROM app_user WHERE id = ?", NORMAL_ID)[0]

    response = client.post(
        f"/api/admin/users/{NORMAL_ID}/password", json={"password": PASSWORD}, headers=ORIGIN
    )

    assert response.status_code == 200
    after = pool.query("SELECT session_epoch, password_hash FROM app_user WHERE id = ?", NORMAL_ID)[
        0
    ]
    assert after["session_epoch"] == before["session_epoch"] + 1
    assert verify_password(after["password_hash"], PASSWORD)


@pytest.mark.parametrize(
    ("suffix", "body"),
    [
        ("password", {"password": PASSWORD}),
        ("active", {"is_active": False}),
        ("superuser", {"is_superuser": True}),
    ],
)
def test_an_unknown_user_id_is_a_404(client: TestClient, suffix: str, body: dict[str, Any]) -> None:
    _as(client, ROOT_ID)
    response = client.post(f"/api/admin/users/9999/{suffix}", json=body, headers=ORIGIN)

    assert response.status_code == 404
    assert response.json()["detail"] == "Користувача не знайдено."


# --------------------------------------------------------------------------------------
# GET /models
# --------------------------------------------------------------------------------------


def test_models_come_from_the_price_table_with_one_selected(client: TestClient) -> None:
    """`app/records/pricing.py` is the only place this codebase knows a price, and the
    OpenAI API is never called to build this list."""
    _as(client, ROOT_ID)
    assert _put_setting(client, "model", "gpt-5.6-luna").status_code == 200

    models = client.get("/api/admin/models").json()

    assert [m["id"] for m in models] == sorted(MODEL_PRICES)
    assert [m["id"] for m in models if m["selected"]] == ["gpt-5.6-luna"]
    terra = next(m for m in models if m["id"] == "gpt-5.6-terra")
    assert terra["input_per_mtok"] == 2.0
    assert terra["cached_input_per_mtok"] == 0.2
    assert terra["output_per_mtok"] == 12.0
    assert terra["pricing_version"]


def test_a_configured_but_unpriced_model_is_still_reported(client: TestClient) -> None:
    """Running an unpriced model means every turn records `cost_usd = NULL` and the usage
    dashboard goes blind. Hiding it would hide exactly that."""
    _as(client, ROOT_ID)
    assert _put_setting(client, "model", "gpt-9-unpriced").status_code == 200

    models = client.get("/api/admin/models").json()

    assert models[0]["id"] == "gpt-9-unpriced"
    assert models[0]["selected"] is True
    assert models[0]["input_per_mtok"] is None
    assert [m["id"] for m in models if m["selected"]] == ["gpt-9-unpriced"]
