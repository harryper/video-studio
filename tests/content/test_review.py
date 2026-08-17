"""Review service: paragraph-anchored comments, protected spans,
targeted rewrite, and immutable approval.

Two contracts from the design spec drive these tests:

1. Protected spans (offsets inside a 'rewrite' comment) survive the
   rewrite byte-for-byte. If the model drops them, the service
   refuses to ship the new revision.
2. Only paragraphs that have at least one ``ai_action="rewrite"``
   comment get sent to the model; everything else is byte-equal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from studio.content.review import (
    NewerDraftExists,
    ProtectedSpanViolation,
    ReviewService,
    approve_draft,
    create_comment,
    list_comments,
    validate_offsets,
)
from studio.models import EditorialComment as OrmEditorialComment
from studio.providers.fake import FakeModelProvider
from studio.schemas import (
    ApprovedScript,
    DraftParagraph,
    DraftRevision,
    EditorialComment,
    FactCard,
    NarrativeBeat,
    NarrativePlan,
    ResearchPacket,
    SourceDocument,
)

# ---------------------------------------------------------------------------
# Test helpers / fixtures
# ---------------------------------------------------------------------------


class _RewriteParagraph(BaseModel):
    paragraph_id: str
    text: str


class _RewriteOutput(BaseModel):
    paragraphs: list[_RewriteParagraph]


def _draft() -> DraftRevision:
    return DraftRevision(
        id="d1",
        narrative_plan_id="plan-1",
        paragraphs=[
            DraftParagraph(id="p1", text="第一段开场内容。"),
            DraftParagraph(id="p2", text="第二段正文，真正的原因在供应链。"),
            DraftParagraph(id="p3", text="第三段结尾。"),
        ],
        editorial_text="第一段开场内容。\n\n第二段正文，真正的原因在供应链。\n\n第三段结尾。",
        change_source="initial",
        created_at=datetime(2026, 8, 18, tzinfo=UTC),
    )


def _now() -> datetime:
    return datetime(2026, 8, 18, tzinfo=UTC)


def protect(text: str) -> EditorialComment:
    """Find ``text`` in the canonical test draft; return a protected-span comment."""

    d = _draft()
    for para in d.paragraphs:
        idx = para.text.find(text)
        if idx != -1:
            return EditorialComment(
                id=str(uuid4()),
                draft_artifact_id=d.id,
                paragraph_id=para.id,
                start_offset=idx,
                end_offset=idx + len(text),
                kind="保留原句",
                body=f"保留 {text}",
                ai_action="rewrite",
                processed_in_revision=None,
                created_at=_now(),
            )
    raise ValueError(f"text {text!r} not found in test draft paragraphs")


def comment_on(paragraph_id: str, body: str) -> EditorialComment:
    """A 'rewrite' comment anchored to ``paragraph_id`` with no protected span."""

    return EditorialComment(
        id=str(uuid4()),
        draft_artifact_id="d1",
        paragraph_id=paragraph_id,
        start_offset=0,
        end_offset=0,
        kind="comment",
        body=body,
        ai_action="rewrite",
        processed_in_revision=None,
        created_at=_now(),
    )


def note_on(paragraph_id: str, body: str) -> EditorialComment:
    """A 'note' comment — recorded but the paragraph is not sent to the rewrite model."""

    return EditorialComment(
        id=str(uuid4()),
        draft_artifact_id="d1",
        paragraph_id=paragraph_id,
        start_offset=0,
        end_offset=0,
        kind="comment",
        body=body,
        ai_action="note",
        processed_in_revision=None,
        created_at=_now(),
    )


def noop_on(paragraph_id: str, body: str) -> EditorialComment:
    """A 'none' comment — ignored entirely by the rewrite."""

    return EditorialComment(
        id=str(uuid4()),
        draft_artifact_id="d1",
        paragraph_id=paragraph_id,
        start_offset=0,
        end_offset=0,
        kind="comment",
        body=body,
        ai_action="none",
        processed_in_revision=None,
        created_at=_now(),
    )


@pytest.fixture
def draft() -> DraftRevision:
    return _draft()


@pytest.fixture
def review_service(draft: DraftRevision) -> ReviewService:
    """Provider returns p2 with the protected span preserved; p1/p3 unchanged."""

    provider = FakeModelProvider()
    provider.queue(
        "rewrite",
        _RewriteOutput(
            paragraphs=[
                _RewriteParagraph(
                    paragraph_id="p2",
                    text="重写后的第二段，真正的原因在供应链依然成立。",
                ),
            ]
        ),
    )
    return ReviewService(provider)


# ---------------------------------------------------------------------------
# Verbatim-from-brief tests
# ---------------------------------------------------------------------------


def test_protected_span_survives_rewrite_exactly(
    review_service: ReviewService, draft: DraftRevision
) -> None:
    revised = review_service.rewrite(draft, [protect("真正的原因在供应链")])
    assert "真正的原因在供应链" in revised.editorial_text


def test_rewrite_touches_only_selected_paragraphs(draft: DraftRevision) -> None:
    provider = FakeModelProvider()
    provider.queue(
        "rewrite",
        _RewriteOutput(
            paragraphs=[_RewriteParagraph(paragraph_id="p2", text="重写后的第二段。")]
        ),
    )
    service = ReviewService(provider)
    revised = service.rewrite(draft, [comment_on("p2", "这里没讲懂")])
    assert revised.paragraph("p1") == draft.paragraph("p1")
    assert revised.paragraph("p3") == draft.paragraph("p3")


# ---------------------------------------------------------------------------
# offset validation
# ---------------------------------------------------------------------------


def test_validate_offsets_accepts_no_range_sentinel() -> None:
    """``(0, 0)`` means "no protected range" — must pass."""

    validate_offsets("hello", 0, 0)


def test_validate_offsets_accepts_in_range() -> None:
    validate_offsets("hello world", 0, 5)
    validate_offsets("hello world", 6, 11)


def test_validate_offsets_rejects_negative_start() -> None:
    with pytest.raises(ValueError):
        validate_offsets("hello", -1, 3)


def test_validate_offsets_rejects_end_le_start() -> None:
    with pytest.raises(ValueError):
        validate_offsets("hello", 3, 3)
    with pytest.raises(ValueError):
        validate_offsets("hello", 4, 3)


def test_validate_offsets_rejects_end_past_length() -> None:
    with pytest.raises(ValueError):
        validate_offsets("hello", 0, 100)


# ---------------------------------------------------------------------------
# ai_action semantics
# ---------------------------------------------------------------------------


def test_note_comment_leaves_paragraph_byte_equal(draft: DraftRevision) -> None:
    provider = FakeModelProvider()
    # No rewrite queued — provider would raise if called.
    service = ReviewService(provider)
    revised = service.rewrite(draft, [note_on("p2", "记一下，备用")])
    assert revised.paragraph("p2") == draft.paragraph("p2")
    assert provider.responses.get("rewrite", []) == []


def test_none_comment_is_ignored_entirely(draft: DraftRevision) -> None:
    provider = FakeModelProvider()
    service = ReviewService(provider)
    revised = service.rewrite(draft, [noop_on("p2", "忽略这条")])
    assert revised.paragraph("p2") == draft.paragraph("p2")
    # Model was never called because no rewrite action was triggered.
    assert provider.responses.get("rewrite", []) == []


def test_no_comments_returns_byte_equal_revision(draft: DraftRevision) -> None:
    """No comments at all => no rewrite happens; result is byte-equal."""

    provider = FakeModelProvider()
    service = ReviewService(provider)
    revised = service.rewrite(draft, [])
    assert revised.editorial_text == draft.editorial_text
    assert provider.responses.get("rewrite", []) == []


# ---------------------------------------------------------------------------
# protected span enforcement
# ---------------------------------------------------------------------------


def test_protected_span_violation_raises_when_model_drops_span(
    draft: DraftRevision,
) -> None:
    provider = FakeModelProvider()
    provider.queue(
        "rewrite",
        _RewriteOutput(
            paragraphs=[
                _RewriteParagraph(
                    paragraph_id="p2",
                    text="重写后整段话都不一样了。",  # span dropped
                )
            ]
        ),
    )
    service = ReviewService(provider)
    with pytest.raises(ProtectedSpanViolation):
        service.rewrite(draft, [protect("真正的原因在供应链")])


# ---------------------------------------------------------------------------
# rewrite output metadata
# ---------------------------------------------------------------------------


def test_rewrite_output_metadata(review_service: ReviewService, draft: DraftRevision) -> None:
    revised = review_service.rewrite(draft, [comment_on("p2", "重写 p2")])
    assert [p.id for p in revised.paragraphs] == ["p1", "p2", "p3"]
    assert revised.parent_id == draft.id
    assert revised.change_source == "rewrite"
    assert revised.editorial_text == "\n\n".join(p.text for p in revised.paragraphs)


def test_rewrite_copies_paragraphs_byte_equal_when_no_comment(
    review_service: ReviewService, draft: DraftRevision
) -> None:
    """A comment on p2 still copies p1 and p3 byte-for-byte into the new revision."""

    revised = review_service.rewrite(draft, [comment_on("p2", "重写 p2")])
    p1_obj = next(p for p in revised.paragraphs if p.id == "p1")
    p3_obj = next(p for p in revised.paragraphs if p.id == "p3")
    assert p1_obj.text == draft.paragraph("p1")
    assert p3_obj.text == draft.paragraph("p3")


# ---------------------------------------------------------------------------
# comment repository (create / list)
# ---------------------------------------------------------------------------


def test_create_comment_persists_row(
    session: Session, project, draft: DraftRevision
) -> None:
    """``create_comment`` writes an EditorialComment row."""

    from studio.artifacts import ArtifactRepository

    repo = ArtifactRepository(session)
    artifact = repo.create(project.id, "draft", draft.model_dump(mode="json"))
    repo.accept(project.id, artifact.id)
    session.commit()

    create_comment(
        project.id,
        artifact.id,
        paragraph_id="p2",
        start_offset=0,
        end_offset=4,
        kind="comment",
        body="人工反馈",
        ai_action="rewrite",
        session=session,
    )
    session.commit()

    rows = (
        session.query(OrmEditorialComment)
        .filter_by(draft_artifact_id=artifact.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].body == "人工反馈"
    assert rows[0].ai_action == "rewrite"


def test_list_comments_returns_only_for_requested_draft(
    session: Session, project, draft: DraftRevision
) -> None:
    from studio.artifacts import ArtifactRepository

    repo = ArtifactRepository(session)
    a = repo.create(project.id, "draft", draft.model_dump(mode="json"))
    repo.accept(project.id, a.id)
    b = repo.create(project.id, "draft", draft.model_dump(mode="json"))
    repo.accept(project.id, b.id)
    session.commit()

    create_comment(project.id, a.id, "p1", 0, 0, "comment", "a 上的", "rewrite", session)
    create_comment(project.id, b.id, "p1", 0, 0, "comment", "b 上的", "rewrite", session)
    session.commit()

    a_comments = list_comments(project.id, a.id, session)
    b_comments = list_comments(project.id, b.id, session)
    assert {c.body for c in a_comments} == {"a 上的"}
    assert {c.body for c in b_comments} == {"b 上的"}


# ---------------------------------------------------------------------------
# ApprovedScript frozen defence
# ---------------------------------------------------------------------------


def test_approved_script_editorial_text_is_frozen() -> None:
    approved = ApprovedScript(
        id="a1",
        draft_revision_id="d1",
        editorial_text="正文",
        structure=["p1"],
        fact_card_ids=[],
        approved_at=_now(),
    )
    with pytest.raises(ValidationError):
        approved.editorial_text = "新正文"


def test_validate_payload_normalises_approved_script() -> None:
    """``validate_payload('approved_script', ...)`` round-trips through ApprovedScript."""

    from studio.schemas import validate_payload

    approved = ApprovedScript(
        id="a1",
        draft_revision_id="d1",
        editorial_text="正文",
        structure=["p1", "p2"],
        fact_card_ids=["fact-1"],
        approved_at=_now(),
    )
    payload = approved.model_dump(mode="json")
    payload["payload_kind"] = "approved_script"
    normalised = validate_payload("approved_script", payload)
    assert normalised["payload_kind"] == "approved_script"
    assert normalised["editorial_text"] == "正文"


# ---------------------------------------------------------------------------
# approve_draft
# ---------------------------------------------------------------------------


def _seed_for_approve(
    session: Session,
    project,
    *,
    extra_draft: bool = False,
) -> dict[str, str]:
    """Seed research + narrative + draft artifacts; optionally a second (newer) draft."""

    from studio.artifacts import ArtifactRepository

    repo = ArtifactRepository(session)
    doc = SourceDocument(
        title="x", url="https://example.com/x", snippet="x", publisher="x"
    )
    research = ResearchPacket(
        mechanisms=["m"],
        fact_cards=[
            FactCard(
                claim="c1",
                narrative_value="nv",
                confidence=0.9,
                risk="number",
                sources=[doc],
                verification_status="verified",
                payoff_critical=True,
            )
        ],
        people_events=[],
        concrete_scenes=[],
        visual_details=[],
        uncertainties=[],
        sources=[doc],
    )
    plan = NarrativePlan(
        id="plan-1",
        pitch_id="pitch-1",
        beats=[
            NarrativeBeat(
                id="p1",
                purpose="setup",
                fact_card_ids=["c1"],
                new_information="...",
                next_question="...",
                withheld_information="",
            ),
            NarrativeBeat(
                id="p2",
                purpose="payoff",
                fact_card_ids=["c1"],
                new_information="...",
                next_question="",
                withheld_information="",
            ),
        ],
        created_at=_now(),
    )
    draft = _draft()

    ids: dict[str, str] = {}
    a = repo.create(project.id, "research", research.model_dump(mode="json"))
    repo.accept(project.id, a.id)
    ids["research"] = a.id

    a = repo.create(project.id, "narrative", plan.model_dump(mode="json"))
    repo.accept(project.id, a.id)
    ids["narrative"] = a.id

    a = repo.create(project.id, "draft", draft.model_dump(mode="json"))
    repo.accept(project.id, a.id)
    ids["draft"] = a.id

    if extra_draft:
        a = repo.create(project.id, "draft", draft.model_dump(mode="json"))
        repo.accept(project.id, a.id)
        ids["draft_newer"] = a.id

    session.commit()
    return ids


def test_approve_draft_creates_approved_script_artifact(
    session: Session, project
) -> None:
    ids = _seed_for_approve(session, project)
    artifact = approve_draft(project.id, ids["draft"], session)
    assert artifact.kind == "approved_script"
    assert artifact.payload["payload_kind"] == "approved_script"
    assert artifact.payload["editorial_text"] == _draft().editorial_text
    assert artifact.payload["structure"] == ["p1", "p2", "p3"]
    assert "c1" in artifact.payload["fact_card_ids"]


def test_approve_draft_moves_head_pointer(session: Session, project) -> None:
    from studio.artifacts import ArtifactRepository

    ids = _seed_for_approve(session, project)
    artifact = approve_draft(project.id, ids["draft"], session)
    head = ArtifactRepository(session).current(project.id, "approved_script")
    assert head is not None
    assert head.id == artifact.id


def test_approve_draft_marks_comments_processed(session: Session, project) -> None:
    ids = _seed_for_approve(session, project)
    create_comment(
        project.id,
        ids["draft"],
        "p2",
        0,
        0,
        "comment",
        "feedback",
        "rewrite",
        session,
    )
    session.commit()

    artifact = approve_draft(project.id, ids["draft"], session)
    session.expire_all()
    rows = (
        session.query(OrmEditorialComment)
        .filter_by(draft_artifact_id=ids["draft"])
        .all()
    )
    assert len(rows) == 1
    assert rows[0].processed_in_revision == artifact.id


def test_approve_draft_refuses_when_newer_draft_exists(
    session: Session, project
) -> None:
    ids = _seed_for_approve(session, project, extra_draft=True)
    with pytest.raises(NewerDraftExists):
        approve_draft(project.id, ids["draft"], session)