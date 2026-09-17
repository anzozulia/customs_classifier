"""The override layer over `app/settings.py`: settings the admin panel can change live.

Three keys, and deliberately only three:

    access_mode        'public' | 'private'    — the kill switch
    model              str                     — which OpenAI model runs a turn
    reasoning_effort   'none' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max'

`app/settings.py` stays the DEFAULT layer: env-backed, immutable for the life of the
process, and the answer whenever `app_setting` has no row for a key. This module is the
OVERRIDE layer: a row in `app_setting` wins, and the admin panel is the only thing that
writes one. Migration 0005 seeds nothing, so a fresh database resolves every key to its env
default and the two layers never disagree about who is in charge.

`access_mode` is the exception that has no env default at all. It resolves to 'private' when
the table is empty, hardcoded, because this is the switch that decides whether the internet
at large can spend the author's OpenAI credit. Every other failure in this module — an
unreadable row, a value someone hand-edited into nonsense, a database that will not answer —
also resolves to 'private'. There is exactly one way to open this deployment up, and it is
an explicit, validated write by a superuser.

**Validation runs twice, on purpose.** `set_runtime` rejects a bad value at write time,
which is where a human can still be told why. But a row can also arrive by `psql`, and a
`model` of `"gpt-5.6-terra; DROP"` or a `reasoning_effort` of `"ultra"` would fail every
turn in this deployment until someone noticed. So the read path validates too, logs loudly,
and falls back to the default. A hand-edited row degrades this app to its env configuration;
it does not brick it.

**Caching.** `model` and `reasoning_effort` are read on the hot path — once per turn, next
to a 60-second model call — and are served from a 3-second in-process TTL cache. Three
seconds is chosen against what it costs to be wrong: a turn that starts with the previous
model for up to three seconds after a change is invisible, while a `SELECT` per turn is a
round trip nobody needs. Under multiple uvicorn workers each worker holds its own cache, so
a change CONVERGES within the TTL rather than applying instantly; that is stated here rather
than hidden, and for a demo with one worker it is exactly zero.

`access_mode` does NOT use that cache, ever. It is the kill switch: the author flips it to
'private' to stop guest traffic, and "stops within 3 seconds, per worker" is a worse promise
than the one line of code it takes to avoid making it. `get_access_mode()` issues a primary-
key lookup on a three-row table on the auth path — a path that was already going to
`SELECT` the user row on the same connection pool in the same millisecond. Two single-row
lookups instead of one buys an exact, cluster-wide kill switch, and that is the trade this
module makes.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, get_args

from app.db import get_pool
from app.settings import Settings, get_settings

log = logging.getLogger(__name__)

RuntimeKey = Literal["access_mode", "model", "reasoning_effort"]
AccessMode = Literal["public", "private"]
# Every value the Responses API accepts, per the reasoning guide. Some models support only
# a SUBSET — an unsupported pairing is a 400 from the provider, which classify_error maps to
# upstream_4xx and the user sees as a plain failure rather than a crash. We do not hard-code
# per-model matrices here: they are undocumented per variant and would rot silently.
# NOTE: this agent uses a hosted WebSearchTool, and OpenAI's web-search guide warns that
# "none" degrades search quality. It is offered because the API offers it; the panel labels
# it as discouraged rather than hiding a real capability.
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]

RUNTIME_KEYS: Final[tuple[RuntimeKey, ...]] = get_args(RuntimeKey)
ACCESS_MODES: Final[tuple[AccessMode, ...]] = get_args(AccessMode)
REASONING_EFFORTS: Final[tuple[ReasoningEffort, ...]] = get_args(ReasoningEffort)

# Fail closed. This is not read from the environment on purpose: "the demo is open because
# someone left a stale line in a .env file" is not a state this deployment can reach.
DEFAULT_ACCESS_MODE: Final[AccessMode] = "private"

# Conservative on purpose. Every published OpenAI model id — 'gpt-5.6-terra', 'o4-mini',
# 'gpt-4o-2024-08-06', and the 'ft:base:org::id' form of a fine-tune — fits this, and
# nothing with a space, a quote, a slash or a newline does. The value ends up in an API
# request and in `classification.model`, so the set of accepted shapes stays small.
_MODEL_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

# See the module docstring: 3 s is the convergence window for model / reasoning_effort under
# multiple workers. access_mode never reads it.
_CACHE_TTL_S: Final = 3.0


class InvalidRuntimeValueError(ValueError):
    """A runtime setting was written with a value this module refuses to store."""


@dataclass(frozen=True, slots=True)
class ResolvedSetting:
    """One key, resolved, plus where the answer came from.

    `source` is what the admin panel renders: "db" means a superuser set this and it
    overrides the environment, "default" means nobody has ever set it and the env is in
    charge. A row whose value failed read-time validation reports "default" too — because
    that is genuinely what is in effect — and is logged at WARNING when it is read.
    """

    key: RuntimeKey
    value: str
    source: Literal["db", "default"]
    updated_at: datetime | None = None
    updated_by: int | None = None


# --------------------------------------------------------------------------------------
# Validation — the point of this module
# --------------------------------------------------------------------------------------


def validate_runtime_value(key: RuntimeKey, value: Any) -> str:
    """Return the canonical value for `key`, or raise `InvalidRuntimeValueError`.

    Shared by the write path and the read path so that "what is a legal model id" has one
    definition. Whitespace is stripped rather than rejected: a value pasted into an admin
    form with a trailing space is a typo, not an attack, and silently fixing it is kinder
    than a validation error the author has to squint at.
    """
    if key not in RUNTIME_KEYS:
        raise InvalidRuntimeValueError(f"unknown runtime setting: {key!r}")
    if not isinstance(value, str):
        raise InvalidRuntimeValueError(f"{key} must be a string, got {type(value).__name__}")

    cleaned = value.strip()
    if not cleaned:
        raise InvalidRuntimeValueError(f"{key} must not be empty")

    if key == "access_mode":
        if cleaned not in ACCESS_MODES:
            raise InvalidRuntimeValueError(
                f"access_mode must be one of {', '.join(ACCESS_MODES)}; got {cleaned!r}"
            )
    elif key == "reasoning_effort":
        if cleaned not in REASONING_EFFORTS:
            raise InvalidRuntimeValueError(
                f"reasoning_effort must be one of {', '.join(REASONING_EFFORTS)}; got {cleaned!r}"
            )
    elif not _MODEL_RE.match(cleaned):
        raise InvalidRuntimeValueError(
            f"model must match {_MODEL_RE.pattern} (letters, digits, . _ - :); got {cleaned!r}"
        )

    return cleaned


def default_for(key: RuntimeKey) -> str:
    """The env/settings answer for a key, itself validated.

    The environment is configuration too, and `REASONING_EFFORT=ultra` in a .env file is the
    same bug as `reasoning_effort = 'ultra'` in the database. When the env value does not
    validate, this falls back one further step to the field default declared on `Settings`,
    which is a literal in the source and therefore cannot be wrong.
    """
    if key == "access_mode":
        return DEFAULT_ACCESS_MODE

    settings = get_settings()
    configured = settings.model if key == "model" else settings.reasoning_effort
    try:
        return validate_runtime_value(key, configured)
    except InvalidRuntimeValueError as exc:
        floor = str(Settings.model_fields[key].default)
        log.warning(
            "environment value for %s is invalid (%s); falling back to the built-in %r",
            key,
            exc,
            floor,
        )
        return floor


# --------------------------------------------------------------------------------------
# The TTL cache
# --------------------------------------------------------------------------------------

_cache: dict[str, ResolvedSetting] | None = None
_cache_expires_at: float = 0.0


def invalidate_cache() -> None:
    """Drop the cached snapshot. Called by `set_runtime`, and by tests."""
    global _cache, _cache_expires_at
    _cache = None
    _cache_expires_at = 0.0


def _parse_value(key: RuntimeKey, raw: Any) -> str | None:
    """A stored JSONB value → a validated string, or None when the row is unusable.

    asyncpg hands JSONB back as `str` (no codec is installed — see `app/db.py`), so the
    normal path is a `json.loads`. The isinstance check covers a future codec, and a
    non-string JSON value (a number, an object) falls through validation like any other bad
    value.
    """
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        log.warning("app_setting[%s] is not valid JSON; using the default", key)
        return None

    try:
        return validate_runtime_value(key, value)
    except InvalidRuntimeValueError as exc:
        log.warning("app_setting[%s] holds an invalid value (%s); using the default", key, exc)
        return None


def _resolve_row(key: RuntimeKey, row: Any) -> ResolvedSetting:
    if row is not None:
        parsed = _parse_value(key, row["value"])
        if parsed is not None:
            return ResolvedSetting(
                key=key,
                value=parsed,
                source="db",
                updated_at=row["updated_at"],
                updated_by=row["updated_by"],
            )
    return ResolvedSetting(key=key, value=default_for(key), source="default")


async def _load_snapshot() -> dict[str, ResolvedSetting]:
    """Resolve all three keys in one query. Three rows; there is nothing to paginate."""
    try:
        rows = await get_pool().fetch("SELECT key, value, updated_at, updated_by FROM app_setting")
    except Exception:
        # A database that will not answer resolves every key to its default, which for
        # access_mode is 'private'. Failing closed here matters more than propagating: the
        # caller is the auth path, and the alternative to a default is a 500 on every route.
        log.exception("could not read app_setting; falling back to environment defaults")
        rows = []

    by_key = {row["key"]: row for row in rows}
    return {key: _resolve_row(key, by_key.get(key)) for key in RUNTIME_KEYS}


async def _snapshot() -> dict[str, ResolvedSetting]:
    global _cache, _cache_expires_at
    now = time.monotonic()
    if _cache is not None and now < _cache_expires_at:
        return _cache
    _cache = await _load_snapshot()
    _cache_expires_at = now + _CACHE_TTL_S
    return _cache


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------


async def get_runtime(key: RuntimeKey) -> str:
    """The effective value for `key`: the database row if there is a valid one, else the
    environment default. Served from the 3 s TTL cache.

    `access_mode` callers on the auth path must use `get_access_mode()` instead — see the
    module docstring for why that one key skips the cache.
    """
    if key not in RUNTIME_KEYS:
        raise InvalidRuntimeValueError(f"unknown runtime setting: {key!r}")
    return (await _snapshot())[key].value


async def get_all_runtime() -> dict[RuntimeKey, ResolvedSetting]:
    """Every key, resolved, each tagged with its source. This is what the admin panel reads.

    Uncached: the panel is one superuser refreshing a page, and showing them a value up to
    three seconds behind the one they just saved would make the panel look broken.
    """
    snapshot = await _load_snapshot()
    return {key: snapshot[key] for key in RUNTIME_KEYS}


async def get_access_mode() -> AccessMode:
    """The kill switch, read straight from the database on every call. Never cached.

    One primary-key lookup on a three-row table, on a request that is already fetching the
    user row from the same pool. In exchange, flipping to 'private' stops guest traffic on
    the very next request, in every worker, with no convergence window to explain.
    """
    try:
        row = await get_pool().fetchrow(
            "SELECT key, value, updated_at, updated_by FROM app_setting WHERE key = 'access_mode'"
        )
    except Exception:
        log.exception("could not read access_mode; failing closed to %r", DEFAULT_ACCESS_MODE)
        return DEFAULT_ACCESS_MODE

    resolved = _resolve_row("access_mode", row)
    # The Literal narrowing is guaranteed by validate_runtime_value / DEFAULT_ACCESS_MODE.
    return resolved.value  # type: ignore[return-value]


async def set_runtime(key: RuntimeKey, value: Any, *, updated_by: int) -> None:
    """Validate and store an override, then drop the cache so the next read is exact.

    `updated_by` is the superuser's `app_user.id` and is required, not optional: with no
    spend caps and no rate limits in this deployment, "who opened this to the public and
    when" is information the author will want and cannot reconstruct afterwards.

    Raises `InvalidRuntimeValueError` before touching the database. Nothing else in this
    module can write a row, so the table cannot acquire a bad value through this process.
    """
    cleaned = validate_runtime_value(key, value)
    await get_pool().execute(
        """
        INSERT INTO app_setting (key, value, updated_at, updated_by)
             VALUES ($1, $2::jsonb, now(), $3)
        ON CONFLICT (key) DO UPDATE
                SET value = excluded.value,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
        """,
        key,
        json.dumps(cleaned),
        updated_by,
    )
    # Immediately, not on the next TTL tick: the superuser who just saved must see the new
    # value on the redirect back to the panel.
    invalidate_cache()
    log.info("runtime setting %s set to %r by user %s", key, cleaned, updated_by)
