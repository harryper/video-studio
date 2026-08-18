"""Editorial comment HTTP routes.

Two routes, both scoped to ``(project_id, draft_artifact_id)``:

* ``POST`` validates the paragraph exists and the offsets fall inside
  the paragraph text, then persists the comment row.
* ``GET`` returns every comment attached to the draft as Pydantic
  mirrors.

The provider is not touched here — comments are pure CRUD. The model
provider only enters the picture in the rewrite / approve routes
mounted under :mod:`studio.api.routes.stages`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from studio.api.auth import require_csrf, require_session
from studio.api.dependencies import get_session
from studio.artifacts import ArtifactRepository
from studio.content.review import (
    create_comment,
    list_comments,
    validate_offsets,
)
from studio.schemas import DraftRevision, EditorialComment

router = APIRouter(
    prefix="/api/projects",
    tags=["comments"],
    dependencies=[Depends(require_session)],
)


class CreateCommentRequest(BaseModel):
    paragraph_id: str
    start_offset: int
    end_offset: int
    kind: str
    body: str
    ai_action: str


def _load_paragraph_text(
    session: Session, project_id: str, draft_artifact_id: str, paragraph_id: str
) -> str:
    repo = ArtifactRepository(session)
    artifact = repo.get(draft_artifact_id)
    if artifact is None or artifact.project_id != project_id:
        raise HTTPException(status_code=404, detail="draft artifact not found")
    if artifact.kind != "draft":
        raise HTTPException(
            status_code=400,
            detail=f"artifact {draft_artifact_id!r} is not a draft",
        )
    revision = DraftRevision.model_validate(artifact.payload)
    try:
        return revision.paragraph(paragraph_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/{project_id}/drafts/{draft_artifact_id}/comments",
    status_code=201,
    dependencies=[Depends(require_csrf)],
)
def create_comment_route(
    project_id: str,
    draft_artifact_id: str,
    body: CreateCommentRequest,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    paragraph_text = _load_paragraph_text(
        session, project_id, draft_artifact_id, body.paragraph_id
    )
    try:
        validate_offsets(paragraph_text, body.start_offset, body.end_offset)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    comment = create_comment(
        project_id,
        draft_artifact_id,
        body.paragraph_id,
        body.start_offset,
        body.end_offset,
        body.kind,
        body.body,
        body.ai_action,
        session,
    )
    session.commit()
    return comment.model_dump(mode="json")


@router.get("/{project_id}/drafts/{draft_artifact_id}/comments")
def list_comments_route(
    project_id: str,
    draft_artifact_id: str,
    session: Session = Depends(get_session),
) -> list[dict[str, object]]:
    comments: list[EditorialComment] = list_comments(
        project_id, draft_artifact_id, session
    )
    return [c.model_dump(mode="json") for c in comments]


__all__ = ["router"]