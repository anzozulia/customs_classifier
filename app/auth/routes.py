"""POST /api/login · POST /api/logout · GET /api/me.

Three routes, and deliberately no fourth: there is no /api/register, no password-reset and
no invite flow anywhere in this application. Accounts are created by `python -m app.cli
create-user`. The absence of the route is the "no self-registration" requirement — there is
nothing to leave switched on by accident.
"""

from __future__ import annotations

import asyncio
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.auth.deps import CurrentUser, current_user, fetch_user_by_username, require_same_origin
from app.auth.passwords import dummy_hash, hash_password, needs_rehash, verify_password
from app.db import get_pool

router = APIRouter(prefix="/api", tags=["auth"])

# One message for every failure mode below. "No such user", "wrong password" and "account
# disabled" must be indistinguishable to the client.
# (Ukrainian wording note: ruff RUF001 rejects any word whose letters are ALL confusable
# with ASCII — the common three-letter word for "or" is one — so this uses a synonym.
# A trailing per-line noqa for that rule is the alternative if a message needs it.)
_INVALID = "Невірне ім'я користувача чи пароль."


class LoginRequest(BaseModel):
    """The login body. Consumed as `application/x-www-form-urlencoded`, not as JSON.

    Form encoding is what the architecture's handler (§8, `username: str = Form(...)`) and the
    shipped SPA (`web/src/pages/LoginPage.tsx` posts a `URLSearchParams`) both use, and it is
    why `python-multipart` is pinned. FastAPI's pydantic-model-as-form support (>=0.113) keeps
    the validation constraints below in one declared place instead of two `Form(...)` defaults,
    so this is the form contract AND the model. A JSON body is rejected with 422.
    """

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=4096)


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str


@router.post("/login", response_model=UserOut)
async def login(request: Request, body: Annotated[LoginRequest, Form()]) -> UserOut:
    require_same_origin(request)

    row = await fetch_user_by_username(body.username)
    # Always hash something. Returning early for an unknown username makes the endpoint a
    # username oracle: ~0 ms vs ~80 ms is trivially measurable over the network.
    stored = row["password_hash"] if row is not None else dummy_hash()

    # argon2 is deliberately slow. Off the event loop, or every concurrent SSE stream in
    # this worker stalls for the duration.
    ok = await asyncio.to_thread(verify_password, stored, body.password)

    if row is None or not ok or not row["is_active"]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _INVALID)

    pool = get_pool()
    if needs_rehash(row["password_hash"]):
        new_hash = await asyncio.to_thread(hash_password, body.password)
        await pool.execute(
            "UPDATE app_user SET password_hash = $2 WHERE id = $1", row["id"], new_hash
        )
    await pool.execute("UPDATE app_user SET last_login_at = now() WHERE id = $1", row["id"])

    # Session fixation: SessionMiddleware re-signs with a fresh timestamp on every
    # modification, so clear-then-set is a genuine rotation of the cookie value.
    # `ep` is the revocation check (see deps.current_user); `iat` drives sliding expiry.
    request.session.clear()
    request.session.update({"uid": row["id"], "ep": row["session_epoch"], "iat": int(time.time())})
    return UserOut(id=row["id"], username=row["username"], display_name=row["display_name"])


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request) -> None:
    require_same_origin(request)
    # Clearing an already-empty session is a no-op, so this is safe to call unauthenticated
    # and gives the client one unconditional way to end a session.
    request.session.clear()


@router.get("/me", response_model=UserOut)
async def me(user: Annotated[CurrentUser, Depends(current_user)]) -> UserOut:
    """The frontend's session probe: 200 means "render the chat", 401 means "render login"."""
    return UserOut(id=user.id, username=user.username, display_name=user.display_name)
