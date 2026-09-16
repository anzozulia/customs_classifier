"""One asyncpg pool for the whole process.

v1 opened a brand-new connection at 15 separate call sites and pooled nothing: every tool
call paid a fresh TCP + auth round trip, and a connection leak was one `except:` away.
Here there is exactly one pool, created in the FastAPI lifespan and closed on shutdown.

Deliberately simple: asyncpg's `Pool` IS the data-access object. There is no repository
class wrapping it, no session factory, no unit-of-work. The SQL lives next to the code
that needs it (`app/chat/store.py`, `app/auth/deps.py`, `app/cli.py`), which for a
four-table schema is fewer moving parts, not more.

Note on JSON: no jsonb codec is installed on purpose. asyncpg hands JSONB back as `str`,
which is exactly what pydantic's `model_validate_json` wants — decoding to a dict here
just to re-encode it there would be pure waste.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

_pool: asyncpg.Pool | None = None

# "postgresql+asyncpg://" is SQLAlchemy/Alembic spelling. asyncpg only speaks libpq URLs,
# and DATABASE_URL is shared with the migration runner, so normalise instead of demanding
# two env vars that must be kept in sync.
_SQLA_DRIVER = re.compile(r"^(postgres(?:ql)?)\+\w+://")


class PoolNotInitialisedError(RuntimeError):
    """get_pool() was called before init_pool(). Always a wiring bug, never a runtime state."""


def normalise_dsn(dsn: str) -> str:
    return _SQLA_DRIVER.sub(r"\1://", dsn)


async def init_pool(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 10,
    command_timeout: float = 30.0,
) -> asyncpg.Pool:
    """Create the process-wide pool. Idempotent: a second call returns the first pool.

    max_size=10 is sized for the deployment target (one uvicorn worker, ~22 users), and
    every query in this app is a single-row or single-page lookup measured in milliseconds.
    command_timeout is the bound v1 never had anywhere: a wedged query fails the turn
    instead of holding a connection until the 600 s driver default.
    """
    global _pool
    if _pool is not None:
        return _pool
    _pool = await asyncpg.create_pool(
        normalise_dsn(dsn),
        min_size=min_size,
        max_size=max_size,
        command_timeout=command_timeout,
        max_inactive_connection_lifetime=300.0,
    )
    return _pool


async def close_pool() -> None:
    """Close the pool and forget it. Safe to call when there is no pool."""
    global _pool
    pool, _pool = _pool, None
    if pool is not None:
        await pool.close()


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise PoolNotInitialisedError(
            "database pool is not initialised — call init_pool(dsn) in the app lifespan"
        )
    return _pool


@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    """Borrow a connection. Only needed for multi-statement work that must share a
    transaction; single statements should go straight through `get_pool().fetch*()`,
    which acquires and releases for you."""
    async with get_pool().acquire() as conn:
        yield conn
