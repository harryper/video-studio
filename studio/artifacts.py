"""Append-only artifact repository.

Wraps the four persistence operations every Content Studio service needs:

* :meth:`ArtifactRepository.create` writes a new revision (revision = max + 1).
* :meth:`ArtifactRepository.get` fetches by id.
* :meth:`ArtifactRepository.current` joins ``project_artifact_heads``.
* :meth:`ArtifactRepository.accept` updates the current pointer atomically.
* :meth:`ArtifactRepository.list_revisions` returns history newest-first.

The repository takes a ``Session`` (not a factory) so callers control the
transaction boundary — usually one ``with session.begin():`` block per
business action.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from studio.models import Artifact, Project, ProjectArtifactHead
from studio.schemas import validate_payload


class ArtifactRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        project_id: str,
        kind: str,
        payload: dict[str, Any],
        parent_id: str | None = None,
        created_by: str = "system",
    ) -> Artifact:
        """Append a new revision. Auto-increments ``revision`` for ``(project, kind)``."""

        payload_dict = validate_payload(kind, payload)
        revision = self._next_revision(project_id, kind)
        artifact = Artifact(
            project_id=project_id,
            kind=kind,
            revision=revision,
            parent_id=parent_id,
            payload=payload_dict,
            created_by=created_by,
        )
        self._session.add(artifact)
        self._session.flush()
        return artifact

    def get(self, artifact_id: str) -> Artifact | None:
        return self._session.get(Artifact, artifact_id)

    def current(self, project_id: str, kind: str) -> Artifact | None:
        stmt = (
            select(Artifact)
            .join(ProjectArtifactHead, ProjectArtifactHead.artifact_id == Artifact.id)
            .where(ProjectArtifactHead.project_id == project_id)
            .where(ProjectArtifactHead.kind == kind)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def accept(self, project_id: str, artifact_id: str) -> Artifact:
        """Mark ``artifact_id`` as the current head for ``(project_id, kind)``."""

        artifact = self.get(artifact_id)
        if artifact is None:
            raise NoResultFound(f"artifact {artifact_id} not found")
        if artifact.project_id != project_id:
            raise ValueError(
                f"artifact {artifact_id} belongs to project {artifact.project_id!r}, "
                f"not {project_id!r}"
            )

        head = self._session.get(ProjectArtifactHead, (project_id, artifact.kind))
        if head is None:
            head = ProjectArtifactHead(
                project_id=project_id,
                kind=artifact.kind,
                artifact_id=artifact.id,
            )
            self._session.add(head)
        else:
            head.artifact_id = artifact.id
        self._session.flush()
        return artifact

    def list_revisions(self, project_id: str, kind: str) -> list[Artifact]:
        stmt = (
            select(Artifact)
            .where(Artifact.project_id == project_id)
            .where(Artifact.kind == kind)
            .order_by(Artifact.revision.desc())
        )
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _next_revision(self, project_id: str, kind: str) -> int:
        stmt = (
            select(Artifact.revision)
            .where(Artifact.project_id == project_id)
            .where(Artifact.kind == kind)
            .order_by(Artifact.revision.desc())
            .limit(1)
        )
        current_max = self._session.execute(stmt).scalar_one_or_none()
        return 1 if current_max is None else current_max + 1


__all__ = ["ArtifactRepository"]