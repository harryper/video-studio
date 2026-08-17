"""Editorial review service: paragraph comments, targeted rewrite,
and immutable approval.

Three responsibilities:

* :func:`validate_offsets` — guard against out-of-range anchor offsets
  before they reach the database.
* :func:`create_comment` / :func:`list_comments` — thin repositories over
  the :class:`~studio.models.EditorialComment` ORM rows.
* :class:`ReviewService` — turn a :class:`DraftRevision` + anchored
  comments into a new revision. Only paragraphs that carry at least one
  ``ai_action="rewrite"`` comment are sent to the model; everything
  else is copied byte-for-byte. A protected span (the text inside a
  ``rewrite`` comment's ``[start_offset, end_offset)`` range) MUST
  appear in the rewritten output — any drop raises
  :class:`ProtectedSpanViolation` and the new revision is refused.
* :func:`approve_draft` — fold a draft into a frozen
  :class:`ApprovedScript` artifact, mark every comment as processed,
  and refuse (409) if a newer draft revision exists.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from studio.artifacts import ArtifactRepository
from studio.models import EditorialComment as OrmEditorialComment
from studio.providers.base import ModelProvider
from studio.schemas import (
    ApprovedScript,
    DraftParagraph,
    DraftRevision,
    EditorialComment,
    NarrativePlan,
    ResearchPacket,
)

# ---------------------------------------------------------------------------
# exceptions
# ---------------------------------------------------------------------------


class ProtectedSpanViolation(Exception):
    """Raised when a rewrite drops a protected span the user required."""

    def __init__(self, paragraph_id: str, span: str) -> None:
        super().__init__(
            f"rewrite dropped protected span {span!r} in paragraph {paragraph_id!r}"
        )
        self.paragraph_id = paragraph_id
        self.span = span


class NewerDraftExists(Exception):
    """Raised by :func:`approve_draft` when a newer draft exists."""

    def __init__(self, draft_id: str) -> None:
        super().__init__(
            f"cannot approve draft {draft_id!r}: a newer draft revision exists"
        )
        self.draft_id = draft_id


# ---------------------------------------------------------------------------
# offset validation
# ---------------------------------------------------------------------------


def validate_offsets(paragraph_text: str, start_offset: int, end_offset: int) -> None:
    """Raise ``ValueError`` when the offsets fall outside ``paragraph_text``.

    ``(0, 0)`` is the "no protected range" sentinel — the comment anchors
    to a paragraph but no substring is reserved for preservation.
    """

    if start_offset == 0 and end_offset == 0:
        return
    if start_offset < 0:
        raise ValueError(f"start_offset {start_offset} is negative")
    if end_offset <= start_offset:
        raise ValueError(
            f"end_offset {end_offset} must be > start_offset {start_offset}"
        )
    if end_offset > len(paragraph_text):
        raise ValueError(
            f"end_offset {end_offset} exceeds paragraph length {len(paragraph_text)}"
        )


# ---------------------------------------------------------------------------
# model output shapes
# ---------------------------------------------------------------------------


class _RewriteParagraph(BaseModel):
    paragraph_id: str
    text: str


class _RewriteOutput(BaseModel):
    paragraphs: list[_RewriteParagraph]


# ---------------------------------------------------------------------------
# review service
# ---------------------------------------------------------------------------


REWRITE_SYSTEM = """你是科普短视频的定向改写员。你只能处理人类编辑明确标注为交给 AI 的批注，并且必须保留被标注为"保留原句"的文字原封不动。

严禁使用以下套路化表达：
- "你以为……其实……"
- "这就有意思了"
- "离谱的是"
- "说白了"
- "关键是"
- "没了"

响应必须是严格符合 schema 的 JSON 对象：paragraphs 列表，每项对应一段需改写的内容，包含 paragraph_id（必须与输入一致）和改写后的 text。被标记为"保留原句"的文字必须原封不动地出现在 text 中。"""


class ReviewService:
    """Turn a draft + comments into a new revision that preserves protected spans."""

    def __init__(
        self,
        provider: ModelProvider,
        research: ResearchPacket | None = None,
    ) -> None:
        self._provider = provider
        self._research = research or _empty_research()

    # ------------------------------------------------------------------
    def rewrite(
        self,
        draft: DraftRevision,
        comments: list[EditorialComment],
    ) -> DraftRevision:
        """Produce a new ``DraftRevision`` with ``parent_id=draft.id``.

        * ``ai_action="rewrite"`` comments trigger a model call for
          their paragraph; the protected span inside each such comment
          MUST survive in the model output.
        * ``ai_action="note"`` comments are recorded but never trigger
          a rewrite.
        * ``ai_action="none"`` comments are ignored entirely.
        """

        by_paragraph: dict[str, list[EditorialComment]] = {}
        for comment in comments:
            if comment.ai_action == "none":
                continue
            by_paragraph.setdefault(comment.paragraph_id, []).append(comment)

        rewrite_targets = [
            para
            for para in draft.paragraphs
            if any(c.ai_action == "rewrite" for c in by_paragraph.get(para.id, []))
        ]

        rewritten_texts: dict[str, str] = {}
        if rewrite_targets:
            prompt = _rewrite_prompt(draft, rewrite_targets, by_paragraph)
            output = self._provider.generate(
                _RewriteOutput, REWRITE_SYSTEM, prompt, operation="rewrite"
            )
            self._validate_rewrite_output(rewrite_targets, output, by_paragraph)
            rewritten_texts = {p.paragraph_id: p.text for p in output.paragraphs}

        new_paragraphs: list[DraftParagraph] = []
        for para in draft.paragraphs:
            text = rewritten_texts.get(para.id, para.text)
            new_paragraphs.append(DraftParagraph(id=para.id, text=text))

        return DraftRevision(
            id=str(uuid.uuid4()),
            narrative_plan_id=draft.narrative_plan_id,
            paragraphs=new_paragraphs,
            editorial_text="\n\n".join(p.text for p in new_paragraphs),
            parent_id=draft.id,
            change_source="rewrite",
            author_note="",
            created_at=datetime.now(UTC),
        )

    # ------------------------------------------------------------------
    def _validate_rewrite_output(
        self,
        targets: list[DraftParagraph],
        output: _RewriteOutput,
        comments_by_paragraph: dict[str, list[EditorialComment]],
    ) -> None:
        target_ids = [p.id for p in targets]
        returned_ids = [p.paragraph_id for p in output.paragraphs]
        if returned_ids != target_ids:
            raise ValueError(
                f"rewrite paragraph_ids {returned_ids!r} do not match targets "
                f"{target_ids!r}"
            )

        originals = {p.id: p.text for p in targets}
        for rewritten in output.paragraphs:
            for comment in comments_by_paragraph.get(rewritten.paragraph_id, []):
                if comment.ai_action != "rewrite":
                    continue
                if comment.start_offset == 0 and comment.end_offset == 0:
                    continue
                original = originals[rewritten.paragraph_id]
                span = original[comment.start_offset : comment.end_offset]
                if span and span not in rewritten.text:
                    raise ProtectedSpanViolation(rewritten.paragraph_id, span)


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------


def _rewrite_prompt(
    draft: DraftRevision,
    targets: list[DraftParagraph],
    comments_by_paragraph: dict[str, list[EditorialComment]],
) -> str:
    para_blocks: list[str] = []
    for para in targets:
        comments = comments_by_paragraph.get(para.id, [])
        comment_lines: list[str] = []
        for comment in comments:
            if comment.start_offset == 0 and comment.end_offset == 0:
                comment_lines.append(f"- [{comment.kind}] {comment.body}")
            else:
                original = para.text[comment.start_offset : comment.end_offset]
                comment_lines.append(
                    f"- [{comment.kind}] {comment.body}（保留原句：{original!r}）"
                )
        para_blocks.append(
            f"段落 {para.id}（当前文本：{para.text}）\n"
            f"批注：\n" + "\n".join(comment_lines)
        )
    return (
        "请基于人类编辑的批注重写以下段落。被标记为「保留原句」的文字必须原封不动地保留在重写后的文本中。\n\n"
        f"draft_artifact_id：{draft.id}\n\n"
        "段落（顺序即输出顺序）：\n" + "\n\n".join(para_blocks) + "\n"
    )


def _empty_research() -> ResearchPacket:
    return ResearchPacket(
        mechanisms=[],
        fact_cards=[],
        people_events=[],
        concrete_scenes=[],
        visual_details=[],
        uncertainties=[],
        sources=[],
    )


# ---------------------------------------------------------------------------
# comment repositories
# ---------------------------------------------------------------------------


def create_comment(
    project_id: str,
    draft_artifact_id: str,
    paragraph_id: str,
    start_offset: int,
    end_offset: int,
    kind: str,
    body: str,
    ai_action: str,
    session: Session,
) -> EditorialComment:
    """Insert an editorial comment row and return its Pydantic mirror.

    The route handler is responsible for validating offsets against the
    referenced paragraph text; this function trusts the inputs and only
    enforces the ``ai_action`` whitelist at the ORM layer.
    """

    row = OrmEditorialComment(
        draft_artifact_id=draft_artifact_id,
        paragraph_id=paragraph_id,
        start_offset=start_offset,
        end_offset=end_offset,
        kind=kind,
        body=body,
        ai_action=ai_action,
    )
    session.add(row)
    session.flush()
    return _to_pydantic(row)


def list_comments(
    project_id: str, draft_artifact_id: str, session: Session
) -> list[EditorialComment]:
    """Return every comment attached to ``draft_artifact_id`` as Pydantic mirrors."""

    stmt = (
        select(OrmEditorialComment)
        .where(OrmEditorialComment.draft_artifact_id == draft_artifact_id)
        .order_by(OrmEditorialComment.created_at.asc(), OrmEditorialComment.id.asc())
    )
    rows = list(session.execute(stmt).scalars().all())
    return [_to_pydantic(r) for r in rows]


def _to_pydantic(row: OrmEditorialComment) -> EditorialComment:
    return EditorialComment(
        id=row.id,
        draft_artifact_id=row.draft_artifact_id,
        paragraph_id=row.paragraph_id,
        start_offset=row.start_offset,
        end_offset=row.end_offset,
        kind=row.kind,
        body=row.body,
        ai_action=row.ai_action,  # type: ignore[arg-type]
        processed_in_revision=row.processed_in_revision,
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# approval
# ---------------------------------------------------------------------------


def approve_draft(
    project_id: str,
    draft_artifact_id: str,
    session: Session,
) -> Artifact:  # noqa: F821 — forward ref to avoid circular import
    """Fold a draft into a frozen :class:`ApprovedScript`.

    Refuses (raises :class:`NewerDraftExists`) if any other
    ``kind="draft"`` revision has a higher revision number than the
    target — the editor must explicitly approve the newer revision
    rather than have an older one silently win.

    Returns the newly created ``Artifact`` row; the head pointer for
    ``kind="approved_script"`` is moved onto it.
    """


    repo = ArtifactRepository(session)
    draft = repo.get(draft_artifact_id)
    if draft is None:
        raise LookupError(f"artifact {draft_artifact_id!r} not found")
    if draft.project_id != project_id:
        raise ValueError(
            f"artifact {draft_artifact_id!r} belongs to project {draft.project_id!r}, "
            f"not {project_id!r}"
        )
    if draft.kind != "draft":
        raise ValueError(
            f"artifact {draft_artifact_id!r} is kind {draft.kind!r}, not 'draft'"
        )

    for rev in repo.list_revisions(project_id, "draft"):
        if rev.id != draft_artifact_id and rev.revision > draft.revision:
            raise NewerDraftExists(draft_artifact_id)

    revision = DraftRevision.model_validate(draft.payload)
    fact_card_ids = _aggregate_fact_card_ids(repo, project_id, revision.narrative_plan_id)

    approved = ApprovedScript(
        id=str(uuid.uuid4()),
        draft_revision_id=draft_artifact_id,
        editorial_text=revision.editorial_text,
        structure=[p.id for p in revision.paragraphs],
        fact_card_ids=fact_card_ids,
        approved_at=datetime.now(UTC),
    )

    artifact = repo.create(
        project_id,
        "approved_script",
        approved.model_dump(mode="json"),
        parent_id=draft_artifact_id,
        created_by="editor",
    )
    repo.accept(project_id, artifact.id)

    stmt = select(OrmEditorialComment).where(
        OrmEditorialComment.draft_artifact_id == draft_artifact_id
    )
    for comment in session.execute(stmt).scalars().all():
        comment.processed_in_revision = artifact.id
    session.commit()

    return artifact


def _aggregate_fact_card_ids(
    repo: ArtifactRepository, project_id: str, narrative_plan_id: str
) -> list[str]:
    """Roll up the fact-card ids cited by the plan backing the draft.

    The draft's ``narrative_plan_id`` is a content-level id (e.g.
    ``"plan-1"``), not an artifact id — walk the project's narrative
    revisions newest-first and pick the plan whose ``plan.id`` matches.
    """

    for rev in repo.list_revisions(project_id, "narrative"):
        try:
            plan = NarrativePlan.model_validate(rev.payload)
        except ValidationError:
            continue
        if plan.id != narrative_plan_id:
            continue
        ids: list[str] = []
        for beat in plan.beats:
            ids.extend(beat.fact_card_ids)
        return ids
    return []


__all__ = [
    "REWRITE_SYSTEM",
    "NewerDraftExists",
    "ProtectedSpanViolation",
    "ReviewService",
    "approve_draft",
    "create_comment",
    "list_comments",
    "validate_offsets",
]