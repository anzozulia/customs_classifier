"""The ChatKit transcript: chat_thread · chat_thread_item · chat_attachment.

Revision ID: 0002_chatkit_store
Revises: 0001_users
Create Date: 2026-09-16

Shape, and why:

* The whole `ThreadMetadata` / `ThreadItem` document is stored in `payload` JSONB, and only
  the fields we query on are denormalised into columns. Those pydantic models gain fields
  between SDK releases; a column-per-field schema silently drops them on upgrade.

* `chat_thread_item.user_id` is DENORMALISED ON PURPOSE. Every authorization check in
  `app/chat/store.py` is then a single-table predicate (`WHERE thread_id = $1 AND user_id = $2`)
  with no join to forget. `ChatKitServer` performs zero authorization and hands the Store a
  client-supplied thread_id, so this predicate is the entire multi-user boundary — it must be
  impossible to write a query that accidentally omits it.

* `seq` is a database-assigned identity, i.e. a global monotonic counter rather than a
  per-thread one. It still gives a total order within any single thread (that is all
  pagination needs), while a per-thread MAX(seq)+1 would be a read-modify-write race.
  UNIQUE (thread_id, seq) documents the intent and is satisfied for free.
  Ordering by `seq` instead of by `created_at` also removes the tie-break problem: ChatKit
  timestamps items with `datetime.now()`, and two items written in the same microsecond
  would otherwise paginate non-deterministically.

* Deletes are hard deletes with ON DELETE CASCADE — no `deleted_at`. A soft-delete column
  means every one of the 14 Store methods has a second predicate it can forget, for a
  feature ("undelete a conversation") nobody asked for.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_chatkit_store"
down_revision = "0001_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_thread",
        # 'thr_' + 24 random bytes, minted by PgStore.generate_thread_id.
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # Full ThreadMetadata.model_dump(mode="json").
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # Keyset pagination index, matching ORDER BY (created_at, id) in BOTH directions.
    op.create_index(
        "chat_thread_user_created_idx",
        "chat_thread",
        ["user_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )

    op.create_table(
        "chat_thread_item",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "thread_id",
            sa.Text(),
            sa.ForeignKey("chat_thread.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Denormalised on purpose — see the module docstring.
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        # The ThreadItem discriminator: user_message, assistant_message, widget, workflow, …
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        # Also the index that serves every item query: (thread_id, seq) with a user_id filter.
        sa.UniqueConstraint("thread_id", "seq", name="chat_thread_item_thread_seq_key"),
    )

    op.create_table(
        "chat_attachment",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # v2 ships with attachment_store=None, so this table stays empty. It exists because the
    # three attachment methods are @abstractmethod on chatkit.store.Store: the class will not
    # instantiate without them, and a `pass` stub is a latent cross-user read the day anyone
    # switches uploads on.


def downgrade() -> None:
    op.drop_table("chat_attachment")
    op.drop_table("chat_thread_item")
    op.drop_index("chat_thread_user_created_idx", table_name="chat_thread")
    op.drop_table("chat_thread")
