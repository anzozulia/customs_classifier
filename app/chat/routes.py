"""The FastAPI wiring, and the one rule this file exists to enforce.

**The streaming endpoint returns `200 text/event-stream`, always.** Once
`StreamingResponse` has put those headers on the wire, an exception inside the generator can no
longer be turned into an HTTP error: Starlette raises
`RuntimeError: Caught handled exception, but response already started.` and the browser sees an
empty stream with no error item. And `store.load_thread` runs inside the SDK's generator, after
the headers, so "thread not found" arrives on exactly that path.

The measured cost of getting it wrong: a 5xx, *or* a 200 with any other content type, is
indistinguishable from a failure to the client and triggers about five silent full re-runs over
~25 seconds — five times the model bill for one user action. An in-stream `{"type":"error"}`
event is retried zero times, fires `chatkit.error`, and shows the message verbatim.

So: auth and same-origin are checked BEFORE `process()` (those may be a real 401/403 and are on
the wire before the stream starts), and everything after it is converted in-band by `_guarded`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator

from chatkit.server import NonStreamingResult, StreamingResult
from chatkit.store import NotFoundError
from chatkit.types import ErrorEvent
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from app.auth.deps import CurrentUser, current_user, require_same_origin
from app.chat.errors import classify_error, is_retryable
from app.context import RequestContext
from app.runtime_settings import get_access_mode
from app.settings import get_settings

logger = logging.getLogger("uktzed.chat")

router = APIRouter()

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # Honoured by nginx, a harmless no-op under Caddy. Kept because a proxy in front of this
    # app that buffers the stream turns a live drill-down back into v1's static string.
    "X-Accel-Buffering": "no",
}


def _frame(event: ErrorEvent) -> bytes:
    """One SSE frame. The wire format is exactly `data: <json>\\n\\n` — no `event:`, no `id:`,
    no heartbeat and no terminator."""
    payload = event.model_dump_json(by_alias=True, exclude_none=True)
    return b"data: " + payload.encode("utf-8") + b"\n\n"


async def _guarded(result: StreamingResult) -> AsyncIterator[bytes]:
    """Convert any post-headers failure into an in-stream error event. See the module docstring."""
    try:
        async for chunk in result:
            yield chunk
    except NotFoundError:
        yield _frame(ErrorEvent(code="custom", message="Розмову не знайдено.", allow_retry=False))
    except Exception as exc:  # the last place anything can be logged at all
        error_class = classify_error(exc)
        logger.exception("chatkit stream failed: error_class=%s", error_class)
        yield _frame(
            ErrorEvent(
                code="custom",
                message="Внутрішня помилка. Спробуйте ще раз.",
                allow_retry=is_retryable(error_class),
            )
        )


@router.post("/chatkit")
async def chatkit_endpoint(
    request: Request,
    # Depends-in-a-default is the FastAPI idiom; B008's concern (a mutable default built
    # once at import) does not apply to a dependency marker.
    user: CurrentUser = Depends(current_user),  # noqa: B008 - 401 before any work happens
) -> Response:
    require_same_origin(request)

    context = RequestContext(
        user_id=user.id,
        request_id=request.headers.get("chatkit-frame-instance-id") or uuid.uuid4().hex,
        # ChatKit sends one locale per request; the first tag is the one the user picked.
        locale=(request.headers.get("accept-language") or "uk").split(",")[0].strip() or "uk",
    )

    # A malformed body is a CLIENT error. chatkit's process() validates the tagged union with
    # pydantic and lets ValidationError escape, which would surface as a 500 and — worse —
    # tell the caller nothing. Catch it here, BEFORE any streaming starts, and return 400.
    try:
        result = await request.app.state.server.process(await request.body(), context)
    except ValidationError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed ChatKit request"
        ) from None

    if isinstance(result, StreamingResult):
        # media_type is LOAD-BEARING; nothing downstream may rewrite this header.
        return StreamingResponse(
            _guarded(result), media_type="text/event-stream", headers=SSE_HEADERS
        )

    # The ten non-streaming ops. `NonStreamingResult.json` is BYTES, not a str.
    assert isinstance(result, NonStreamingResult)
    return Response(content=result.json, media_type="application/json")


@router.get("/api/config")
async def client_config(request: Request) -> JSONResponse:
    """Runtime configuration for the browser. PUBLIC — never 401.

    The domain key is fetched at RUNTIME rather than read from `import.meta.env`, because Vite
    inlines `VITE_` variables at build time: a baked-in key would mean rebuilding and
    redeploying the bundle to change domains, and the key is registered per hostname. It is a
    public, origin-scoped identifier — it is meant to be in the page — but it is still a value
    that changes on a different schedule than the JavaScript does.

    `user` is what makes this endpoint doubly load-bearing: the SPA's auth guard
    (`web/src/components/AppShell.tsx`) redirects to /login whenever it is null, so the route
    has to answer for an anonymous caller (or /login itself 401-loops) AND has to report the
    session when there is one (or a successful login still lands back on /login). Hence the
    swallowed `HTTPException` rather than a `Depends(current_user)`: `current_user` is reused
    verbatim, including its session_epoch revocation check, and only its 401 is dropped.

    Both key spellings are emitted on purpose. The frontend accepts either (`config.ts`), the
    two chunks that wrote the two sides documented different ones, and one duplicated public
    string is cheaper than a deploy-day mismatch that manifests as a self-deleting iframe.

    Since 0005 this route is also where a PUBLIC-mode visitor becomes somebody. `current_user`
    mints a guest `app_user` row when there is no session and the mode allows it, and writes
    {uid, ep, iat} into `request.session` — so `SessionMiddleware` puts a `Set-Cookie` on this
    very response and the SPA's first request is what gives a first-ever visitor an identity,
    and therefore a private history, before they type anything. Minting stays here rather than
    moving to /chatkit because the SPA reads `user` from this route to decide what to render.
    """
    settings = get_settings()

    # Read the mode explicitly rather than inferring it from `user`. A logged-in human
    # short-circuits `current_user` without consulting it at all, and the SPA needs it in
    # every case: it decides the «демо» pill, the «Гість» label and whether /login is a
    # dead end. One primary-key lookup on a three-row table, once per page load.
    access_mode = await get_access_mode()

    try:
        user = await current_user(request)
    except HTTPException:
        # Private mode with no session — and also the 503 `ensure_guest` raises if it cannot
        # mint. Either way this route answers 200 with `user: null`, because /login reads it
        # too and a 401 here is a redirect loop.
        user = None

    return JSONResponse(
        {
            "domain_key": settings.chatkit_domain_key,
            "domainKey": settings.chatkit_domain_key,
            "locale": "uk-UA",
            # One URL; the operation is in the POST body, not in the path.
            "chatkit_url": "/chatkit",
            "access_mode": access_mode,
            "user": {
                "username": user.username,
                "display_name": user.display_name,
                # A guest is a real app_user row, so these two are the only thing that tells
                # the SPA it is one — and `is_superuser` is what reveals the ✦ entry point to
                # the hidden panel. The panel's DATA is gated by `require_superuser`; this
                # flag only decides whether a link is drawn.
                "kind": user.kind,
                "is_superuser": user.is_superuser,
            }
            if user
            else None,
        },
        headers={"Cache-Control": "no-store"},
    )
