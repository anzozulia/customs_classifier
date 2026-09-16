## Template used by `alembic revision`. Lines beginning with ## are mako comments:
## they are NOT written into the generated migration.
##
## `import sqlalchemy as sa` is included because almost every migration here creates
## tables and needs column types. If a revision turns out to be raw SQL only
## (op.execute(...)), DELETE the import — ruff F401 flags it, and adding a `# noqa`
## instead would then trip RUF100 the moment someone uses sa again.
##
## autogenerate is unavailable by design (migrations/env.py: target_metadata is None,
## there are no ORM models), so `upgrades`/`downgrades` are always empty and every
## migration is written by hand.
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
