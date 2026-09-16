"""/api/admin/* — the five things the author can do to a running demo, and nothing else.

This panel exists because of one decision made elsewhere: **there are no spend caps and no
rate limits**. The access-mode toggle is the only lever, and the usage dashboard is the only
spend visibility. Both of those facts shape this file:

* **The dashboard is computed, never estimated.** Every number in `usage` is an aggregate
  over `classification`, the table that already records one row per turn — including the
  turns that failed, which is exactly the population a cost estimate built from successes
  would miss. No counters, no cache, no second source of truth to drift. `today` means
  00:00 UTC, and each window reports the `since` it actually used.
* **The toggle is instant.** `PUT /api/admin/settings` delegates to
  `app.runtime_settings.set_runtime`, which writes the row and drops the process cache; the
  response is then read BACK out of the database rather than echoing the request, so the
  superuser sees what is actually in effect. `access_mode` is never served from a cache on
  the auth path (see `app/runtime_settings`), so flipping to `private` logs every guest out
  on their very next request.

**Everything is behind `require_superuser`, declared once on the router.** A route added
later cannot forget the door. `require_same_origin` sits next to it in the same list and is
a documented no-op for GET/HEAD/OPTIONS, so every mutating route is CSRF-checked
structurally rather than by remembering a call. The order matters: the 404 from
`require_superuser` is resolved first, so a cross-origin probe from a non-superuser still
learns nothing but "there is no such path".

**This is the one place in the application that reads across users.** Everywhere else —
the fourteen `PgStore` methods, `/api/history`, `classification` — the boundary is
`user_id`, and `tests/test_store_isolation.py` enforces that structurally. Here the boundary
is the door, which is why `tests/test_admin_api.py` asserts it on every single route.

**Money is a JSON float**, six decimal places, matching `/api/history`'s `cost_usd` and the
`NUMERIC(10,6)` column behind it. (`Decimal` is used for the arithmetic; the conversion
happens once, at the wire.) Token counts and classification counts are integers.

Deliberately NOT here, because the author ruled them out: spend caps, rate limits, a
cross-user classification browser, and a tariff re-ingest trigger.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Final, Literal, cast

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.agent.prompts import PROMPT_VERSION, render_system_prompt
from app.auth.deps import CurrentUser, require_same_origin, require_superuser
from app.auth.passwords import hash_password
from app.db import get_pool
from app.records.pricing import MODEL_PRICES, PRICING_VERSION
from app.runtime_settings import (
    InvalidRuntimeValueError,
    RuntimeKey,
    get_all_runtime,
    set_runtime,
)

logger = logging.getLogger("uktzed.admin")

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    # The door, declared once. `require_superuser` 404s for anonymous callers, for ordinary
    # humans and for guests; `require_same_origin` returns immediately for safe methods, so
    # listing it here covers every mutating route in this file and every one added later.
    # Order is load-bearing: 404 before 403, or the 403 confirms the path exists.
    dependencies=[Depends(require_superuser), Depends(require_same_origin)],
)

#: The locale the prompt digest in `/overview` is rendered for. `RequestContext.locale`
#: defaults to "uk" and the SPA only ever sends uk-UA, so this is the digest that lands on
#: essentially every `classification` row — but it IS a per-locale value, hence the constant.
_PROMPT_LOCALE: Final = "uk"

#: The lower bound for the "all time" window. A parameter rather than a second statement, so
#: there is exactly one aggregate query in this file to read and to review.
_ALL_TIME: Final = datetime(1, 1, 1, tzinfo=UTC)

#: `cost_usd` is NUMERIC(10,6); this is the same precision on the way out.
_MONEY_DP: Final = 6


# ---------------------------------------------------------------------------- the contract


class OutcomeMix(BaseModel):
    """The four-value HTTP vocabulary, not the five-value stored one.

    `classification.outcome` also holds `conversation` — a turn that answered in prose
    without reaching a terminal tool. `app/records/routes.py` collapses it into `classified`
    at the API boundary and so does the SQL below, so the admin dashboard and a user's own
    history never disagree about what happened.
    """

    classified: int
    clarification: int
    error: int
    pending: int


class UsageWindow(BaseModel):
    """One time window of the register, aggregated."""

    #: Inclusive lower bound, always UTC — `today` is 00:00 UTC, not 00:00 Kyiv. Reported
    #: rather than implied so the panel can label the window it is actually showing. Null
    #: for the all-time window, which has no lower bound.
    since: datetime | None
    classifications: int
    #: USD, a float with six decimals. See the module docstring. It is a FLOOR, not a total:
    #: a turn on a model missing from `app/records/pricing.py` stores `cost_usd = NULL` and
    #: contributes nothing here, which is what `runs_unpriced` exists to say out loud.
    cost_usd: float
    #: Turns in this window with `cost_usd IS NULL` — i.e. money this dashboard cannot see.
    #: Zero in every normal deployment; non-zero is the one number that makes `cost_usd` a
    #: lie if it is not shown, so the panel renders it in amber whenever it is not zero.
    runs_unpriced: int
    tokens_in: int
    #: A SUBSET of `tokens_in`, billed at the cached rate (see `app/records/pricing.py`).
    tokens_cached: int
    tokens_out: int
    outcomes: OutcomeMix
    #: Distinct `app_user`s who ran at least one classification in this window. On the
    #: `today` window this is the "active users today" number.
    users: int
    human_users: int
    guest_users: int
    human_classifications: int
    guest_classifications: int


class Usage(BaseModel):
    today: UsageWindow
    last_7d: UsageWindow
    all_time: UsageWindow


class Overview(BaseModel):
    """Everything the panel's landing page renders."""

    access_mode: str
    model: str
    reasoning_effort: str
    #: The prompt this process would run right now — read from `app/agent/prompts`, never a
    #: literal. v1 shipped a banner naming a model and a prompt it did not run.
    prompt_version: str
    prompt_sha256: str | None
    dataset_sha256: str | None
    usage: Usage


class SettingOut(BaseModel):
    """One runtime setting as it is now IN EFFECT — read back after the write, not echoed.

    `source` is `db` when a superuser has overridden the environment and `default` when the
    env layer in `app/settings.py` is still in charge.
    """

    key: str
    value: str
    source: str
    updated_at: datetime | None = None
    updated_by: int | None = None


class SettingRequest(BaseModel):
    """Only the SHAPE is checked here; the VALUES belong to `runtime_settings`.

    So `key` is a plain `str` rather than the `RuntimeKey` Literal and neither field has a
    minimum length: duplicating the vocabulary or the emptiness rule would create a second
    place for them to drift, and it would answer 422 where the panel expects the 400 that
    carries `runtime_settings`' own explanation. A missing or non-string field is still a
    422, and the maxima are a body-size guard, not a validation rule.
    """

    key: str = Field(max_length=64)
    value: str = Field(max_length=256)


class AdminUser(BaseModel):
    id: int
    username: str
    display_name: str
    kind: Literal["human", "guest"]
    is_active: bool
    is_superuser: bool
    created_at: datetime
    last_login_at: datetime | None = None
    #: Written at guest-mint time and then at most once per sliding-refresh window; see
    #: `app/auth/guest.py::touch_last_seen`. Null means "not seen since 0005 landed".
    last_seen_at: datetime | None = None
    classifications: int


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=4096)
    display_name: str | None = Field(default=None, max_length=256)
    is_superuser: bool = False


class PasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=4096)


class ActiveRequest(BaseModel):
    is_active: bool


class SuperuserRequest(BaseModel):
    is_superuser: bool


class ModelOption(BaseModel):
    """A model the panel may offer, with what it costs.

    `app/records/pricing.py` is the only place in this codebase that knows a price, so it is
    the only source for this list — the OpenAI API is never called to build it. A model that
    is configured but absent from that table comes back with null prices rather than being
    hidden: `price_usd` returns None for it, so every turn it runs is recorded with a NULL
    `cost_usd`, and a panel that silently omitted it would hide exactly that.
    """

    id: str
    selected: bool
    input_per_mtok: float | None
    cached_input_per_mtok: float | None
    output_per_mtok: float | None
    pricing_version: str


# ---------------------------------------------------------------------------- Ukrainian copy

_NO_SUCH_USER: Final = "Користувача не знайдено."
_DUPLICATE_USERNAME: Final = "Користувач із таким іменем уже існує."
_SELF_DEMOTE: Final = (
    "Не можна зняти права суперкористувача із себе — інакше вхід до панелі буде втрачено."
)
_SELF_DEACTIVATE: Final = "Не можна деактивувати власний обліковий запис."
_LAST_SUPERUSER: Final = (
    "Це останній активний суперкористувач: його не можна ні позбавити прав, ні вимкнути."
)
_GUEST_NO_SUPERUSER: Final = "Гостьовий обліковий запис не може бути суперкористувачем."
_GUEST_NO_PASSWORD: Final = "Гостьовий обліковий запис не має пароля і не може його мати."


# ---------------------------------------------------------------------------- usage

_USAGE = """
SELECT count(*)                                                        AS classifications,
       coalesce(sum(c.cost_usd), 0)                                    AS cost_usd,
       coalesce(sum(c.tokens_in), 0)                                   AS tokens_in,
       coalesce(sum(c.tokens_cached), 0)                               AS tokens_cached,
       coalesce(sum(c.tokens_out), 0)                                  AS tokens_out,
       count(*) FILTER (WHERE c.cost_usd IS NULL)                      AS runs_unpriced,
       count(*) FILTER (WHERE c.outcome IN ('classified', 'conversation'))
                                                                       AS classified,
       count(*) FILTER (WHERE c.outcome = 'clarification')             AS clarification,
       count(*) FILTER (WHERE c.outcome = 'error')                     AS error,
       count(*) FILTER (WHERE c.outcome = 'pending')                   AS pending,
       count(DISTINCT c.user_id)                                       AS users,
       count(DISTINCT c.user_id) FILTER (WHERE u.kind = 'human')       AS human_users,
       count(DISTINCT c.user_id) FILTER (WHERE u.kind = 'guest')       AS guest_users,
       count(*) FILTER (WHERE u.kind = 'human')                        AS human_classifications,
       count(*) FILTER (WHERE u.kind = 'guest')                        AS guest_classifications
  FROM classification c
  JOIN app_user u ON u.id = c.user_id
 WHERE c.created_at >= $1
"""
"""One window of the dashboard. Run once per window, with the boundary as a parameter.

The boundaries are computed in Python rather than with `date_trunc('day', now())` so that
"today" means 00:00 UTC and not "whatever the database session's TimeZone happens to be" —
a setting no one in this deployment ever chose, and the difference between an honest number
and one that silently shifts by two hours when the container's locale changes.
"""


def _money(value: Any) -> float:
    """NUMERIC → the wire. `Decimal` for the quantisation, `float` for the JSON."""
    return float(Decimal(str(value or 0)).quantize(Decimal(10) ** -_MONEY_DP))


async def _usage_window(since: datetime, *, report_since: bool) -> UsageWindow:
    row = await get_pool().fetchrow(_USAGE, since)
    # A pool that answers nothing at all (no double in the suite does, but a `fetchrow` is
    # typed Optional) is reported as an empty window rather than a 500 on the landing page.
    data: dict[str, Any] = dict(row) if row is not None else {}
    return UsageWindow(
        since=since if report_since else None,
        classifications=data.get("classifications", 0),
        cost_usd=_money(data.get("cost_usd")),
        runs_unpriced=data.get("runs_unpriced", 0) or 0,
        tokens_in=data.get("tokens_in", 0) or 0,
        tokens_cached=data.get("tokens_cached", 0) or 0,
        tokens_out=data.get("tokens_out", 0) or 0,
        outcomes=OutcomeMix(
            classified=data.get("classified", 0),
            clarification=data.get("clarification", 0),
            error=data.get("error", 0),
            pending=data.get("pending", 0),
        ),
        users=data.get("users", 0),
        human_users=data.get("human_users", 0),
        guest_users=data.get("guest_users", 0),
        human_classifications=data.get("human_classifications", 0),
        guest_classifications=data.get("guest_classifications", 0),
    )


async def _live_prompt(request: Request) -> tuple[str | None, str | None]:
    """`(prompt_sha256, dataset_sha256)` for the prompt THIS process would render now.

    Both come off `app.state.server`, which caches the section catalogue and the active
    dataset for the life of the process — so this costs one `render_system_prompt` and no
    queries. Degrading to `(None, None)` rather than raising is deliberate: the usage
    dashboard is the reason the author opens this page, and a tariff that is mid-ingest must
    not take it down. `/readyz` is where an unavailable dataset is supposed to be loud.
    """
    server = getattr(request.app.state, "server", None)
    if server is None:
        return None, None
    try:
        _, prompt_sha = render_system_prompt(
            catalogue=await server.section_catalogue(), locale=_PROMPT_LOCALE
        )
        return prompt_sha, await server.dataset_sha256()
    except Exception:
        logger.warning("live prompt unavailable for the admin overview", exc_info=True)
        return None, None


@router.get("/overview", response_model=Overview)
async def overview(request: Request) -> Overview:
    """What this deployment is running, and what it has cost.

    `get_all_runtime()` is the uncached read on purpose — the panel must never show a
    superuser a value three seconds behind the one they just saved.
    """
    settings = await get_all_runtime()
    prompt_sha256, dataset_sha256 = await _live_prompt(request)

    now = datetime.now(UTC)
    return Overview(
        access_mode=settings["access_mode"].value,
        model=settings["model"].value,
        reasoning_effort=settings["reasoning_effort"].value,
        prompt_version=PROMPT_VERSION,
        prompt_sha256=prompt_sha256,
        dataset_sha256=dataset_sha256,
        usage=Usage(
            today=await _usage_window(
                now.replace(hour=0, minute=0, second=0, microsecond=0), report_since=True
            ),
            last_7d=await _usage_window(now - timedelta(days=7), report_since=True),
            all_time=await _usage_window(_ALL_TIME, report_since=False),
        ),
    )


# ---------------------------------------------------------------------------- settings


@router.put("/settings", response_model=SettingOut)
async def update_setting(
    body: SettingRequest,
    actor: Annotated[CurrentUser, Depends(require_superuser)],
) -> SettingOut:
    """Change one runtime setting. The access-mode kill switch is this route.

    All validation lives in `set_runtime` — the key set, the access-mode vocabulary, the
    reasoning-effort vocabulary and the shape of a model id — because the read path in
    `app/runtime_settings.py` validates against the same function, and a second copy here
    would be a second thing to keep in step. A rejected value is a 400 carrying that
    module's message, which names the key and lists what was allowed.

    The response is read BACK through `get_all_runtime()` after the write rather than echoed
    from the request. That is what makes it worth trusting: it proves the row landed and
    that the process cache was dropped, so "the switch is now private" is a statement about
    the database and not about the request body.
    """
    # The cast asserts nothing: `set_runtime` checks the key against `RUNTIME_KEYS` before it
    # touches the database, and an unknown one leaves here as the 400 below.
    key = cast(RuntimeKey, body.key)
    try:
        await set_runtime(key, body.value, updated_by=actor.id)
    except InvalidRuntimeValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    resolved = (await get_all_runtime())[key]
    logger.info("%s set %s=%r", actor.username, resolved.key, resolved.value)
    return SettingOut(
        key=resolved.key,
        value=resolved.value,
        source=resolved.source,
        updated_at=resolved.updated_at,
        updated_by=resolved.updated_by,
    )


# ---------------------------------------------------------------------------- users

_USER_SELECT = """
SELECT u.id, u.username, u.display_name, u.kind, u.is_active, u.is_superuser,
       u.created_at, u.last_login_at, u.last_seen_at,
       count(c.id) AS classifications
  FROM app_user u
  LEFT JOIN classification c ON c.user_id = u.id
"""

_USER_LIST = (
    _USER_SELECT
    + """
 WHERE ($1::text IS NULL OR u.kind = $1)
 GROUP BY u.id
 ORDER BY (u.kind = 'guest'),
          CASE WHEN u.kind = 'guest' THEN NULL ELSE u.id END ASC NULLS LAST,
          coalesce(u.last_seen_at, u.last_login_at, u.created_at) DESC,
          u.id DESC
 LIMIT $2
"""
)
"""Humans first in the order they were created, then guests by most-recent activity.

Three sort keys, and the first one does the work: booleans order false-before-true, so every
human precedes every guest. The second key is nulled out for guests, which makes it a
no-op inside that group and leaves the recency key to order them — guests have no meaningful
creation order to the author, only "who is here now".
"""

_USER_ONE = _USER_SELECT + " WHERE u.id = $1 GROUP BY u.id"


async def _load_user(user_id: int) -> AdminUser:
    row = await get_pool().fetchrow(_USER_ONE, user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NO_SUCH_USER)
    return AdminUser(**dict(row))


async def _require_user(user_id: int) -> Any:
    """The target of a mutation, or 404. Read before the rails so they can be specific."""
    row = await get_pool().fetchrow(
        "SELECT id, username, kind, is_active, is_superuser FROM app_user WHERE id = $1",
        user_id,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NO_SUCH_USER)
    return row


@router.get("/users", response_model=list[AdminUser])
async def list_users(
    kind: Annotated[Literal["human", "guest"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[AdminUser]:
    """Every account, newest-active guests first. Guests outnumber humans, hence the limit."""
    rows = await get_pool().fetch(_USER_LIST, kind, limit)
    return [AdminUser(**dict(row)) for row in rows]


@router.post("/users", response_model=AdminUser, status_code=status.HTTP_201_CREATED)
async def create_user(body: CreateUserRequest) -> AdminUser:
    """Create a human account. There is no route that creates a guest.

    A guest is minted by `app/auth/guest.py` only when a real browser arrives without a
    session, and a guest row with no browser holding its cookie is an orphan — so making one
    from this panel would be making garbage.
    """
    username = body.username.strip()
    if not username:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ім'я користувача не може бути порожнім.")

    # argon2 is ~80 ms of CPU. Off the event loop, or every concurrent SSE stream in this
    # worker stalls for the duration.
    password_hash = await asyncio.to_thread(hash_password, body.password)

    try:
        user_id = await get_pool().fetchval(
            """
            INSERT INTO app_user (username, password_hash, display_name, kind, is_superuser)
                 VALUES ($1, $2, $3, 'human', $4)
              RETURNING id
            """,
            username,
            password_hash,
            body.display_name or username,
            body.is_superuser,
        )
    except asyncpg.UniqueViolationError as exc:
        # `username` is CITEXT, so the collision is case-insensitive and so is this message.
        raise HTTPException(status.HTTP_409_CONFLICT, _DUPLICATE_USERNAME) from exc

    logger.info("created user %r (id=%s, superuser=%s)", username, user_id, body.is_superuser)
    return await _load_user(user_id)


_SET_PASSWORD = """
UPDATE app_user
   SET password_hash = $2, session_epoch = session_epoch + 1
 WHERE id = $1 AND kind = 'human'
RETURNING id
"""
"""`session_epoch + 1` is what turns a password change into a logout everywhere.

`current_user` compares the epoch in the signed cookie against this column on every request,
so every outstanding session for this user dies on its next one. `kind = 'human'` is
repeated here even though the handler already checked it: this statement is the one that
runs, and the check belongs where the write is.
"""


@router.post("/users/{user_id}/password", response_model=AdminUser)
async def set_password(user_id: int, body: PasswordRequest) -> AdminUser:
    """Set a human's password and log them out everywhere.

    A guest is refused: its `password_hash` is `GUEST_PASSWORD_SENTINEL`, deliberately not
    an argon2 string, which is what makes "a guest can never log in" a property of the row
    rather than a rule someone has to remember. Writing a real hash there would quietly
    convert a guest into an account.
    """
    target = await _require_user(user_id)
    if target["kind"] != "human":
        raise HTTPException(status.HTTP_409_CONFLICT, _GUEST_NO_PASSWORD)

    password_hash = await asyncio.to_thread(hash_password, body.password)
    updated = await get_pool().fetchval(_SET_PASSWORD, user_id, password_hash)
    if updated is None:  # pragma: no cover - the row changed kind between the two statements
        raise HTTPException(status.HTTP_409_CONFLICT, _GUEST_NO_PASSWORD)

    logger.info("password changed for user %s; sessions invalidated", user_id)
    return await _load_user(user_id)


_SET_ACTIVE = """
UPDATE app_user
   SET is_active = $2::boolean,
       session_epoch = session_epoch + CASE WHEN $2::boolean THEN 0 ELSE 1 END
 WHERE id = $1
   AND ($2::boolean
        OR NOT (is_superuser AND is_active)
        OR EXISTS (SELECT 1 FROM app_user a
                    WHERE a.is_superuser AND a.is_active AND a.kind = 'human' AND a.id <> $1))
RETURNING id
"""
"""Deactivating revokes; reactivating does not.

The epoch is bumped only on the way down, because that is the half that has to take effect
immediately — disabling an account whose cookie stays valid is advisory, not enforced.
Re-enabling deliberately does not bump: it would log the user out of a session they were
never told was ending.

The `EXISTS` is the last-superuser rail, enforced inside the statement that does the write.
Doing it in one statement rather than as a `SELECT count(*)` followed by an `UPDATE` is what
makes it safe against the real race: two superusers disabling each other at the same moment
would both read "there is another one" and both write, and the demo would end with nobody
able to reach the panel.
"""


@router.post("/users/{user_id}/active", response_model=AdminUser)
async def set_active(
    user_id: int,
    body: ActiveRequest,
    actor: Annotated[CurrentUser, Depends(require_superuser)],
) -> AdminUser:
    """Enable or disable an account. Disabling kills its sessions on the next request.

    Two rails, and the reason for both is the same: this panel has no shell behind it. A
    superuser who locks themselves out of a running demo cannot get back in without
    `docker compose exec`, which is exactly the situation the panel exists to avoid.
    """
    target = await _require_user(user_id)
    if not body.is_active and target["id"] == actor.id:
        raise HTTPException(status.HTTP_409_CONFLICT, _SELF_DEACTIVATE)

    updated = await get_pool().fetchval(_SET_ACTIVE, user_id, body.is_active)
    if updated is None:
        # The statement's own guard refused: this row is the last active superuser. (A row
        # deleted between the two statements lands here too — same 409, and the panel's next
        # refresh shows it gone.)
        raise HTTPException(status.HTTP_409_CONFLICT, _LAST_SUPERUSER)

    logger.info("user %s is_active=%s (by %s)", user_id, body.is_active, actor.username)
    return await _load_user(user_id)


_SET_SUPERUSER = """
UPDATE app_user
   SET is_superuser = $2::boolean
 WHERE id = $1
   AND (NOT $2::boolean OR kind = 'human')
   AND ($2::boolean
        OR NOT (is_superuser AND is_active)
        OR EXISTS (SELECT 1 FROM app_user a
                    WHERE a.is_superuser AND a.is_active AND a.kind = 'human' AND a.id <> $1))
RETURNING id
"""
"""Two guards, both in the statement: a guest can never be granted, and the last active
superuser can never be demoted.

`session_epoch` is deliberately NOT bumped — `require_superuser` re-reads the row on every
request, so a revoked admin loses the panel on their very next click, and logging them out
of the chat as well would be a side effect nobody asked for. `app/cli.py::_set_superuser`
makes the same choice for the same reason.
"""


@router.post("/users/{user_id}/superuser", response_model=AdminUser)
async def set_superuser(
    user_id: int,
    body: SuperuserRequest,
    actor: Annotated[CurrentUser, Depends(require_superuser)],
) -> AdminUser:
    """Grant or revoke the admin panel.

    A guest can never be granted it. That is already true three times over — `is_superuser`
    defaults to false, the mint never sets it, and `require_superuser` checks
    `kind == 'human'` on top of the flag — and it is refused a fourth time here, because
    this is the only route in the application that can set the column at all.
    """
    target = await _require_user(user_id)
    if body.is_superuser and target["kind"] != "human":
        raise HTTPException(status.HTTP_409_CONFLICT, _GUEST_NO_SUPERUSER)
    if not body.is_superuser and target["id"] == actor.id:
        raise HTTPException(status.HTTP_409_CONFLICT, _SELF_DEMOTE)

    updated = await get_pool().fetchval(_SET_SUPERUSER, user_id, body.is_superuser)
    if updated is None:
        raise HTTPException(status.HTTP_409_CONFLICT, _LAST_SUPERUSER)

    logger.info("user %s is_superuser=%s (by %s)", user_id, body.is_superuser, actor.username)
    return await _load_user(user_id)


# ---------------------------------------------------------------------------- models


@router.get("/models", response_model=list[ModelOption])
async def list_models() -> list[ModelOption]:
    """The models the panel may offer, priced from `app/records/pricing.py`.

    That table is the only place this codebase knows a price, so it is the only place this
    list can come from — and it is the right constraint rather than a limitation: switching
    to a model that is not in it means every subsequent turn records `cost_usd = NULL`, and
    the usage dashboard, which is the author's only spend visibility, goes blind. Offering
    only priced models makes that impossible by construction.
    """
    current = (await get_all_runtime())["model"].value
    options = [
        ModelOption(
            id=model_id,
            selected=model_id == current,
            input_per_mtok=float(price.input_per_mtok),
            cached_input_per_mtok=float(price.cached_input_per_mtok),
            output_per_mtok=float(price.output_per_mtok),
            pricing_version=PRICING_VERSION,
        )
        for model_id, price in sorted(MODEL_PRICES.items())
    ]
    if current not in MODEL_PRICES:
        # Configured through the environment, unknown to the price table. Shown with null
        # prices and first, so the panel reports the truth — "this is running and we cannot
        # cost it" — instead of rendering nothing as selected.
        options.insert(
            0,
            ModelOption(
                id=current,
                selected=True,
                input_per_mtok=None,
                cached_input_per_mtok=None,
                output_per_mtok=None,
                pricing_version=PRICING_VERSION,
            ),
        )
    return options


__all__ = ["router"]
