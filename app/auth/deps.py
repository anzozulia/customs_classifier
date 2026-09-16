"""Who is making this request, and is it allowed to be a POST.

`current_user` is the only place a session cookie becomes a user id, and
`RequestContext.user_id` — the Store's entire authorization input — comes from here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Final
from urllib.parse import urlsplit

import asyncpg
from fastapi import HTTPException, Request, status

from app.db import get_pool
from app.settings import get_settings

# Columns every caller of the two fetch helpers below relies on.
_USER_COLUMNS: Final = "id, username, display_name, is_active, session_epoch, password_hash"

# Re-sign the cookie at most once a day. Starlette only emits Set-Cookie when the session
# was modified, so without this touch the 14-day window is fixed from login rather than
# sliding, and a user who logs in and only reads is logged out mid-sentence on day 14.
_SLIDING_REFRESH_S: Final = 86_400


@dataclass(frozen=True, slots=True)
class CurrentUser:
    id: int
    username: str
    display_name: str


async def fetch_user_by_id(user_id: int) -> asyncpg.Record | None:
    return await get_pool().fetchrow(f"SELECT {_USER_COLUMNS} FROM app_user WHERE id = $1", user_id)


async def fetch_user_by_username(username: str) -> asyncpg.Record | None:
    # username is CITEXT, so this comparison is case-insensitive in the database.
    return await get_pool().fetchrow(
        f"SELECT {_USER_COLUMNS} FROM app_user WHERE username = $1", username.strip()
    )


async def current_user(request: Request) -> CurrentUser:
    """Resolve the signed-cookie session to a live, active user. Runs on every request.

    The session cookie is SIGNED, NOT ENCRYPTED — anyone holding it can base64-decode the
    payload — so it carries only {uid, ep, iat} and nothing else, ever.

    The session_epoch re-check is what makes a stateless cookie revocable: `disable-user`
    and `set-password` bump it, and every outstanding cookie for that user dies on its next
    request. The row was being fetched anyway, so revocation costs one integer comparison.
    """
    uid = request.session.get("uid")
    epoch = request.session.get("ep")
    if uid is None or epoch is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    row = await fetch_user_by_id(uid)
    if row is None or not row["is_active"] or row["session_epoch"] != epoch:
        request.session.clear()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session no longer valid")

    now = int(time.time())
    if now - request.session.get("iat", 0) > _SLIDING_REFRESH_S:
        request.session["iat"] = now  # any mutation re-signs with a fresh timestamp

    return CurrentUser(id=row["id"], username=row["username"], display_name=row["display_name"])


@lru_cache(maxsize=1)
def allowed_origins() -> frozenset[str]:
    parts = urlsplit(get_settings().public_base_url)
    return frozenset({f"{parts.scheme}://{parts.netloc}"})


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
