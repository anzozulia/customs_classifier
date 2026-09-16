"""Alembic environment.

Three deliberate choices, all of them about staying out of the app's way:

1. The URL comes from ``DATABASE_URL`` in the environment, never from alembic.ini.
   One value configures the app and its migrations, and no credential is committed.

2. Alembic opens its **own short-lived synchronous connection**. It does not import
   ``app.db`` and does not borrow the asyncpg pool: migrations run in a one-shot
   container *before* any app process exists, so there is no pool to borrow, and DDL
   on an event loop buys nothing. ``DATABASE_URL`` is written for asyncpg, so the
   driver is swapped to psycopg here (see ``_sync_url``).

3. ``target_metadata`` is ``None``. There are no SQLAlchemy ORM models in this project
   — the schema is hand-written DDL and the request path is raw asyncpg — so
   ``--autogenerate`` is unavailable by construction rather than by accident.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No ORM models; see the module docstring.
target_metadata = None

# psycopg (v3) rather than psycopg2: binary wheels, still maintained, and the
# dialect SQLAlchemy 2.0 documents for new sync code.
SYNC_DRIVER = "psycopg"


def _sync_url() -> str:
    """DATABASE_URL, rewritten to address a synchronous driver.

    Accepts anything the rest of the project might set — ``postgresql://``,
    ``postgresql+asyncpg://``, ``postgres://`` — and always returns
    ``postgresql+psycopg://``. The URL object is passed to ``create_engine`` directly,
    so passwords containing ``%`` need no escaping.
    """
    raw = os.environ.get("DATABASE_URL", "").strip()
    if not raw:
        raise RuntimeError(
            "DATABASE_URL is not set. Alembic has no database to migrate. "
            "In compose it comes from .env; in a shell, export it first."
        )
    url = make_url(raw)
    if not url.get_backend_name().startswith("postgres"):
        raise RuntimeError(f"DATABASE_URL must be a PostgreSQL URL, got {url.get_backend_name()!r}")
    return url.set(drivername=f"postgresql+{SYNC_DRIVER}").render_as_string(hide_password=False)


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (``alembic upgrade head --sql``).

    Useful for a review of what a deploy is about to do; never used by the migrate
    container, which runs online.
    """
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Open one connection, run every pending revision, close it.

    NullPool because this process exists for a few seconds: a connection pool would
    only delay the exit.
    """
    engine = create_engine(_sync_url(), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
