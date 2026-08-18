"""Project lifecycle routes.

* ``GET  /api/projects`` — list with optional ``stage`` / ``status`` filters.
* ``POST /api/projects`` — create a project and enqueue the diagnosis job.
* ``GET  /api/projects/{id}`` — single project record.
* ``GET  /api/projects/{id}/artifacts`` — full revision history.
* ``POST /api/projects/{id}/jobs/{job_id}/retry`` — requeue a failed job.
* ``POST /api/projects/{id}/jobs/{job_id}/cancel`` — cancel a queued job.
* ``POST /api/projects/{id}/pitch/regenerate`` — first-time pitch generation.
* ``POST /api/projects/{id}/pitch/reopen`` — discard downstream artifacts
  before regenerating pitches (requires the two-step confirmation handshake).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from studio.api.auth import require_csrf, require_session
from studio.api.dependencies import get_session
from studio.api.errors import ApiError
from studio.artifacts import ArtifactRepository
from studio.jobs import JobNotClaimed, LeaseQueue, Stage
from studio.models import Artifact, Project, ProjectArtifactHead, StageJob
from studio.workflow import current_pitch_set

router = APIRouter(prefix="/api/projects", tags=["projects"])


# ---------------------------------------------------------------------------
# request shapes
# ---------------------------------------------------------------------------


class CreateProjectRequest(BaseModel):
    title: str
    topic: str


class ProjectSummary(BaseModel):
    id: str
    title: str
    topic: str
    created_at: datetime
    updated_at: datetime
    latest_stage: str | None
    latest_job_status: str | None


class ArtifactHistoryEntry(BaseModel):
    id: str
    kind: str
    revision: int
    parent_id: str | None
    created_at: datetime
    accepted_at: datetime | None
    is_head: bool


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _latest_job(session: Session, project_id: str) -> StageJob | None:
    stmt = (
        select(StageJob)
        .where(StageJob.project_id == project_id)
        .order_by(StageJob.created_at.desc(), StageJob.id.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def _project_summary(session: Session, project: Project) -> dict[str, Any]:
    latest = _latest_job(session, project.id)
    return {
        "id": project.id,
        "title": project.title,
        "topic": project.topic,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "latest_stage": latest.stage if latest is not None else None,
        "latest_job_status": latest.status if latest is not None else None,
    }


def _downstream_kinds(session: Session, project_id: str) -> list[str]:
    """Kinds whose head exists for ``project_id`` and are downstream of pitch."""

    stmt = (
        select(ProjectArtifactHead.kind)
        .where(ProjectArtifactHead.project_id == project_id)
        .where(
            ProjectArtifactHead.kind.in_(
                ["narrative", "draft", "speech_plan", "approved_script"]
            )
        )
    )
    return sorted(session.execute(stmt).scalars().all())


def _drop_downstream_heads(session: Session, project_id: str, kinds: list[str]) -> None:
    if not kinds:
        return
    stmt = delete(ProjectArtifactHead).where(
        ProjectArtifactHead.project_id == project_id,
        ProjectArtifactHead.kind.in_(kinds),
    )
    session.execute(stmt)


# ---------------------------------------------------------------------------
# list + create
# ---------------------------------------------------------------------------


@router.get("", dependencies=[Depends(require_session)])
def list_projects(
    stage: str | None = Query(default=None),
    status: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    """List projects ordered by ``updated_at`` desc, optionally filtered.

    ``stage`` and ``status`` match the most recent job for each project.
    """

    projects = list(
        session.execute(
            select(Project).order_by(Project.updated_at.desc(), Project.id.desc())
        ).scalars()
    )
    summaries: list[dict[str, Any]] = []
    for project in projects:
        latest = _latest_job(session, project.id)
        latest_stage = latest.stage if latest is not None else None
        latest_status = latest.status if latest is not None else None
        if stage is not None and latest_stage != stage:
            continue
        if status is not None and latest_status != status:
            continue
        summaries.append(
            {
                "id": project.id,
                "title": project.title,
                "topic": project.topic,
                "created_at": project.created_at,
                "updated_at": project.updated_at,
                "latest_stage": latest_stage,
                "latest_job_status": latest_status,
            }
        )
    return summaries


@router.post("", status_code=201, dependencies=[Depends(require_csrf)])
def create_project(
    body: CreateProjectRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    project = Project(
        title=body.title,
        topic=body.topic,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(project)
    session.flush()
    job = LeaseQueue(session).enqueue(project.id, Stage.DIAGNOSIS, [])
    session.commit()
    return {
        "id": project.id,
        "title": project.title,
        "topic": project.topic,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "stage": "diagnosis_queued",
        "job_id": job.id,
    }


# ---------------------------------------------------------------------------
# single project
# ---------------------------------------------------------------------------


@router.get("/{project_id}", dependencies=[Depends(require_session)])
def get_project(
    project_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found")
    return _project_summary(session, project)


# ---------------------------------------------------------------------------
# artifact history
# ---------------------------------------------------------------------------


@router.get(
    "/{project_id}/artifacts", dependencies=[Depends(require_session)]
)
def list_artifacts(
    project_id: str, session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found")

    repo = ArtifactRepository(session)
    stmt = (
        select(Artifact)
        .where(Artifact.project_id == project_id)
        .order_by(Artifact.created_at.asc(), Artifact.id.asc())
    )
    artifacts = list(session.execute(stmt).scalars())

    rows: list[dict[str, Any]] = []
    for artifact in artifacts:
        head = repo.current(project_id, artifact.kind)
        rows.append(
            {
                "id": artifact.id,
                "kind": artifact.kind,
                "revision": artifact.revision,
                "parent_id": artifact.parent_id,
                "created_at": artifact.created_at,
                "accepted_at": artifact.accepted_at,
                "is_head": head is not None and head.id == artifact.id,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# pitch / regenerate / reopen
# ---------------------------------------------------------------------------


@router.post(
    "/{project_id}/pitch/regenerate",
    status_code=202,
    dependencies=[Depends(require_csrf)],
)
def pitch_regenerate(
    project_id: str, session: Session = Depends(get_session)
) -> dict[str, str]:
    """Queue a fresh pitches job — only valid when no current pitch set exists."""

    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found")
    if current_pitch_set(project_id, session) is not None:
        invalidates = _downstream_kinds(session, project_id)
        raise ApiError(
            "confirmation_required",
            "pitch set already exists; reopen before regenerating",
            status_code=409,
            details={"invalidates": invalidates},
        )

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


@router.post(
    "/{project_id}/pitch/reopen",
    dependencies=[Depends(require_csrf)],
)
def pitch_reopen(
    project_id: str,
    confirm_invalidates: str | None = Query(default=None, alias="confirm_invalidates"),
    x_confirm_invalidates: str | None = Header(default=None, alias="X-Confirm-Invalidates"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Confirm and clear downstream heads before regenerating pitches.

    First call (no confirm header) returns 409 with the list of downstream
    kinds that will be invalidated. Second call must include the same list
    via the ``X-Confirm-Invalidates`` header (or ``?confirm_invalidates=``
    query) to proceed. When no downstream artifacts exist the reopen is a
    no-op and returns 200 without requiring confirmation.
    """

    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found")

    if current_pitch_set(project_id, session) is None:
        raise HTTPException(
            status_code=409,
            detail="no pitch set exists for this project; nothing to reopen",
        )

    invalidates = _downstream_kinds(session, project_id)
    if not invalidates:
        return {"invalidated": []}

    confirmed = x_confirm_invalidates or confirm_invalidates
    if not confirmed:
        raise ApiError(
            "confirmation_required",
            "reopening pitches will discard downstream artifacts; confirm before proceeding",
            status_code=409,
            details={"invalidates": invalidates},
        )

    proposed = sorted(part.strip() for part in confirmed.split(",") if part.strip())
    if proposed != invalidates:
        raise ApiError(
            "confirmation_mismatch",
            "listed invalidates do not match current downstream artifacts",
            status_code=409,
            details={"expected": invalidates, "received": proposed},
        )

    _drop_downstream_heads(session, project_id, invalidates)
    session.commit()
    return {"invalidated": invalidates}


# ---------------------------------------------------------------------------
# retry / cancel
# ---------------------------------------------------------------------------


@router.post(
    "/{project_id}/jobs/{job_id}/retry",
    dependencies=[Depends(require_csrf)],
)
def retry_job(project_id: str, job_id: str, session: Session = Depends(get_session)) -> dict[str, str]:
    job = session.get(StageJob, job_id)
    if job is None or job.project_id != project_id:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    if job.status != "failed":
        raise ApiError(
            "job_not_failed",
            f"job {job_id} is in status {job.status!r}; only failed jobs can be retried",
            status_code=409,
            details={"status": job.status},
        )
    job.status = "queued"
    job.error_code = None
    job.error_message = None
    job.lease_token = None
    job.lease_expires_at = None
    session.commit()
    return {"job_id": job.id, "status": job.status}


@router.post(
    "/{project_id}/jobs/{job_id}/cancel",
    dependencies=[Depends(require_csrf)],
)
def cancel_job(project_id: str, job_id: str, session: Session = Depends(get_session)) -> dict[str, str]:
    job = session.get(StageJob, job_id)
    if job is None or job.project_id != project_id:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    if job.status != "queued":
        raise ApiError(
            "job_not_queued",
            f"job {job_id} is in status {job.status!r}; only queued jobs can be cancelled",
            status_code=409,
            details={"status": job.status},
        )
    try:
        LeaseQueue(session).cancel(job_id)
    except JobNotClaimed as exc:
        raise ApiError(
            "job_not_queued",
            str(exc),
            status_code=409,
            details={"status": job.status},
        ) from exc
    return {"job_id": job_id, "status": "cancelled"}


__all__ = ["router"]