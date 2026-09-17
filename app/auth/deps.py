"""Who is making this request, and is it allowed to be a POST.

`current_user` is the only place a session cookie becomes a user id, and
`RequestContext.user_id` — the Store's entire authorization input — comes from here.

Since 0005 it is also the only place the runtime ACCESS MODE is enforced. The rule, whole:

    valid session, kind='human'   → that user, in either mode. Humans log in; the mode is
                                    not consulted for them, so a superuser can always reach
                                    the admin panel and switch the mode back.
    valid session, kind='guest'   → that guest in 'public'; in 'private' the session is
                                    CLEARED and the request 401s. This is what makes the
                                    switch instant for everyone mid-demo: the next request
                                    each open browser tab makes, it is logged out.
    no valid session              → a freshly minted guest in 'public'; 401 in 'private'.

The mode is read from the database on every request that needs it, uncached, deliberately —
see `app/runtime_settings.get_access_mode`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Final, Literal
from urllib.parse import urlsplit

import asyncpg
from fastapi import HTTPException, Request, status

from app.auth.guest import ensure_guest, touch_last_seen
from app.db import get_pool
from app.runtime_settings import get_access_mode
from app.settings import get_settings

# Columns every caller of the two fetch helpers below relies on.
_USER_COLUMNS: Final = (
    "id, username, display_name, is_active, session_epoch, password_hash, kind, is_superuser"
)

# Re-sign the cookie at most once a day. Starlette only emits Set-Cookie when the session
# was modified, so without this touch the SESSION_MAX_AGE_S window is fixed from login rather
# than sliding, and a user who logs in and only reads is logged out mid-sentence on the last
# day of it.
_SLIDING_REFRESH_S: Final = 86_400

# The admin panel is HIDDEN, not merely protected: `require_superuser` answers 404 so that a
# prober cannot tell an admin route from a path that was never mounted. The body matches the
# one `app/main.py` already returns for a Store NotFoundError, so the two are identical on
# the wire as well as in status.
_NOT_FOUND: Final = "Not found"


@dataclass(frozen=True, slots=True)
class CurrentUser:
    id: int
    username: str
    display_name: str
    # 'human' or 'guest'. Carried on the identity rather than re-queried, because three
    # separate consumers need it — the mode check below, `require_superuser`, and
    # /api/config, which tells the SPA whether to render "log in" or "you are a guest".
    kind: Literal["human", "guest"] = "human"
    is_superuser: bool = False


def _to_current_user(row: asyncpg.Record) -> CurrentUser:
    return CurrentUser(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        kind=row["kind"],
        is_superuser=row["is_superuser"],
    )


async def fetch_user_by_id(user_id: int) -> asyncpg.Record | None:
    return await get_pool().fetchrow(f"SELECT {_USER_COLUMNS} FROM app_user WHERE id = $1", user_id)


async def fetch_user_by_username(username: str) -> asyncpg.Record | None:
    # username is CITEXT, so this comparison is case-insensitive in the database.
    return await get_pool().fetchrow(
        f"SELECT {_USER_COLUMNS} FROM app_user WHERE username = $1", username.strip()
    )


async def resolve_session(request: Request) -> asyncpg.Record | None:
    """The signed cookie → a live, active user row, or None. Never mints, never raises.

    The session cookie is SIGNED, NOT ENCRYPTED — anyone holding it can base64-decode the
    payload — so it carries only {uid, ep, iat} and nothing else, ever.

    The session_epoch re-check is what makes a stateless cookie revocable: `disable-user`
    and `set-password` bump it, and every outstanding cookie for that user dies on its next
    request. The row was being fetched anyway, so revocation costs one integer comparison.
    A session that fails any of those checks is cleared here, so the browser stops sending a
    cookie that can never work again.

    Split out of `current_user` so that `require_superuser` can ask "who is this?" WITHOUT
    the guest-minting side effect: otherwise every probe of a hidden admin URL would insert
    an `app_user` row, and the hidden panel would be a way to fill the table.
    """
    uid = request.session.get("uid")
    epoch = request.session.get("ep")
    if uid is None or epoch is None:
        return None

    row = await fetch_user_by_id(uid)
    if row is None or not row["is_active"] or row["session_epoch"] != epoch:
        request.session.clear()
        return None
    return row


async def _refresh(request: Request, row: asyncpg.Record) -> None:
    """Slide the cookie window, and piggyback the activity stamp on the same schedule."""
    now = int(time.time())
    if now - request.session.get("iat", 0) > _SLIDING_REFRESH_S:
        request.session["iat"] = now  # any mutation re-signs with a fresh timestamp
        await touch_last_seen(row["id"])


async def current_user(request: Request) -> CurrentUser:
    """Resolve the request to a user, minting a guest when the mode allows it. Every request.

    Note for `/api/config`, which calls this and swallows the 401: in public mode this
    function has a WRITE side effect — the first anonymous GET mints a row. That is the
    intended trigger (the SPA's very first call is /api/config, so the visitor has an
    identity before they type anything), and it is why guests are purgeable from the CLI:
    a crawler that never sends the cookie back leaves one abandoned row per visit.
    """
    row = await resolve_session(request)

    # A human's session is valid in both modes, so the kill switch is not on their path at
    # all — one fewer query for logged-in users, and one fewer way for the admin to lock
    # themselves out of the switch they just flipped.
    if row is not None and row["kind"] == "human":
        await _refresh(request, row)
        return _to_current_user(row)

    mode = await get_access_mode()

    if row is None:
        if mode == "public":
            return await ensure_guest(request)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    # A guest with a valid session, in private mode: this is the mid-demo flip. Clearing the
    # session (rather than only 401-ing) means the browser drops the cookie, so flipping back
    # to public later hands them a NEW guest identity instead of silently resurrecting the
    # old one from a cookie they were told was dead.
    if mode == "private":
        request.session.clear()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session no longer valid")

    await _refresh(request, row)
    return _to_current_user(row)


async def require_superuser(request: Request) -> CurrentUser:
    """The admin panel's only door. 404 for everyone who is not a superuser — never 403.

    403 would confirm the route exists, which is precisely what a hidden panel must not do;
    404 makes every admin URL indistinguishable from a path that was never registered. This
    is the same convention `/api/history/{id}` and the Store already use for "not yours".

    Deliberately NOT mode-aware and deliberately not routed through `current_user`:
      * a superuser must be able to reach the switch in either mode — that is the whole
        point of the panel;
      * `resolve_session` cannot mint, so probing an admin URL anonymously creates nothing.

    `kind == 'human'` is asserted on top of `is_superuser`. A guest row cannot have the flag
    set (nothing in the codebase sets it, and the mint hardcodes the default), so this is a
    belt-and-braces check against a future INSERT that copies the wrong template.
    """
    row = await resolve_session(request)
    if row is None or row["kind"] != "human" or not row["is_superuser"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    await _refresh(request, row)
    return _to_current_user(row)


# http://localhost:8000 and http://127.0.0.1:8000 are DIFFERENT origins to a browser, and
# a developer types whichever they happen to type. With only the configured one allowed, the
# other produced a 403 on every POST — which the SPA then read as "log in", landing a public
# visitor on a login page telling them they do not need to log in. Loopback aliases are
# therefore treated as one origin.
#
# Scope is deliberately narrow: this ONLY applies when the configured host is itself loopback.
# A real deployment (PUBLIC_BASE_URL=https://your.domain) gets exactly one allowed origin and
# nothing is relaxed. Loopback cannot be reached by a third-party site's user anyway, so the
# CSRF property this check exists for is untouched.
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})


@lru_cache(maxsize=1)
def allowed_origins() -> frozenset[str]:
    parts = urlsplit(get_settings().public_base_url)
    scheme, netloc = parts.scheme, parts.netloc
    origins = {f"{scheme}://{netloc}"}

    host = (parts.hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        port = f":{parts.port}" if parts.port else ""
        origins |= {f"{scheme}://{alias}{port}" for alias in ("localhost", "127.0.0.1", "[::1]")}

    return frozenset(origins)


def require_same_origin(request: Request) -> None:
    """CSRF layer 2.

    Layer 1 is SameSite=Lax on the session cookie, which defeats the classic cross-site
    form autosubmit. This is layer 2: per the Fetch Standard a browser sends `Origin` on
    every non-GET/HEAD request INCLUDING same-origin ones, and every state-changing request
    in this app (/api/login, /api/logout, /chatkit) is a same-origin POST from our own page.
    So the header is always present and always ours — the check is both sound and free.

    Layer 3, a double-submit token, was considered and rejected: in a same-origin,
    no-CORS, cookie-auth app it adds a cookie, a header, a token store and a rotation story
    for no additional attacker-model coverage. (And no CORSMiddleware is ever added here —
    allow_origins=["*"] with credentials is not even a valid CORS configuration.)
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin")
    if origin is None:
        # A browser always sends it on POST; absence means a non-browser client.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing Origin")
    if origin not in allowed_origins():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-origin request rejected")
