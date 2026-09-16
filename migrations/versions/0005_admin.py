"""app_user.kind / is_superuser / last_seen_at, and app_setting — the runtime override layer.

Revision ID: 0005_admin
Revises: 0004_records
Create Date: 2026-09-17

Two ideas, one migration.

**A guest is a real `app_user` row.** Every isolation boundary in this application already
keys on `app_user.id` — all fourteen `PgStore` methods filter on `context.user_id`,
`classification.user_id` carries the audit record, `/api/history` scopes by it. Giving an
anonymous visitor a real row means every one of those boundaries keeps working with zero
changes, and a visitor's history survives a reload and a browser restart for exactly the
same reason a logged-in user's does. The alternative — a nullable `user_id`, or a parallel
`anonymous_session` table — would have forced an "or anonymous" branch into each of those
fourteen methods, which is fourteen places to get authorization wrong.

So `kind` is the ONLY new identity concept: 'human' (has a password, logs in) or 'guest'
(minted by `app/auth/guest.py`, holds an unusable password hash, can never log in). Both
columns land NOT NULL with a server default, which on PostgreSQL 11+ is a catalogue-only
change — no table rewrite, no lock held while every row is copied.

The partial index exists because guests outnumber humans by whatever the demo's traffic
turns out to be, against a handful of real accounts. `WHERE kind = 'guest'` is the exact
predicate of both queries that will ever be run against this column — the admin panel's
guest count and `python -m app.cli purge-guests` — so the index contains precisely the rows
those queries want and nothing else, and the count can be answered index-only.

**`app_setting` is the override layer over `app/settings.py`.** The author needs to flip
this deployment between `public` (open demo, no login) and `private` (login required) mid-
demo, without a redeploy, and to change the model and reasoning effort the same way. The
env file stays the DEFAULT layer; this table is the OVERRIDE layer.

Nothing is seeded here, deliberately. An ABSENT row means "use the env default", which is
what keeps the two layers honest: a seeded row would freeze whatever the env happened to
say on migration day into the database, and from then on editing `.env` would silently do
nothing. It also means `access_mode` defaults to 'private' (see `app/runtime_settings.py`)
on a fresh database — the safe direction to fail in, since the failure mode of the other
direction is "the demo is open to the internet and nobody noticed".

`value` is JSONB rather than TEXT so that the column can hold a non-string setting later
without a migration; today all three keys are strings, and `app/runtime_settings.py`
validates every read AND every write, because a hand-edited row must not brick the app.

`updated_by` is ON DELETE SET NULL, not CASCADE: deleting the superuser who flipped the
switch must not delete the switch.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_admin"
down_revision = "0004_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column("kind", sa.Text(), nullable=False, server_default="human"),
    )
    op.add_column(
        "app_user",
        sa.Column("is_superuser", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # NULL means "never seen since this column existed". Written at guest mint time and then
    # at most once per sliding-refresh window (one day) by `app/auth/deps.py` — a per-request
    # UPDATE would turn every read into a write and every session into a hot row.
    op.add_column(
        "app_user",
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    # The closed vocabulary, enforced by the schema rather than by every INSERT remembering.
    op.create_check_constraint(
        "app_user_kind_check", "app_user", sa.text("kind IN ('human', 'guest')")
    )

    op.create_index(
        "app_user_guest_idx",
        "app_user",
        ["kind"],
        postgresql_where=sa.text("kind = 'guest'"),
    )

    op.create_table(
        "app_setting",
        # Closed set, mirrored by the Literal in app/runtime_settings.py: access_mode, model,
        # reasoning_effort. An unknown key here is inert — nothing reads it.
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_by",
            sa.BigInteger(),
            sa.ForeignKey("app_user.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("app_setting")
    op.drop_index("app_user_guest_idx", table_name="app_user")
    op.drop_constraint("app_user_kind_check", "app_user", type_="check")
    op.drop_column("app_user", "last_seen_at")
    op.drop_column("app_user", "is_superuser")
    # Guest rows outlive the column that identified them; this downgrade does NOT delete
    # them, because dropping a column must not delete other tables' rows through a cascade.
    op.drop_column("app_user", "kind")
