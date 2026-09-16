"""classification · classification_code · classification_tool_call — the audit record.

Revision ID: 0004_records
Revises: 0003_tariff
Create Date: 2026-09-16

This is the table v1 did not have. v1 wrote a row only when a turn *succeeded*, so every
crashed, cancelled or wall-clocked turn was invisible, and its 1,403 classifications left
ZERO usable input/output pairs behind: no tokens, no latency, no tool trace, and no record
at all of the 29 turns that failed. The shape below is the fix, and three properties carry it:

* **The row is INSERTed `pending` BEFORE the model runs.** `app/records/writer.py` opens the
  record in `begin_turn()` and closes it in `finish_turn()` / `fail_turn()`. A turn that
  crashes between them stays `pending` with a NULL `finished_at`, which is a visible,
  queryable state rather than an absence. Everything past `input_text` is therefore NULLable:
  at INSERT time the answer genuinely does not exist yet.

* **`outcome` stores the LOSSLESS value, five states wide.** The agent layer's vocabulary is
  `result | clarification | conversation` (`app.agent.Outcome`) and the HTTP contract's is
  `classified | clarification | error | pending` (`web/src/lib/history.ts`). `result` is
  stored as `classified`, and `conversation` — a turn that answered in prose without ever
  reaching a terminal tool, 29/1,403 in v1 — is stored as itself and collapsed to
  `classified` only at the API boundary. Collapsing it here instead would make "how often
  does the agent chat instead of classifying?" unanswerable from the record, which is one of
  the few questions this table exists to answer. The CHECK is the closed vocabulary.

* **`id` is `secrets.token_urlsafe`, not a sequence.** It travels in `/history/:id` URLs; a
  BIGSERIAL would make one user's history a neighbourhood of someone else's.

`thread_id` is TEXT NULL and carries NO foreign key, deliberately, for two reasons: an eval
or CLI run is a classification with no chat thread at all, and deleting a conversation must
not delete the record of what was classified in it.

Money lives in `NUMERIC(10,6)` — never a float. It is computed by `app/records/pricing.py`
from a per-model table and stays NULL for a model the table does not know, because a
plausible-looking wrong cost is worse than a missing one.

`pg_trgm` is NOT created here: migration 0003 already creates it (and deliberately does not
drop it), and `classification_input_text_trgm_idx` is what makes `/api/history?q=` a search
rather than a sequential scan of every row the user ever wrote.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_records"
down_revision = "0003_tariff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "classification",
        # 'cls_' + 18 random bytes, minted by app/records/writer.py. Never sequential.
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # NULLable and unconstrained on purpose: an eval run has no chat thread, and deleting
        # a conversation must not take the record of what was classified in it.
        sa.Column("thread_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # NULL for as long as the turn is running. `outcome = 'pending' AND finished_at IS
        # NULL` is the abandoned-turn query v1 could not write.
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("clarification_question", sa.Text(), nullable=True),
        # The closed vocabulary from app/chat/errors.py, so GROUP BY error_class means
        # something. Not a CHECK: that list evolves with the client libraries, and a record
        # write must never be the thing that fails.
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        # Time to first streamed event — the number the user actually feels. v1's median cold
        # latency was 48.2 s behind one static string and nothing measured it.
        sa.Column("ttfb_ms", sa.Integer(), nullable=True),
        # From app/settings.py, NEVER a literal: v1's banner claimed o3 + gpt-5 while the code
        # ran o4-mini + gpt-4.1, and its logs lied for two months.
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        # The rendered prompt's digest. prompt_version alone cannot tell two renderings of the
        # same version apart, and the catalogue is interpolated into it.
        sa.Column("prompt_sha256", sa.Text(), nullable=False),
        # Which tariff snapshot answered. NULLable: a turn can fail before the repo is read.
        sa.Column("dataset_sha256", sa.Text(), nullable=True),
        sa.Column("turns", sa.Integer(), nullable=True),
        sa.Column("repairs", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # tokens_cached is a SUBSET of tokens_in, not a sibling of it — see pricing.py.
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_cached", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True),
        sa.CheckConstraint(
            "outcome IN ('pending', 'classified', 'clarification', 'error', 'conversation')",
            name="classification_outcome",
        ),
    )

    # THE index: `/api/history` pages by keyset, not by offset. `WHERE user_id = $1 AND
    # (created_at, id) < ($2, $3) ORDER BY created_at DESC, id DESC LIMIT $4` reads exactly
    # one page from this index however deep the user scrolls, and cannot skip or repeat a row
    # when a new classification is inserted mid-scroll — which OFFSET does, on every page.
    # `id DESC` is the tie-break that makes the cursor total: created_at is `now()`, and a
    # user can start two turns inside the same transaction timestamp.
    op.create_index(
        "classification_user_created_idx",
        "classification",
        ["user_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    # Search over the user's own descriptions. Trigram rather than tsvector: the inputs are
    # short product descriptions, frequently misspelled, and Ukrainian has no stemming
    # configuration shipped with Postgres — `%` degrades gracefully where `to_tsquery` finds
    # nothing at all.
    op.create_index(
        "classification_input_text_trgm_idx",
        "classification",
        ["input_text"],
        postgresql_using="gin",
        postgresql_ops={"input_text": "gin_trgm_ops"},
    )

    op.create_table(
        "classification_code",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "classification_id",
            sa.Text(),
            sa.ForeignKey("classification.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # 0-based, and the order the answer was given in. UNIQUE with the parent so a retried
        # write cannot double a code instead of replacing it.
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        # description and full_path are DENORMALISED COPIES of the tariff row as it read at
        # answer time, resolved from the turn ledger — never from the model's own text, and
        # never a join to tariff_node. A record of what the user was told must not change
        # when the next dataset is ingested.
        sa.Column("description", sa.Text(), nullable=False),
        # Always rendered instead of `description`: 2,473 of 10,490 leaf descriptions are
        # literally "інші".
        sa.Column("full_path", sa.Text(), nullable=False),
        # False marks a considered-and-rejected alternative; the SPA labels it «альтернатива».
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # This also indexes the only access path there is — "the codes of this record, in
        # order" — so no separate index on classification_id is created here.
        sa.UniqueConstraint("classification_id", "position", name="classification_code_position"),
    )

    op.create_table(
        "classification_tool_call",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "classification_id",
            sa.Text(),
            sa.ForeignKey("classification.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # `ToolCallRecord.index`, i.e. the ledger's own call order.
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        # The arguments as the model sent them. JSONB and not TEXT because "which codes did
        # emit_classification try?" is a query over this column, not a grep.
        sa.Column("arguments", postgresql.JSONB(), nullable=True),
        # ToolCallRecord.result_digest — a digest, never the full tool result. Storing whole
        # payloads would make this table larger than the tariff itself within a week.
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        # The rejection code from the gate (provenance_violation, code_not_leaf, …). This
        # column is the repair loop's whole evidence base.
        sa.Column("error", sa.Text(), nullable=True),
        # As above: the unique index is the lookup index, so there is no second one.
        sa.UniqueConstraint(
            "classification_id", "position", name="classification_tool_call_position"
        ),
    )


def downgrade() -> None:
    op.drop_table("classification_tool_call")
    op.drop_table("classification_code")
    op.drop_index("classification_input_text_trgm_idx", table_name="classification")
    op.drop_index("classification_user_created_idx", table_name="classification")
    op.drop_table("classification")
