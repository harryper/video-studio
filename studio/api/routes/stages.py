"""Stage routes for the pitch human gate.

The model provider is injected via :func:`set_default_provider` rather than a
FastAPI dependency: the pitch job itself runs in the worker (Task 14), so the
provider is only needed for future synchronous stage calls, and a module-level
setter keeps tests offline without an app-factory argument.
"""

from __future__ import annotations

from collections.abc import Generator

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from studio import db as studio_db
from studio.artifacts import ArtifactRepository
from studio.content.review import (
    NewerDraftExists,
    ReviewService,
    approve_draft,
    list_comments,
)
from studio.jobs import LeaseQueue, Stage
from studio.providers.base import ModelProvider
from studio.schemas import DraftRevision, EditorialComment, ResearchPacket, StoryPitch
from studio.workflow import accept_pitch, current_pitch_set

router = APIRouter(prefix="/api/projects", tags=["stages"])

_default_provider: ModelProvider | None = None


def set_default_provider(provider: ModelProvider | None) -> None:
    """Install the process-wide model provider used by stage routes."""

    global _default_provider
    _default_provider = provider


def get_default_provider() -> ModelProvider | None:
    return _default_provider


def get_session() -> Generator[Session, None, None]:
    factory = sessionmaker(
        bind=studio_db.get_engine(), autoflush=False, expire_on_commit=False
    )
    session = factory()
    try:
        yield session
    finally:
        session.close()


class AcceptRequest(BaseModel):
    edited_pitch: StoryPitch | None = None


@router.post("/{project_id}/pitches/generate", status_code=202)
def generate_pitches_route(
    project_id: str, session: Session = Depends(get_session)
) -> dict[str, str]:
    repo = ArtifactRepository(session)
    diagnosis = repo.current(project_id, "diagnosis")
    research = repo.current(project_id, "research")
    if diagnosis is None or research is None:
        raise HTTPException(
            status_code=409,
            detail="pitch generation needs an accepted diagnosis and research packet",
        )

    job = LeaseQueue(session).enqueue(
        project_id, Stage.PITCHES, [diagnosis.id, research.id]
    )
    session.commit()
    return {"job_id": job.id}


@router.get("/{project_id}/pitches")
def get_pitches_route(
    project_id: str, session: Session = Depends(get_session)
) -> dict[str, object]:
    found = current_pitch_set(project_id, session)
    if found is None:
        raise HTTPException(status_code=404, detail="no pitch set for this project")
    return found[1].model_dump(mode="json")


@router.post("/{project_id}/pitches/{pitch_id}/accept", status_code=201)
def accept_pitch_route(
    project_id: str,
    pitch_id: str,
    body: AcceptRequest | None = None,
    session: Session = Depends(get_session),
) -> dict[str, str]:
    found = current_pitch_set(project_id, session)
    if found is None:
        raise HTTPException(status_code=404, detail="no pitch set for this project")
    set_artifact, pitch_set = found

    if not any(p.id == pitch_id for p in pitch_set.pitches):
        raise HTTPException(
            status_code=404, detail=f"pitch {pitch_id} is not in the current set"
        )

    edited = body.edited_pitch if body else None
    if edited is not None and edited.id != pitch_id:
        raise HTTPException(
            status_code=400,
            detail="edited_pitch.id must match the pitch being accepted",
        )

    # The accepted-pitch artifact (not the set artifact) is what Stage.NARRATIVE
    # reads as its first input. Its payload is ``AcceptedPitch`` —
    # ``selected_pitch_id`` plus an optional ``edited_pitch``. If
    # ``edited_pitch`` is null the narrative handler walks ``parent_id`` back
    # to the set artifact (``current_pitch_set`` finds the same set revision)
    # and picks the pitch by ``selected_pitch_id``.
    artifact = accept_pitch(project_id, set_artifact.id, pitch_id, session, edited)
    job = LeaseQueue(session).enqueue(project_id, Stage.NARRATIVE, [artifact.id])
    session.commit()
    return {"artifact_id": artifact.id, "job_id": job.id}


# ---------------------------------------------------------------------------
# review: rewrite + approve
# ---------------------------------------------------------------------------


def _load_draft(
    session: Session, project_id: str, draft_artifact_id: str
) -> DraftRevision:
    repo = ArtifactRepository(session)
    artifact = repo.get(draft_artifact_id)
    if artifact is None or artifact.project_id != project_id:
        raise HTTPException(status_code=404, detail="draft artifact not found")
    if artifact.kind != "draft":
        raise HTTPException(
            status_code=400,
            detail=f"artifact {draft_artifact_id!r} is not a draft",
        )
    return DraftRevision.model_validate(artifact.payload)


def _load_research(
    session: Session, project_id: str
) -> ResearchPacket | None:
    repo = ArtifactRepository(session)
    artifact = repo.current(project_id, "research")
    if artifact is None:
        return None
    return ResearchPacket.model_validate(artifact.payload)


@router.post("/{project_id}/drafts/{draft_artifact_id}/rewrite", status_code=201)
def rewrite_route(
    project_id: str,
    draft_artifact_id: str,
    session: Session = Depends(get_session),  # noqa: B008 — FastAPI dependency
) -> dict[str, str]:
    repo = ArtifactRepository(session)
    draft_artifact = repo.get(draft_artifact_id)
    if draft_artifact is None or draft_artifact.project_id != project_id:
        raise HTTPException(status_code=404, detail="draft artifact not found")
    draft = DraftRevision.model_validate(draft_artifact.payload)

    provider = get_default_provider()
    if provider is None:
        raise HTTPException(
            status_code=503,
            detail="rewrite requires the model provider to be configured",
        )
    research = _load_research(session, project_id)

    comments: list[EditorialComment] = list_comments(
        project_id, draft_artifact_id, session
    )
    new_revision = ReviewService(provider, research).rewrite(draft, comments)
    artifact = repo.create(
        project_id,
        "draft",
        new_revision.model_dump(mode="json"),
        parent_id=draft_artifact_id,
        created_by="editor",
    )
    repo.accept(project_id, artifact.id)
    session.commit()
    return {"artifact_id": artifact.id}


@router.post("/{project_id}/drafts/{draft_artifact_id}/approve", status_code=201)
def approve_route(
    project_id: str,
    draft_artifact_id: str,
    session: Session = Depends(get_session),  # noqa: B008 — FastAPI dependency
) -> dict[str, str]:
    try:
        artifact = approve_draft(project_id, draft_artifact_id, session)
    except NewerDraftExists as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"artifact_id": artifact.id}


__all__ = ["get_default_provider", "router", "set_default_provider"]
