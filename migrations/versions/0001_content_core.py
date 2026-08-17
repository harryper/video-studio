"""content core tables

Revision ID: 0001_content_core
Revises:
Create Date: 2026-08-17 20:55:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0001_content_core"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.String(length=36), sa.ForeignKey("artifacts.id"), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default="system"),
        sa.UniqueConstraint("project_id", "kind", "revision", name="uq_artifact_project_kind_revision"),
    )
    op.create_index("ix_artifacts_project_id", "artifacts", ["project_id"])
    op.create_index("ix_artifacts_kind", "artifacts", ["kind"])

    op.create_table(
        "project_artifact_heads",
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), primary_key=True, nullable=False),
        sa.Column("kind", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("artifact_id", sa.String(length=36), sa.ForeignKey("artifacts.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "kind", name="uq_head_project_kind"),
    )

    op.create_table(
        "stage_jobs",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_artifact_ids", sa.JSON(), nullable=False),
        sa.Column("output_artifact_id", sa.String(length=36), sa.ForeignKey("artifacts.id"), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_stage_jobs_project_id", "stage_jobs", ["project_id"])
    op.create_index(
        "ix_stage_jobs_status_lease", "stage_jobs", ["status", "lease_expires_at"]
    )

    op.create_table(
        "editorial_comments",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "draft_artifact_id",
            sa.String(length=36),
            sa.ForeignKey("artifacts.id"),
            nullable=False,
        ),
        sa.Column("paragraph_id", sa.String(length=64), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("ai_action", sa.String(length=16), nullable=False),
        sa.Column(
            "processed_in_revision",
            sa.String(length=36),
            sa.ForeignKey("artifacts.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_editorial_comments_draft_artifact_id",
        "editorial_comments",
        ["draft_artifact_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_editorial_comments_draft_artifact_id", table_name="editorial_comments")
    op.drop_table("editorial_comments")
    op.drop_index("ix_stage_jobs_status_lease", table_name="stage_jobs")
    op.drop_index("ix_stage_jobs_project_id", table_name="stage_jobs")
    op.drop_table("stage_jobs")
    op.drop_table("project_artifact_heads")
    op.drop_index("ix_artifacts_kind", table_name="artifacts")
    op.drop_index("ix_artifacts_project_id", table_name="artifacts")
    op.drop_table("artifacts")
    op.drop_table("projects")