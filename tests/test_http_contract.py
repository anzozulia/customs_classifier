"""The HTTP seams between the Python app and the SPA.

Every assertion here pins a contract that two chunks wrote independently and that nothing
else in the suite touches. Three of them were actually broken when the chunks were merged:

* `GET /api/config` returned neither `user` nor `chatkit_url`, so `AppShell`'s auth guard
  (`if (!config.user) redirect('/login')`) sent every logged-in user straight back to the
  login page — a 200 on every request and an application that could not be entered.
* `POST /api/login` expected a JSON body while `LoginPage.tsx` posts a `URLSearchParams`,
  which is a 422 on every login attempt.
* `app.main` mounted no static files at all, so `web/dist` was never served and a reload on
  `/history/<id>` had nothing to fall back to.

The wiring pass for the admin milestone added three more, all of them seams between chunks
that were written in parallel and none of them visible to the tests those chunks shipped:

* `GET /api/config` now carries `access_mode` and, on `user`, `kind` and `is_superuser` —
  everything `web/src/lib/config.ts` reads. In PUBLIC mode the route MINTS a guest, so the
  first request a first-ever visitor makes is what gives them an identity; the `Set-Cookie`
  that carries it is asserted here rather than assumed.
* Flipping the mode to `private` ends a live guest session on its very next request. That is
  the author's kill switch, and this is the only test that exercises it through the real app
  rather than against the dependency in isolation.
* `app/main.py` includes the admin router, and includes it BEFORE the SPA mount. The tell is
  the 404 body: `require_superuser` answers `{"detail": "Not found"}` and Starlette's own
  handler answers `{"detail": "Not Found"}`, so the capital letter is the difference between
  "the hidden panel is mounted" and "the static handler swallowed it".

No database and no network: the asyncpg pool is a stub that answers the handful of queries
the authentication path, the runtime-settings layer, the admin overview and `/readyz`
actually issue. That is the point — these are contract tests, and a contract test that needs
Postgres does not get run.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Before `app.main` is imported: it builds the FastAPI app (and reads the session secret) at
# module scope, and `get_settings()` is lru_cached.
os.environ["SESSION_SECRET"] = "test-secret-not-used-anywhere-real"
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ["DATABASE_URL"] = "postgresql://unused:unused@127.0.0.1:5432/unused"

from fastapi.testclient import TestClient  # noqa: E402

import app.db as db  # noqa: E402
import app.runtime_settings as runtime_settings  # noqa: E402
from app.auth.deps import allowed_origins  # noqa: E402
from app.auth.passwords import hash_password  # noqa: E402
from app.settings import get_settings  # noqa: E402

get_settings.cache_clear()
allowed_origins.cache_clear()

import app.main as main  # noqa: E402

ORIGIN = {"Origin": "http://testserver"}
PASSWORD = "a-correct-password"
# One real argon2 hash for the module: hashing is ~80 ms and both accounts can share it.
_HASH = hash_password(PASSWORD)
USER = {
    "id": 42,
    "username": "anton",
    "display_name": "Anton",
    "is_active": True,
    "session_epoch": 3,
    # Added by migration 0005. This stub mirrors `_USER_COLUMNS`, so a column the auth path
    # reads has to appear here too — 'human' is what every account this file creates is.
    "kind": "human",
    "is_superuser": False,
    "password_hash": _HASH,
}
#: The one account that can see /api/admin/*. Separate from USER on purpose: the tests below
#: assert that an ordinary human gets exactly the same 404 an anonymous caller does.
SUPERUSER = {
    "id": 7,
    "username": "root",
    "display_name": "Root",
    "is_active": True,
    "session_epoch": 0,
    "kind": "human",
    "is_superuser": True,
    "password_hash": _HASH,
}


class _StubPool:
    """The queries the tested paths issue, and nothing else.

    Matched on SQL fragments rather than on exact strings so that reformatting a query does
    not silently turn a contract test green by returning None everywhere.

    It holds mutable state for two reasons, both of them the point of the new tests: guests
    are INSERTed by `app/auth/guest.py` and then re-read by `current_user` on the next
    request (so they have to persist across requests the way a row does), and `app_setting`
    is what `app/runtime_settings.py` reads — an empty `settings` dict is a database with no
    override, which resolves to the closed 'private' mode.
    """

    def __init__(self) -> None:
        self.users: dict[int, dict[str, Any]] = {
            int(USER["id"]): dict(USER),
            int(SUPERUSER["id"]): dict(SUPERUSER),
        }
        self.settings: dict[str, str] = {}
        self._next_id = 1000

    # -- test-side helpers --------------------------------------------------------------

    def set_runtime(self, key: str, value: str) -> None:
        """What a superuser's `PUT /api/admin/settings` leaves behind, without the route."""
        self.settings[key] = value

    def _setting_row(self, key: str) -> dict[str, Any] | None:
        if key not in self.settings:
            return None
        # asyncpg hands JSONB back as a str — no codec is installed (app/db.py) — and
        # `_parse_value` json.loads it, so the double quotes here are load-bearing.
        return {
            "key": key,
            "value": json.dumps(self.settings[key]),
            "updated_at": datetime.now(UTC),
            "updated_by": None,
        }

    # -- the asyncpg surface ------------------------------------------------------------

    def get_max_size(self) -> int:
        return 1

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "INSERT INTO app_user" in query:
            # app/auth/guest.py::ensure_guest. The INSERT returns four columns; the row kept
            # here is the full one, because the very next request re-reads it by id.
            self._next_id += 1
            row = {
                "id": self._next_id,
                "username": args[0],
                "password_hash": args[1],
                "display_name": args[2],
                "kind": "guest",
                "is_superuser": False,
                "is_active": True,
                "session_epoch": 0,
            }
            self.users[row["id"]] = row
            return dict(row)
        if "FROM app_setting" in query:
            return self._setting_row("access_mode")
        if "FROM app_user" in query:
            if "username = $1" in query:
                wanted = str(args[0]).lower()
                for row in self.users.values():
                    if str(row["username"]).lower() == wanted:
                        return dict(row)
                return None
            if "id = $1" in query:
                row = self.users.get(args[0])
                return dict(row) if row is not None else None
            return None
        if "tariff_dataset" in query:
            return {
                "id": 1,
                "sha256": "5a113fc09ac05028afbd6ed1884a95133b65e7827e61a87d74d5ffb11a38dee3",
                "source_filename": "uktzed_hierarchical.json",
                "node_count": 14_187,
                "terminal_count": 10_490,
                "ingested_at": datetime.now(UTC),
            }
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        return 1 if query.strip() == "SELECT 1" else None

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        if "FROM app_setting" in query:
            # `get_all_runtime` / `get_runtime` read every key in one statement.
            return [row for row in (self._setting_row(key) for key in self.settings) if row]
        return []

    async def execute(self, query: str, *args: Any) -> str:
        if "INSERT INTO app_setting" in query:
            # `app/runtime_settings.py::set_runtime` — ($1 key, $2 JSON value, $3 updated_by).
            # Stored rather than swallowed so that the kill switch can be tested through the
            # admin route that a superuser actually uses.
            self.settings[str(args[0])] = str(json.loads(args[1]))
            return "INSERT 0 1"
        return "UPDATE 1"

    async def close(self) -> None:
        return None


@pytest.fixture
def pool() -> Iterator[_StubPool]:
    """The database, such as it is. Separate from `client` so a test can change the access
    mode mid-session — which is exactly what the author does during a demo."""
    stub = _StubPool()
    runtime_settings.invalidate_cache()  # the 3 s TTL snapshot is a module global
    yield stub
    runtime_settings.invalidate_cache()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, pool: _StubPool) -> Iterator[TestClient]:
    async def _init_pool(dsn: str, **kwargs: Any) -> _StubPool:
        db._pool = pool
        return pool

    async def _close_pool() -> None:
        db._pool = None

    monkeypatch.setattr(main, "init_pool", _init_pool)
    monkeypatch.setattr(main, "close_pool", _close_pool)
    with TestClient(main.app, base_url="http://testserver") as test_client:
        yield test_client


def _login(client: TestClient, username: str = "anton") -> Any:
    # `URLSearchParams`, i.e. application/x-www-form-urlencoded — what LoginPage.tsx sends.
    return client.post(
        "/api/login", data={"username": username, "password": PASSWORD}, headers=ORIGIN
    )


# ---------------------------------------------------------------------------
# GET /api/config — public, and the SPA's entire notion of "am I logged in"
# ---------------------------------------------------------------------------


def test_config_is_public_and_reports_no_user(client: TestClient) -> None:
    response = client.get("/api/config")
    body = response.json()

    assert response.status_code == 200, "anonymous /api/config must not 401: /login 401-loops"
    assert body["domain_key"], "config.ts reads domain_key; without it the frame self-deletes"
    assert body["locale"] == "uk-UA"
    assert body["chatkit_url"] == "/chatkit"
    assert body["user"] is None


def test_config_reports_the_user_once_logged_in(client: TestClient) -> None:
    assert _login(client).status_code == 200
    body = client.get("/api/config").json()

    # AppShell.tsx redirects to /login whenever this is null.
    assert body["user"] is not None
    assert body["user"]["username"] == "anton"


def test_config_is_never_cached(client: TestClient) -> None:
    assert client.get("/api/config").headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# GET /api/config, part two: the access mode and the guest identity
# ---------------------------------------------------------------------------


def test_config_reports_the_closed_mode_when_nothing_is_set(client: TestClient) -> None:
    """An empty `app_setting` is 'private'. The SPA reads this to decide whether to render a
    login screen, and the failure mode of the other default is "the demo was open all night
    and nobody noticed"."""
    body = client.get("/api/config").json()

    assert body["access_mode"] == "private"
    assert body["user"] is None


def test_config_reports_kind_and_superuser_for_a_logged_in_human(client: TestClient) -> None:
    """`kind` decides whether the header says «Гість»; `is_superuser` decides whether the ✦
    link to the hidden panel is drawn at all."""
    assert _login(client, "root").status_code == 200
    user = client.get("/api/config").json()["user"]

    assert user == {
        "username": "root",
        "display_name": "Root",
        "kind": "human",
        "is_superuser": True,
    }


def test_config_mints_a_guest_in_public_mode_and_sets_the_cookie(
    client: TestClient, pool: _StubPool
) -> None:
    """The whole of "a visitor has a history": one GET, one `app_user` row, one Set-Cookie.

    This is the SPA's first request, so the identity exists before anything is typed — and
    the cookie is what makes the history survive a reload and a closed browser.
    """
    pool.set_runtime("access_mode", "public")
    response = client.get("/api/config")
    body = response.json()

    assert response.status_code == 200
    assert body["access_mode"] == "public"
    assert body["user"]["kind"] == "guest"
    assert body["user"]["username"].startswith("guest_")
    assert body["user"]["is_superuser"] is False
    assert "uktzed_session=" in response.headers.get("set-cookie", ""), (
        "without the cookie the next request mints a second guest and the history is lost"
    )

    # A real row, not a synthetic identity: `current_user` re-reads it by id on every request.
    minted = [row for row in pool.users.values() if row["kind"] == "guest"]
    assert len(minted) == 1
    assert minted[0]["username"] == body["user"]["username"]


def test_the_guest_identity_survives_the_next_request(client: TestClient, pool: _StubPool) -> None:
    """Reload, and it is the same person — one row, not one per page view."""
    pool.set_runtime("access_mode", "public")
    first = client.get("/api/config").json()["user"]["username"]
    second = client.get("/api/config").json()["user"]["username"]

    assert first == second
    assert len([row for row in pool.users.values() if row["kind"] == "guest"]) == 1


def test_switching_to_private_ends_a_live_guest_session(
    client: TestClient, pool: _StubPool
) -> None:
    """The kill switch, end to end through the real app.

    The mode is read from the database on every request and is never cached
    (`app/runtime_settings.get_access_mode`), so the flip lands on the guest's very next
    request — no restart, no TTL to wait out. The session is CLEARED rather than merely
    refused, which is why flipping back to public later hands out a NEW identity.
    """
    pool.set_runtime("access_mode", "public")
    assert client.get("/api/config").json()["user"]["kind"] == "guest"

    pool.set_runtime("access_mode", "private")
    body = client.get("/api/config").json()

    assert body["access_mode"] == "private"
    assert body["user"] is None, "a guest must not survive the switch to private"
    # And /chatkit — the endpoint that spends money — is closed to them too.
    assert client.post("/chatkit", content=b"{}", headers=ORIGIN).status_code == 401


def test_a_human_session_outlives_the_switch_to_private(
    client: TestClient, pool: _StubPool
) -> None:
    """The switch must not log the author out of the panel they just used to flip it."""
    assert _login(client, "root").status_code == 200
    pool.set_runtime("access_mode", "private")

    assert client.get("/api/config").json()["user"]["username"] == "root"


# ---------------------------------------------------------------------------
# POST /api/login — form-encoded, per the architecture's Form(...) handler
# ---------------------------------------------------------------------------


def test_login_accepts_form_encoding(client: TestClient) -> None:
    response = _login(client)
    assert response.status_code == 200, response.text
    assert response.json()["username"] == "anton"


def test_login_rejects_a_wrong_password(client: TestClient) -> None:
    response = client.post(
        "/api/login", data={"username": "anton", "password": "wrong"}, headers=ORIGIN
    )
    assert response.status_code == 401


def test_login_requires_a_same_origin_header(client: TestClient) -> None:
    response = client.post("/api/login", data={"username": "anton", "password": PASSWORD})
    assert response.status_code == 403


def test_me_follows_the_session(client: TestClient) -> None:
    assert client.get("/api/me").status_code == 401
    assert _login(client).status_code == 200
    assert client.get("/api/me").json()["username"] == "anton"
    assert client.post("/api/logout", headers=ORIGIN).status_code == 204
    assert client.get("/api/me").status_code == 401


def test_chatkit_checks_csrf_before_processing(client: TestClient) -> None:
    """Auth and same-origin run BEFORE `process()`, while a real status code is still
    possible; everything after the SSE headers has to be an in-stream error event."""
    assert _login(client).status_code == 200
    assert client.post("/chatkit", content=b"{}").status_code == 403


# ---------------------------------------------------------------------------
# /api/admin/* — mounted, and mounted before the SPA
# ---------------------------------------------------------------------------


def test_the_admin_router_is_mounted_and_answers_404_to_everyone_else(
    client: TestClient,
) -> None:
    """Three callers, one answer, and the answer proves the ROUTER produced it.

    `require_superuser` raises 404 with the body `{"detail": "Not found"}`; Starlette's own
    handler — the one behind the SPA mount at "/" — spells it `"Not Found"`. So a lowercase
    'f' here is the mechanical proof that the router is included and included early enough,
    which is the failure this file exists to catch (see the module docstring).
    """
    for setup in (lambda: client.cookies.clear(), lambda: _login(client, "anton")):
        setup()
        response = client.get("/api/admin/overview")
        assert response.status_code == 404
        assert response.json() == {"detail": "Not found"}


def test_a_superuser_reaches_the_admin_api_through_the_app(client: TestClient) -> None:
    """The panel's landing request, through `app.main` with the SPA mounted underneath."""
    assert _login(client, "root").status_code == 200
    response = client.get("/api/admin/overview")
    body = response.json()

    assert response.status_code == 200
    assert body["access_mode"] == "private"
    assert body["model"] == get_settings().model
    assert set(body["usage"]) == {"today", "last_7d", "all_time"}


def test_the_kill_switch_reaches_a_live_guest_on_their_next_request(
    client: TestClient, pool: _StubPool
) -> None:
    """The whole feature, end to end, in one test.

    A visitor is browsing in public mode. The author opens the panel and closes the demo. The
    visitor's very next request — not their next login, not three seconds later, not after a
    restart — no longer resolves. That is the promise the access mode makes, and it holds
    because `get_access_mode()` reads the row on every request and is never cached.
    """
    pool.set_runtime("access_mode", "public")
    assert client.get("/api/config").json()["user"]["kind"] == "guest"
    guest_cookies = dict(client.cookies)

    # The author signs in — a human, so their own session is untouched by the switch — and
    # throws it through the real route.
    client.cookies.clear()
    assert _login(client, "root").status_code == 200
    flipped = client.put(
        "/api/admin/settings", json={"key": "access_mode", "value": "private"}, headers=ORIGIN
    )
    assert flipped.status_code == 200, flipped.text
    # Read BACK out of the database by the route, not echoed from the request body.
    assert flipped.json()["value"] == "private"
    assert flipped.json()["source"] == "db"

    # Back to the visitor's browser, with the cookie it was holding a moment ago.
    client.cookies.clear()
    client.cookies.update(guest_cookies)
    body = client.get("/api/config").json()

    assert body["access_mode"] == "private"
    assert body["user"] is None
    assert client.post("/chatkit", content=b"{}", headers=ORIGIN).status_code == 401


def test_the_admin_api_rejects_a_cross_origin_write(client: TestClient) -> None:
    """Same-origin is declared on the router next to the door, so no route can forget it."""
    assert _login(client, "root").status_code == 200
    response = client.put("/api/admin/settings", json={"key": "access_mode", "value": "public"})

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Health, and the SPA mount
# ---------------------------------------------------------------------------


def test_healthz_is_unauthenticated_and_dependency_free(client: TestClient) -> None:
    # The Docker healthcheck hits this; it must not touch the database.
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_reports_db_dataset_and_model(client: TestClient) -> None:
    body = client.get("/readyz").json()
    assert body["status"] == "ok"
    assert body["checks"]["db"] is True
    assert body["checks"]["tariff_nodes"] == 14_187
    # v1 shipped a banner naming a model it did not run. This is read from the config object.
    assert body["checks"]["model"] == get_settings().model


def test_readyz_reports_the_model_the_admin_panel_selected(
    client: TestClient, pool: _StubPool
) -> None:
    """…and when a superuser overrides it at runtime, the probe follows.

    `/readyz` reporting the .env value while the process runs something else is the exact
    shape of v1's two-month lie, and since 0005 the model is changeable without a redeploy.
    """
    pool.set_runtime("model", "gpt-5.6-luna")
    assert client.get("/readyz").json()["checks"]["model"] == "gpt-5.6-luna"


@pytest.mark.skipif(
    not (ROOT / "web" / "dist" / "index.html").is_file(),
    reason="web/dist is a build artefact; run `npm --prefix web run build` first",
)
def test_spa_is_served_without_shadowing_the_api(client: TestClient) -> None:
    """The mount is registered last on purpose — Starlette matches in registration order."""
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/config").status_code == 200
    assert client.get("/").status_code == 200

    # BrowserRouter deep link: no file behind it, so it has to fall back to index.html.
    deep = client.get("/history/abc", headers={"Accept": "text/html"})
    assert deep.status_code == 200
    assert "<!doctype html" in deep.text.lower()

    # A missing asset is still a missing asset.
    assert client.get("/assets/nope.js", headers={"Accept": "*/*"}).status_code == 404
