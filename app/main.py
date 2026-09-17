"""The ASGI app.

One FastAPI application: login, `/chatkit`, `/api/history`, health. ChatKit is
framework-agnostic — `process()` takes bytes — so nothing here is a ChatKit adapter; it is
ordinary FastAPI with one streaming endpoint.

`/readyz` is not a ping. It answers the three questions that made v1 undebuggable: is the
database reachable, is the tariff actually ingested, and which model is configured. v1 shipped
a banner claiming `o3` + `gpt-5` while the code constructed `o4-mini` + `gpt-4.1`, and the logs
lied for two months — so the model id is reported here, read from the config object.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from chatkit.store import NotFoundError
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from app.admin.routes import router as admin_router
from app.auth.deps import allowed_origins
from app.auth.routes import router as auth_router
from app.chat.routes import router as chat_router
from app.chat.server import ClassifierServer
from app.chat.store import PgStore
from app.db import close_pool, init_pool
from app.records.routes import router as records_router
from app.runtime_settings import get_runtime
from app.settings import Settings, get_settings
from app.tariff.repo import TariffRepo

logger = logging.getLogger("uktzed")

# How long a signed session cookie stays valid — and, since 0005, how long a GUEST keeps
# their history. That is what sets the number: a guest has no password, no email and no
# recovery flow, so this cookie is the ONLY handle they have on their own conversations. At
# 14 days a visitor who came back after a fortnight silently became a different person with
# an empty history, which is precisely the requirement ("history survives closing and
# reopening the browser") failing quietly. 90 days covers a demo period and the weeks after
# it, and `_SLIDING_REFRESH_S` in app/auth/deps.py re-signs the cookie daily for anyone who
# keeps using the app, so it is a floor rather than a deadline.
#
# A long cookie is not a weaker cookie here: every request re-reads the row and checks
# `is_active` and `session_epoch` (so a password change or `disable-user` revokes instantly),
# flipping the access mode to 'private' clears guest sessions on their very next request, and
# `python -m app.cli purge-guests` deletes the rows themselves.
SESSION_MAX_AGE_S = 90 * 24 * 3600


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    # chatkit/logger.py attaches NO handler unless LOG_LEVEL is set, so every swallowed
    # respond() traceback would otherwise go nowhere at all.
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    pool = await init_pool(settings.database_url)
    # One repo for the process. `TariffRepo` is cheap and documents itself as per-request, but
    # it only caches the active dataset id, and a re-ingest means a restart here anyway.
    tariff = TariffRepo(pool)
    store = PgStore(pool)

    app.state.pool = pool
    app.state.tariff = tariff
    app.state.store = store
    app.state.server = ClassifierServer(store, tariff)

    logger.info("started: model=%s db=%s", settings.model, pool.get_max_size())
    # M2: the one line that lets a deployer see the 403 trap before a visitor hits it. Lives
    # here, not at import — uvicorn configures logging after the module loads, so an import-
    # time logger.info is silently dropped (measured: it never reached `docker compose logs`).
    logger.info("same-origin check accepts Origin in %s", sorted(allowed_origins()))
    try:
        yield
    finally:
        await close_pool()


def _session_secret(settings: Settings) -> str:
    """Fail loudly in production, loudly enough in development.

    The session cookie is signed with this. An empty string signs nothing, and nobody
    discovers that from a running app — so it is checked here, at the only place that uses it.
    """
    if settings.session_secret:
        return settings.session_secret
    if settings.is_production:
        raise RuntimeError("SESSION_SECRET is empty; refusing to sign cookies with nothing")
    logger.warning("SESSION_SECRET is empty: using an ephemeral key, every restart logs out")
    return secrets.token_urlsafe(32)


def refuse_misconfigured_production(settings: Settings) -> None:
    """Boot-time guards for the two settings that fail SILENTLY in production.

    Runs unconditionally at import — an earlier revision put this inside the empty-secret
    fallback of `_session_secret`, after a `raise`, where it could never execute, and a
    smoke test with the placeholder key booted green. Guards that only run when something
    ELSE is already wrong are not guards.

    M3: in production ChatKit verifies CHATKIT_DOMAIN_KEY against the OpenAI org allowlist
    and, on failure, UNMOUNTS the chat. The server sees nothing, both health checks stay
    green, and every visitor gets an empty card. The placeholder must be a boot failure.
    """
    if settings.is_production and settings.chatkit_domain_key in ("", "domain_pk_localhost_dev"):
        raise RuntimeError(
            "CHATKIT_DOMAIN_KEY is unset or still the localhost placeholder while "
            "PUBLIC_BASE_URL is https://; register the domain at platform.openai.com "
            "(org settings -> security -> domain allowlist) and set the real domain_pk_ key"
        )


app = FastAPI(title="UKTZED classifier", lifespan=lifespan, docs_url=None, redoc_url=None)

_settings = get_settings()
refuse_misconfigured_production(_settings)
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret(_settings),
    session_cookie="uktzed_session",
    max_age=SESSION_MAX_AGE_S,
    same_site="lax",
    https_only=_settings.is_production,
)

app.include_router(auth_router)
app.include_router(chat_router)
# /api/history. Registered here with the others and, like them, BEFORE the SPA mount at "/":
# Starlette matches in registration order, so a router included after that mount is
# unreachable — the static handler answers /api/history with its own 404 first.
app.include_router(records_router)
# /api/admin/*. Every route on it is behind `require_superuser`, which answers 404 rather
# than 403, so for everyone else these paths are indistinguishable from the ones that were
# never registered. Included here for the same registration-order reason as the others.
app.include_router(admin_router)


class _SpaFiles(StaticFiles):
    """`web/dist`, with the one thing `StaticFiles` does not do: SPA fallback.

    The frontend uses `BrowserRouter`, so `/history/<id>` is a client-side route with no file
    behind it. Plain `StaticFiles` answers 404 and a reload on any deep link dies — which is
    exactly the failure `web/README.md` asks the deploy to prevent. Falling back to
    `index.html` for a missing path is the whole of "SPA fallback".

    Deliberately not a catch-all `@app.get("/{path:path}")`: this is mounted LAST, so the API
    routers above still win, and a genuinely missing asset under `/assets/` still 404s because
    only navigations (which accept HTML) get the fallback.
    """

    async def get_response(self, path: str, scope: Any) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            accept = Headers(scope=scope).get("accept", "")
            if exc.status_code == 404 and "text/html" in accept:
                return await super().get_response("index.html", scope)
            raise


@app.exception_handler(NotFoundError)
async def _not_found(request: Request, exc: NotFoundError) -> JSONResponse:
    """404, never 403.

    Only reachable for the ten NON-streaming ops (threads.get_by_id, items.list,
    threads.update, threads.delete); the streaming path converts its own failures in-band
    because the headers are already sent. The Store filters by user_id, so "not yours" and
    "does not exist" are the same answer and there is no existence oracle.
    """
    return JSONResponse({"detail": "Not found"}, status_code=404)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Liveness only: the process is up and serving. No dependencies, so a database blip does
    not get the container killed and restarted into the same blip."""
    return JSONResponse({"status": "ok"})


@app.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Readiness: can this process actually serve a classification right now?"""
    checks: dict[str, object] = {}
    ok = True

    try:
        value = await request.app.state.pool.fetchval("SELECT 1")
        checks["db"] = value == 1
        ok &= bool(value == 1)
    except Exception as exc:  # readiness reports, it does not raise
        checks["db"] = f"error: {type(exc).__name__}"
        ok = False

    try:
        dataset = await request.app.state.tariff.active_dataset()
        # The whole file is 14,187 nodes / 10,490 terminals; an active row with nodes in it
        # means ingest ran and this process can actually answer.
        checks["dataset_sha256"] = dataset.sha256[:12]
        checks["tariff_nodes"] = dataset.node_count
        ok &= dataset.node_count > 0
    except Exception as exc:
        checks["tariff_nodes"] = f"error: {type(exc).__name__}"
        ok = False

    # The EFFECTIVE model, not the configured one. Since 0005 a superuser can change it from
    # the admin panel with no redeploy, and a readiness probe still reporting the .env value
    # would be v1's lying banner with a new coat of paint. `get_runtime` falls back to the
    # environment when `app_setting` holds no row — and when the database is unreachable, a
    # case the `db` check above has already reported — so it cannot raise here.
    model = await get_runtime("model")
    checks["model"] = model or None
    ok &= bool(model)

    return JSONResponse(
        {"status": "ok" if ok else "not_ready", "checks": checks},
        status_code=200 if ok else 503,
    )


# Registered LAST, and that is the whole trick: Starlette matches routes in registration
# order, so a mount at "/" declared any earlier would swallow /healthz and /readyz.
# `web/dist` is absent in a fresh checkout (`npm --prefix web run build` has not run), which
# must not stop the API from booting — `make dev` serves the SPA from Vite on another port.
_WEB_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"
if _WEB_DIST.is_dir():
    app.mount("/", _SpaFiles(directory=_WEB_DIST, html=True), name="web")
else:
    logger.warning("web/dist not found at %s - serving the API only", _WEB_DIST)
