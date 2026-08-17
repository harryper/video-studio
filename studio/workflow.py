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
from studio.schemas import AcceptedPitch, StoryPitch, StoryPitchSet


def build_pitch_service(provider: ModelProvider) -> PitchService:
    return PitchService(provider)


def current_pitch_set(
    project_id: str, session: Session
) -> tuple[Artifact, StoryPitchSet] | None:
    """Latest generated pitch set for ``project_id``, newest revision first.

    ``kind="pitches"`` holds two payload shapes — generated sets and the
    acceptance record — so the head pointer alone is not enough: after an
    accept the head is the acceptance record. Walk revisions newest-first
    until a set (``payload_kind == "pitch_set"``) turns up. Returns the
    artifact too, so callers can link the acceptance record back to the
    exact revision the editor was looking at.
    """

    for artifact in ArtifactRepository(session).list_revisions(project_id, "pitches"):
        payload: dict[str, Any] = artifact.payload or {}
        if payload.get("payload_kind") == "pitch_set":
            return artifact, StoryPitchSet.model_validate(payload)
    return None


def accept_pitch(
    project_id: str,
    set_artifact_id: str,
    pitch_id: str,
    session: Session,
    edited_pitch: StoryPitch | None = None,
) -> Artifact:
    """Record the editor's choice as a new ``pitches`` revision and make it head.

    ``set_artifact_id`` is the artifact id of the pitch set being accepted
    (NOT ``StoryPitchSet.id``, which is a content-level id with no FK role).
    It becomes the new revision's ``parent_id`` so the lineage is queryable.
    Downstream stages consume the *acceptance* artifact, whose payload is
    :class:`~studio.schemas.AcceptedPitch`; if ``edited_pitch`` is null they
    walk ``parent_id`` to the set artifact and pick the pitch by
    ``selected_pitch_id``.
    """

    repo = ArtifactRepository(session)
    payload = AcceptedPitch(
        selected_pitch_id=pitch_id,
        edited_pitch=edited_pitch,
    ).model_dump(mode="json")
    artifact = repo.create(
        project_id,
        "pitches",
        payload,
        parent_id=set_artifact_id,
        created_by="editor",
    )
    repo.accept(project_id, artifact.id)
    return artifact


__all__ = ["accept_pitch", "build_pitch_service", "current_pitch_set"]
