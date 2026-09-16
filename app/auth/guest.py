"""Anonymous visitors, as real `app_user` rows.

In public mode a visitor who has never logged in still gets a row in `app_user`, with
`kind = 'guest'`, and a signed session cookie that points at it exactly the way a logged-in
user's does. That is the whole mechanism, and it is deliberately boring: because the guest
IS an `app_user`, every boundary this application already has — the fourteen `PgStore`
methods that filter on `context.user_id`, `classification.user_id`, `/api/history` — keeps
working untouched, and two visitors on the same demo cannot see each other's conversations
for the same reason two employees cannot.

It is also what makes "my history is still here tomorrow" true. The session cookie has a
90-day max age (`app/main.py::SESSION_MAX_AGE_S`, which is sized by exactly this requirement)
and is re-signed as it is used (`app/auth/deps.py::_SLIDING_REFRESH_S`), so closing the
browser and coming back lands on the same `uid`, the same threads and the same
classifications. Nothing is kept in `localStorage`, and the client is never trusted with an
identity it could edit — the cookie is signed, and `current_user` re-reads the row anyway.

A guest can NEVER log in. The row's `password_hash` is `GUEST_PASSWORD_SENTINEL`, which is
not an argon2 PHC string at all, so `verify_password` takes its `InvalidHashError` branch
and returns False for every input including the sentinel itself. This is spelled as a
non-empty, obviously-intentional marker rather than `''`: an empty string is what a column
default or a half-written INSERT produces by accident, and "no argon2 verifier accepts an
empty hash" is a property of a library rather than a property of this application.
`tests/test_guest_identity.py` asserts the sentinel against a list of candidates, including
every value that could plausibly be typed into a login form to reach it.

A guest can never be a superuser either — `is_superuser` defaults to false, nothing here
sets it, and `require_superuser` checks `kind == 'human'` on top of it.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import TYPE_CHECKING, Final

import asyncpg
from fastapi import HTTPException, Request, status

from app.db import get_pool

if TYPE_CHECKING:
    from app.auth.deps import CurrentUser

log = logging.getLogger(__name__)

# NOT a valid argon2 PHC string: those begin with '$argon2'. argon2-cffi raises
# InvalidHashError on it, which `app.auth.passwords.verify_password` turns into False. The
# leading '!' is the Unix /etc/shadow convention for a locked account, chosen so that the
# value is self-explaining if it ever shows up in a psql session.
GUEST_PASSWORD_SENTINEL: Final = "!guest-no-password-login-impossible"

# Ukrainian, because it is rendered in the header next to the user menu.
GUEST_DISPLAY_NAME: Final = "Гість"

# 12 bytes from `secrets.token_urlsafe` is 16 characters of ~96 bits. The retry loop below
# exists for correctness, not because a collision is expected — at 96 bits it never happens,
# so the loop is really insurance against a future shorter token, and it is cheap.
_GUEST_TOKEN_BYTES: Final = 12
_MINT_ATTEMPTS: Final = 5


async def ensure_guest(request: Request) -> CurrentUser:
    """Mint a guest `app_user` row and write the session cookie that points at it.

    Called by `current_user` when there is no valid session AND the runtime access mode is
    'public'. The caller has already checked the mode; this function does not re-check it,
    so that there is exactly one place in the codebase that decides whether anonymous access
    is allowed.

    The session payload is byte-for-byte the shape `POST /api/login` writes — {uid, ep, iat}
    and nothing else — so the existing `current_user` resolves a guest with no guest-specific
    branch, including the `session_epoch` revocation check.
    """
    from app.auth.deps import CurrentUser  # circular at import time, fine at call time

    pool = get_pool()
    for attempt in range(_MINT_ATTEMPTS):
        username = f"guest_{secrets.token_urlsafe(_GUEST_TOKEN_BYTES)}"
        try:
            row = await pool.fetchrow(
                """
                INSERT INTO app_user
                       (username, password_hash, display_name, kind, is_active, last_seen_at)
                     VALUES ($1, $2, $3, 'guest', true, now())
                  RETURNING id, username, display_name, session_epoch
                """,
                username,
                GUEST_PASSWORD_SENTINEL,
                GUEST_DISPLAY_NAME,
            )
        except asyncpg.UniqueViolationError:
            # username is CITEXT UNIQUE. Retry with a fresh token rather than 500.
            log.warning("guest username collision on %r (attempt %d)", username, attempt + 1)
            continue

        if row is None:  # pragma: no cover - INSERT ... RETURNING always returns a row
            break

        request.session.clear()
        request.session.update(
            {"uid": row["id"], "ep": row["session_epoch"], "iat": int(time.time())}
        )
        log.info("minted guest user %s (id=%s)", row["username"], row["id"])
        return CurrentUser(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"],
            kind="guest",
            is_superuser=False,
        )

    # Five collisions in a row on a 96-bit token is not a collision, it is a broken database.
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Не вдалося створити гостьову сесію.")


async def touch_last_seen(user_id: int) -> None:
    """Record activity. Called from the sliding-refresh branch of `current_user`, so at most
    once per user per `_SLIDING_REFRESH_S` (one day) — never once per request.

    That granularity is chosen by its two consumers and is sufficient for both:
    `purge-guests --older-than-days N` and the admin panel's guest count. A per-request
    UPDATE would make every read a write and every active session a contended row, to buy a
    precision nothing in this application asks for.

    Failure is swallowed: this is bookkeeping on the authentication path, and a visitor
    should not be logged out because a statistics column could not be written.
    """
    try:
        await get_pool().execute("UPDATE app_user SET last_seen_at = now() WHERE id = $1", user_id)
    except Exception:
        log.exception("could not update last_seen_at for user %s", user_id)
