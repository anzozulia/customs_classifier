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

No database and no network: the asyncpg pool is a stub that answers the four queries the
authentication path and `/readyz` actually issue. That is the point — these are contract
tests, and a contract test that needs Postgres does not get run.
"""

from __future__ import annotations

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
from app.auth.deps import allowed_origins  # noqa: E402
from app.auth.passwords import hash_password  # noqa: E402
from app.settings import get_settings  # noqa: E402

get_settings.cache_clear()
allowed_origins.cache_clear()

import app.main as main  # noqa: E402

ORIGIN = {"Origin": "http://testserver"}
PASSWORD = "a-correct-password"
USER = {
    "id": 42,
    "username": "anton",
    "display_name": "Anton",
    "is_active": True,
    "session_epoch": 3,
    # One real argon2 hash for the module: hashing is ~80 ms and this is the only one needed.
    "password_hash": hash_password(PASSWORD),
}


class _StubPool:
    """The four queries the tested paths issue, and nothing else.

    Matched on SQL fragments rather than on exact strings so that reformatting a query does
    not silently turn a contract test green by returning None everywhere.
    """

    def get_max_size(self) -> int:
        return 1

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "FROM app_user" in query:
            if "username = $1" in query and str(args[0]).lower() != USER["username"]:
                return None
            if "id = $1" in query and args[0] != USER["id"]:
                return None
            return dict(USER)
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
        return []

    async def execute(self, query: str, *args: Any) -> str:
        return "UPDATE 1"

    async def close(self) -> None:
        return None


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    pool = _StubPool()

    async def _init_pool(dsn: str, **kwargs: Any) -> _StubPool:
        db._pool = pool
        return pool

    async def _close_pool() -> None:
        db._pool = None

    monkeypatch.setattr(main, "init_pool", _init_pool)
    monkeypatch.setattr(main, "close_pool", _close_pool)
    with TestClient(main.app, base_url="http://testserver") as test_client:
        yield test_client


def _login(client: TestClient) -> Any:
    # `URLSearchParams`, i.e. application/x-www-form-urlencoded — what LoginPage.tsx sends.
    return client.post(
        "/api/login", data={"username": "anton", "password": PASSWORD}, headers=ORIGIN
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
