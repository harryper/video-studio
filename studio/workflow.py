"""Thin orchestration layer between HTTP routes and content services.

Routes should not know how a service is constructed, and services should not
know about sessions or artifacts. This module is the only place the two meet.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from studio.artifacts import ArtifactRepository
from studio.content.pitches import PitchService
from studio.models import Artifact
from studio.providers.base import ModelProvider
from studio.schemas import StoryPitch, StoryPitchSet


def build_pitch_service(provider: ModelProvider) -> PitchService:
    return PitchService(provider)


def current_pitch_set(
    project_id: str, session: Session
) -> tuple[Artifact, StoryPitchSet] | None:
    """Latest generated pitch set for ``project_id``, newest revision first.

    ``kind="pitches"`` holds two payload shapes — generated sets and the
    acceptance record — so the head pointer alone is not enough: after an
    accept the head is the acceptance record. Walk revisions until a set
    turns up. Returns the artifact too, so callers can link the acceptance
    record back to the exact revision the editor was looking at.
    """

    for artifact in ArtifactRepository(session).list_revisions(project_id, "pitches"):
        payload: dict[str, Any] = artifact.payload or {}
        if "pitches" in payload:
            return artifact, StoryPitchSet.model_validate(payload)
    return None


def accept_pitch(
    project_id: str,
    pitch_set_id: str,
    pitch_id: str,
    session: Session,
    edited_pitch: StoryPitch | None = None,
) -> Artifact:
    """Record the editor's choice as a new ``pitches`` revision and make it head.

    ``pitch_set_id`` is the *artifact* id of the set being accepted; it becomes
    the new revision's ``parent_id`` so the lineage is queryable.
    """

    repo = ArtifactRepository(session)
    artifact = repo.create(
        project_id,
        "pitches",
        {
            "selected_pitch_id": pitch_id,
            "edited_pitch": (
                edited_pitch.model_dump(mode="json") if edited_pitch else None
            ),
        },
        parent_id=pitch_set_id,
        created_by="editor",
    )
    repo.accept(project_id, artifact.id)
    return artifact


__all__ = ["accept_pitch", "build_pitch_service", "current_pitch_set"]
