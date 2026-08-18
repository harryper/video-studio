"""add topic column to projects

Revision ID: 0002_add_project_topic
Revises: 0001_content_core
Create Date: 2026-08-18 09:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_add_project_topic"
down_revision: str | None = "0001_content_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add as nullable with a server default first so any pre-existing dev row
    # gets backfilled; then tighten to ``nullable=False`` once the column is
    # populated. Two-step is required because SQLite cannot ALTER COLUMN
    # constraints in a single statement.
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(
            sa.Column("topic", sa.String(length=255), nullable=True, server_default="")
        )
    op.execute("UPDATE projects SET topic = '' WHERE topic IS NULL")
    with op.batch_alter_table("projects") as batch_op:
        batch_op.alter_column("topic", existing_type=sa.String(length=255), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("topic")