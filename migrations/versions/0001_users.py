"""app_user — the only identity in the system.

Revision ID: 0001_users
Revises:
Create Date: 2026-09-16

There is NO self-registration path: no /register route exists anywhere in the app, and the
only INSERT into this table lives in `app/cli.py`. The absence of the route IS the
requirement — there is nothing to misconfigure.

`session_epoch` is what makes a stateless signed cookie revocable. The login stores the
current epoch in the cookie; `current_user()` compares it against this row on every
request; bumping it logs that user out everywhere. Without it `disable-user` would be
advisory only, which is the wrong property for the counterpart of "users are created by a
management command".
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_users"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # CITEXT makes "usernames are case-insensitively unique" a property of the schema
    # rather than a convention every call site has to remember to apply lower() to.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "app_user",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("username", postgresql.CITEXT(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),  # argon2id, from app.auth.passwords
        sa.Column("display_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        # Reserved for the feedback/correction path (§9): only a reviewer may attach a
        # corrected_code. Declared here so the records migration never has to ALTER this
        # table while the app is running.
        sa.Column("is_reviewer", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("session_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_login_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("username", name="app_user_username_key"),
    )


def downgrade() -> None:
    op.drop_table("app_user")
    # citext is deliberately NOT dropped: other schemas in the same database may use it,
    # and DROP EXTENSION would take their columns with it.
