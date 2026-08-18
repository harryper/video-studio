"""ORM models and immutable-artifact guarantees for Content Studio.

Five tables back every later task:

* ``projects`` — long-lived project records (created implicitly when the first
  artifact is inserted, see ``studio.artifacts``).
* ``artifacts`` — append-only revisions of structured content. ``payload``,
  ``kind``, ``project_id`` and ``revision`` are frozen once written.
* ``project_artifact_heads`` — exactly one "current" pointer per ``(project_id,
  kind)`` row, mutated only by ``ArtifactRepository.accept``.
* ``editorial_comments`` — human/AI reviewer notes anchored to a draft
  artifact (wired up in Task 8).
* ``stage_jobs`` — durable background work with lease-based recovery (wired
  up in Task 3).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


IMMUTABLE_ARTIFACT_FIELDS = frozenset({"payload", "kind", "project_id", "revision"})


class ImmutabilityError(Exception):
    """Raised when an attribute that is frozen after insert is reassigned."""


class Base(DeclarativeBase):
    """Declarative base for every Content Studio model."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    return str(uuid4())


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    topic: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class Artifact(Base):
    """Append-only revision of structured content for a project.

    The four immutable fields (``payload``, ``kind``, ``project_id``,
    ``revision``) cannot be reassigned once the row has been flushed; this is
    enforced both via ``__setattr__`` (immediate feedback) and a
    ``before_update`` mapper event (defence in depth at flush time).
    """

    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "kind", "revision", name="uq_artifact_project_kind_revision"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(nullable=False)
    parent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("artifacts.id"), nullable=True
    )
    payload: Mapped[Any] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")

    def __setattr__(self, name: str, value: Any) -> None:
        if name in IMMUTABLE_ARTIFACT_FIELDS:
            state = getattr(self, "_sa_instance_state", None)
            if state is not None and state.has_identity:
                raise ImmutabilityError(
                    f"{name!r} is immutable once an Artifact row is persisted"
                )
        super().__setattr__(name, value)


@event.listens_for(Artifact, "before_update", propagate=True)
def _reject_immutable_update(mapper, connection, target):  # noqa: ARG001
    """Defence in depth: even raw updates cannot mutate locked columns."""

    state = target._sa_instance_state
    history = state.attrs
    for field in IMMUTABLE_ARTIFACT_FIELDS:
        attr = history[field]
        if attr.load_history().has_changes():
            raise ImmutabilityError(
                f"{field!r} is immutable once an Artifact row is persisted"
            )


class ProjectArtifactHead(Base):
    __tablename__ = "project_artifact_heads"
    __table_args__ = (
        UniqueConstraint("project_id", "kind", name="uq_head_project_kind"),
    )

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("artifacts.id"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class StageJob(Base):
    __tablename__ = "stage_jobs"
    __table_args__ = (
        Index("ix_stage_jobs_status_lease", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=False, index=True
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    input_artifact_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    output_artifact_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("artifacts.id"), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )


class EditorialComment(Base):
    __tablename__ = "editorial_comments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    draft_artifact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("artifacts.id"), nullable=False, index=True
    )
    paragraph_id: Mapped[str] = mapped_column(String(64), nullable=False)
    start_offset: Mapped[int] = mapped_column(nullable=False)
    end_offset: Mapped[int] = mapped_column(nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    ai_action: Mapped[str] = mapped_column(String(16), nullable=False)
    processed_in_revision: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("artifacts.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


__all__ = [
    "Base",
    "Project",
    "Artifact",
    "ProjectArtifactHead",
    "StageJob",
    "EditorialComment",
    "ImmutabilityError",
    "IMMUTABLE_ARTIFACT_FIELDS",
]