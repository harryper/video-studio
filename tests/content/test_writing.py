"""Draft + repetition-fingerprint service tests.

The draft stage turns a :class:`NarrativePlan` plus a
:class:`ResearchPacket` into a :class:`DraftRevision` whose
``paragraphs[*].id`` matches the plan's ``beats[*].id`` so editorial
comments can anchor to a beat across revisions.

The fingerprint stage compares a draft against recent drafts and reports
``must_replan=True`` when ANY fingerprint similarity exceeds the
threshold — never synonym-swap rewrites. ``rewrite_suggestions`` is
always ``[]`` per the brief.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from studio.content.fingerprints import (
    REPETITION_THRESHOLD,
    analyze_repetition,
    compute_fingerprints,
)
from studio.content.writing import (
    DRAFT_SYSTEM,
    AntiTemplateViolation,
    DraftService,
    write_draft,
)
from studio.providers.fake import FakeModelProvider
from studio.schemas import (
    DraftParagraph,
    DraftRevision,
    FactCard,
    NarrativeBeat,
    NarrativePlan,
    ResearchPacket,
    SourceDocument,
)

# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


class _ParagraphDraft(BaseModel):
    """Model output for one paragraph (text only; the service assigns the id)."""

    text: str


class _DraftDraft(BaseModel):
    """Model output for the full draft."""

    paragraphs: list[_ParagraphDraft]


def _doc() -> SourceDocument:
    return SourceDocument(
        title="Ocean",
        url="https://example.com/o",
        snippet="x",
        publisher="Ex",
    )


def _research() -> ResearchPacket:
    return ResearchPacket(
        mechanisms=["风化"],
        fact_cards=[
            FactCard(
                claim="海洋平均盐度约 35‰",
                narrative_value="量级",
                confidence=0.9,
                risk="number",
                sources=[_doc()],
                verification_status="verified",
                payoff_critical=True,
            )
        ],
        people_events=[],
        concrete_scenes=["盐田结晶"],
        visual_details=["立方晶体"],
        uncertainties=["早期盐度"],
        sources=[_doc()],
    )


def _plan() -> NarrativePlan:
    beats = [
        NarrativeBeat(
            id="b1",
            purpose="setup",
            fact_card_ids=["海洋平均盐度约 35‰"],
            new_information="开场陈述盐度的量级",
            next_question="这些盐从哪儿来？",
            withheld_information="盐度的输入机制尚未揭示",
        ),
        NarrativeBeat(
            id="b2",
            purpose="evidence",
            fact_card_ids=["海洋平均盐度约 35‰"],
            new_information="展示证据路径",
            next_question="为什么海水没越来越咸？",
            withheld_information="盐度平衡尚未揭示",
        ),
        NarrativeBeat(
            id="b3",
            purpose="payoff",
            fact_card_ids=["海洋平均盐度约 35‰"],
            new_information="揭开海洋的盐度平衡",
            next_question="",
            withheld_information="",
        ),
    ]
    return NarrativePlan(
        id="plan-1",
        pitch_id="pitch-1",
        beats=beats,
        created_at=datetime.now(UTC),
    )


def _draft_text(plan: NarrativePlan, *texts: str) -> _DraftDraft:
    """Build a model-output draft with one paragraph per beat in order."""

    if len(texts) != len(plan.beats):
        raise ValueError("must provide one text per beat")
    return _DraftDraft(paragraphs=[_ParagraphDraft(text=t) for t in texts])


def _provider(*drafts: _DraftDraft) -> FakeModelProvider:
    return FakeModelProvider({"draft": list(drafts)})


@pytest.fixture
def plan() -> NarrativePlan:
    return _plan()


@pytest.fixture
def service() -> DraftService:
    return DraftService(_provider(_draft_text(_plan(),
                                                "第一段说盐度大",
                                                "第二段说盐来自风化",
                                                "第三段说海洋是个平衡")))


# ---------------------------------------------------------------------------
# verbatim-from-brief tests
# ---------------------------------------------------------------------------


def test_canned_phrase_is_reported_not_reworded() -> None:
    report = analyze_repetition(_draft_with_text("这就有意思了，盐又多了"), [])
    assert report.must_replan is True
    assert report.rewrite_suggestions == []


# ---------------------------------------------------------------------------
# writing service
# ---------------------------------------------------------------------------


def test_draft_paragraph_ids_match_beat_ids(service: DraftService, plan: NarrativePlan) -> None:
    draft = service.draft(plan, _research())
    assert [p.id for p in draft.paragraphs] == [b.id for b in plan.beats]


def test_draft_links_to_plan(service: DraftService, plan: NarrativePlan) -> None:
    draft = service.draft(plan, _research())
    assert draft.narrative_plan_id == plan.id


def test_initial_draft_metadata(service: DraftService, plan: NarrativePlan) -> None:
    draft = service.draft(plan, _research())
    assert draft.parent_id is None
    assert draft.change_source == "initial"


def test_editorial_text_joins_paragraphs(service: DraftService, plan: NarrativePlan) -> None:
    draft = service.draft(plan, _research())
    assert draft.editorial_text == "\n\n".join(p.text for p in draft.paragraphs)


def test_finalize_rejects_draft_with_unknown_paragraph_id(plan: NarrativePlan) -> None:
    service = DraftService(_provider(_DraftDraft(paragraphs=[
        _ParagraphDraft(text="x"),
        _ParagraphDraft(text="y"),
        _ParagraphDraft(text="z"),
    ])))
    bad = DraftRevision(
        id="d",
        narrative_plan_id=plan.id,
        paragraphs=[
            DraftParagraph(id="b1", text="x"),
            DraftParagraph(id="b9", text="y"),  # bogus id
            DraftParagraph(id="b3", text="z"),
        ],
        editorial_text="x\n\ny\n\nz",
        change_source="initial",
        created_at=datetime.now(UTC),
    )

    with pytest.raises(AntiTemplateViolation):
        service.finalize(bad, plan)


def test_finalize_rejects_empty_paragraph(plan: NarrativePlan) -> None:
    service = DraftService(_provider())
    bad = DraftRevision(
        id="d",
        narrative_plan_id=plan.id,
        paragraphs=[
            DraftParagraph(id="b1", text="x"),
            DraftParagraph(id="b2", text=""),  # empty
            DraftParagraph(id="b3", text="z"),
        ],
        editorial_text="x\n\n\n\nz",
        change_source="initial",
        created_at=datetime.now(UTC),
    )

    with pytest.raises(AntiTemplateViolation):
        service.finalize(bad, plan)


def test_finalize_rejects_banned_substring(plan: NarrativePlan) -> None:
    service = DraftService(_provider())
    bad = DraftRevision(
        id="d",
        narrative_plan_id=plan.id,
        paragraphs=[
            DraftParagraph(id="b1", text="x"),
            DraftParagraph(id="b2", text="这就有意思了，y"),
            DraftParagraph(id="b3", text="z"),
        ],
        editorial_text="x\n\n这就有意思了，y\n\nz",
        change_source="initial",
        created_at=datetime.now(UTC),
    )

    with pytest.raises(AntiTemplateViolation):
        service.finalize(bad, plan)


def test_write_draft_module_helper(plan: NarrativePlan) -> None:
    provider = _provider(_draft_text(plan, "x", "y", "z"))
    draft = write_draft(plan, _research(), provider)
    assert isinstance(draft, DraftRevision)
    assert draft.narrative_plan_id == plan.id


def test_draft_system_prompt_forbids_canned_phrases_and_fixed_structure() -> None:
    for phrase in ("你以为", "这就有意思了", "离谱的是", "说白了", "关键是", "没了"):
        assert phrase in DRAFT_SYSTEM
    assert "五段" in DRAFT_SYSTEM
    assert "反转" in DRAFT_SYSTEM


def test_validate_payload_normalises_real_draft_revision() -> None:
    """``validate_payload('draft', ...)`` normalises a real DraftRevision."""

    from studio.schemas import DraftParagraph, DraftRevision, validate_payload

    revision = DraftRevision(
        id="d1",
        narrative_plan_id="p1",
        paragraphs=[DraftParagraph(id="b1", text="x")],
        editorial_text="x",
        change_source="initial",
        created_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    payload = revision.model_dump(mode="json")
    payload["payload_kind"] = "draft"

    normalised = validate_payload("draft", payload)
    assert normalised["payload_kind"] == "draft"
    assert normalised["id"] == "d1"


def test_validate_payload_passes_through_placeholder_drafts() -> None:
    """Legacy / placeholder payloads (no ``payload_kind``) pass through.

    ``tests/test_jobs.py`` exercises the artifact repository with
    placeholder ``{"text": ...}`` payloads for ``kind="draft"``. The
    draft validator must NOT reject them — real drafts always carry
    ``payload_kind="draft"`` because :class:`DraftRevision` declares
    the literal default, so the discriminator gates strict validation
    safely.
    """

    from studio.schemas import validate_payload

    assert validate_payload("draft", {"text": "placeholder"}) == {"text": "placeholder"}


# ---------------------------------------------------------------------------
# fingerprints
# ---------------------------------------------------------------------------


def _draft_with_text(text: str) -> DraftRevision:
    """Single-paragraph helper for fingerprint tests."""

    return DraftRevision(
        id="d",
        narrative_plan_id="p",
        paragraphs=[DraftParagraph(id="b1", text=text)],
        editorial_text=text,
        change_source="initial",
        created_at=datetime.now(UTC),
    )


def _long_draft(text: str, *, n_paragraphs: int = 1) -> DraftRevision:
    """Helper for tests that need a multi-paragraph draft."""

    paras = [
        DraftParagraph(id=f"b{i + 1}", text=(text if i == 0 else "占位段落 " + "x" * 60))
        for i in range(n_paragraphs)
    ]
    return DraftRevision(
        id="d",
        narrative_plan_id="p",
        paragraphs=paras,
        editorial_text="\n\n".join(p.text for p in paras),
        change_source="initial",
        created_at=datetime.now(UTC),
    )


def test_compute_fingerprints_returns_six_shapes() -> None:
    fps = compute_fingerprints(_long_draft("这是第一段。第二句继续说。", n_paragraphs=2))
    assert hasattr(fps, "opening_syntax")
    assert hasattr(fps, "transition_distribution")
    assert hasattr(fps, "reveal_position")
    assert hasattr(fps, "ending_shape")
    assert hasattr(fps, "comparison_patterns")
    assert hasattr(fps, "misconception_correction_pattern")


def test_repetition_threshold_is_pinned() -> None:
    """Threshold must be a single, named, exported float so it can be tuned."""

    assert isinstance(REPETITION_THRESHOLD, float)
    assert 0.0 < REPETITION_THRESHOLD < 1.0


def test_repetition_below_threshold_keeps_must_replan_false() -> None:
    a = _draft_with_text("这是一个完全不同的开场，跟历史没关系")
    b = _draft_with_text("今天的报道从另一个完全不同的事件切入")
    report = analyze_repetition(a, [b])
    assert report.must_replan is False
    assert report.rewrite_suggestions == []


def test_repetition_above_threshold_sets_must_replan_true() -> None:
    """Two drafts that share an opening canned phrase must trip must_replan."""

    canned = "这就有意思了"
    recent = _draft_with_text(f"{canned}，海洋的盐度增加了")
    current = _draft_with_text(f"{canned}，这次我们换个话题")
    report = analyze_repetition(current, [recent])
    assert report.must_replan is True
    assert report.rewrite_suggestions == []


def test_repetition_similarity_fields_are_populated() -> None:
    report = analyze_repetition(_draft_with_text("这是第一句话。第二句继续。"), [])
    for field in (
        "opening_syntax_similarity",
        "transition_distribution_similarity",
        "reveal_position_similarity",
        "ending_shape_similarity",
        "comparison_pattern_similarity",
        "misconception_correction_pattern_similarity",
    ):
        assert hasattr(report, field)
        assert isinstance(getattr(report, field), float)


def test_repetition_with_no_recent_drafts_is_false() -> None:
    """No history to compare against — no reason to replan."""

    report = analyze_repetition(_draft_with_text("这是第一段开场的文字"), [])
    assert report.must_replan is False
    assert report.rewrite_suggestions == []


def test_rewrite_suggestions_always_empty() -> None:
    """The contract is ``report and replan, do not synonym-swap``."""

    for text in (
        "这就有意思了",
        "你以为是这样吗",
        "这是很普通的第一段开场",
        "普通人以为光速恒定",
    ):
        report = analyze_repetition(_draft_with_text(text), [])
        assert report.rewrite_suggestions == []