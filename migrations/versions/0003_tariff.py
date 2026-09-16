"""tariff_dataset · tariff_node — the nomenclature itself.

Revision ID: 0003_tariff
Revises: 0002_chatkit_store
Create Date: 2026-09-16

Shape, and why:

* **Surrogate PK + `UNIQUE (dataset_id, level, code)`.** All 21 section codes ("01".."21") are
  also valid group codes. v1 addressed both as bare 2-digit strings and silently returned the
  wrong subtree. Here `('section','01')` and `('group','01')` are different rows that cannot
  be confused, and the collision becomes structurally unrepresentable rather than documented.

* **`code` is TEXT.** 1,773 codes have a leading zero. Never INTEGER, never `int()`.

* **`full_path` is materialised and NOT NULL.** 2,473 of the 10,490 leaf descriptions are
  literally "інші" and 46% are 15 characters or shorter, so nothing anywhere may read a bare
  `description`. Materialising it at ingest also means the M4 embedding backfill needs no
  re-ingest: the text it will embed already exists.

* **`is_terminal` is an authored column.** On this snapshot it agrees with `len(code) = 10` on
  all 10,490 terminals. That is a property of the snapshot, not of the nomenclature.

* **`parent_id` is nullable and the prefix tree is incomplete on purpose.** 3,236 ten-digit
  codes have no 6-digit parent in the source; ingest points them at the longest EXISTING
  prefix instead of synthesising rows whose text is not in the tariff.

* **No `embedding` column and no `tsvector`.** Retrieval ships as the tree walk (D23);
  pgvector arrives in M4 with the eval set that can prove it helps. An unused `vector(1536)`
  column plus an HNSW index today would be a second system maintained for nobody.

`pg_trgm` is created here because `search_candidates` scores `word_similarity()` over
`full_path`. The extension is NOT dropped on downgrade: other migrations may rely on it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_tariff"
down_revision = "0002_chatkit_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "tariff_dataset",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("source_filename", sa.Text(), nullable=False),
        # sha256 of the raw source bytes. The ingest is idempotent on this value and
        # /readyz compares it against the file on disk — v1 failed silently when the
        # tariff file was missing.
        sa.Column("sha256", sa.CHAR(64), nullable=False, unique=True),
        sa.Column("node_count", sa.Integer(), nullable=False),
        sa.Column("terminal_count", sa.Integer(), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")
        ),
        sa.Column(
            "ingested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # At most one active dataset, enforced by the database rather than by the ingest code.
    # This is what makes "load the new tariff, then switch" a transaction instead of a race.
    op.create_index(
        "tariff_dataset_one_active",
        "tariff_dataset",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "tariff_node",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.BigInteger(),
            sa.ForeignKey("tariff_dataset.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code", sa.Text(), nullable=False),
        # A CHECK rather than an ENUM: an enum type costs a ::tariff_level cast on every
        # asyncpg parameter and an ALTER TYPE on every change, and buys nothing a CHECK
        # does not already guarantee.
        sa.Column("level", sa.Text(), nullable=False),
        # depth = len(ancestor_codes): section 0, group 1, category 2, prefix tree 3..5.
        sa.Column("depth", sa.SmallInteger(), nullable=False),
        sa.Column(
            "parent_id",
            sa.BigInteger(),
            sa.ForeignKey("tariff_node.id", ondelete="CASCADE"),
            nullable=True,
        ),
        # Whitespace-collapsed, with the trailing ':' that marks a heading stripped. The
        # colon is typography, not legal text; the ingest cross-checks it against the raw
        # JSON (2,622/2,622 internal nodes end with ':', only 6 of 10,490 leaves do).
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("full_path", sa.Text(), nullable=False),
        # Excludes self. ancestor_codes[1] (PG is 1-indexed) is ALWAYS the section, which is
        # how section is derived — it is never a tool parameter.
        sa.Column("ancestor_codes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("is_terminal", sa.Boolean(), nullable=False),
        sa.Column("child_count", sa.Integer(), nullable=False),
        # full_path shared with a sibling: 2,487 terminals (23.71%). A pass, not an error —
        # the answer has to show the whole set, because this dataset cannot separate them.
        sa.Column(
            "path_is_ambiguous", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")
        ),
        # Not terminal and childless: section 15 / group 77 ("Група 77"), reserved. Exactly
        # one row. The tools answer "зарезервована" instead of an empty list.
        sa.Column("is_dead_end", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        # '07/39/3919/391910/3919101200' — ancestor codes plus self. ORDER BY sort_key is
        # document order for the whole tree, which is also correct sibling order (section
        # 06's groups are stored 30…38,28,29 in the source).
        sa.Column("sort_key", sa.Text(), nullable=False),
        sa.UniqueConstraint("dataset_id", "level", "code", name="tariff_node_natural_key"),
        sa.CheckConstraint(
            "level IN ('section', 'group', 'category', 'code')", name="tariff_node_level"
        ),
        sa.CheckConstraint("length(full_path) > 0", name="tariff_node_full_path_not_empty"),
        sa.CheckConstraint(
            "NOT is_terminal OR child_count = 0", name="tariff_node_terminal_is_leaf"
        ),
        sa.CheckConstraint(
            "is_dead_end = (NOT is_terminal AND child_count = 0)", name="tariff_node_dead_end"
        ),
    )

    # The drill-down primitive: children of a node, and the recursive subtree walk.
    op.create_index("tariff_node_parent_idx", "tariff_node", ["dataset_id", "parent_id"])
    # Lookup by bare code when the level is derived from the length. (dataset_id, level, code)
    # is already indexed by the natural-key constraint, so it is not repeated here.
    op.create_index("tariff_node_code_idx", "tariff_node", ["dataset_id", "code"])
    # For lookups against full_path. `search_candidates` today orders by an explicit
    # word_similarity() floor, which is a 14k-row scan and does not touch this index; the
    # index is what makes the `%` / `%>` operators usable (admin lookups now, the M4
    # exact-term path later) without a sequential scan per query.
    op.create_index(
        "tariff_node_full_path_trgm_idx",
        "tariff_node",
        ["full_path"],
        postgresql_using="gin",
        postgresql_ops={"full_path": "gin_trgm_ops"},
    )
    # Deliberately absent: an index on is_terminal (74% of rows — the planner would ignore it)
    # and one on sort_key (widest sibling set measured is 58 rows).


def downgrade() -> None:
    op.drop_table("tariff_node")
    op.drop_index("tariff_dataset_one_active", table_name="tariff_dataset")
    op.drop_table("tariff_dataset")
