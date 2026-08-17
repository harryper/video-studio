"""Story-pitch service tests.

The pitch stage turns a ``TopicDiagnosis`` + ``ResearchPacket`` into three
genuinely different ``StoryPitch`` options for a human to choose between:

* three stable IDs that are never re-used;
* distinct ``(investigation_question, evidence_path)`` signatures — a pitch
  set where two options investigate the same question along the same
  evidence path is not a choice, it is the illusion of one;
* single-pitch regeneration that leaves the two untouched options byte-for-byte
  identical, so accepting "option 1" after revising "option 2" means what the
  editor thought it meant.
"""

from __future__ import annotations

import pytest

from studio.content.pitches import (
    PITCH_SYSTEM,
    PitchService,
    _PitchDraft,
    generate_pitches,
    regenerate_pitch,
)
from studio.providers.fake import FakeModelProvider
from studio.schemas import (
    FactCard,
    ResearchPacket,
    SourceDocument,
    StoryPitch,
    StoryPitchSet,
    TopicDiagnosis,
)

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def diagnosis_fixture() -> TopicDiagnosis:
    return TopicDiagnosis(
        core_question="为什么海水是咸的？",
        audience_prior_knowledge="知道海水咸，但不了解盐的来源",
        central_tension="河流一直注入淡水，海水却没有变淡",
        misconceptions=["海水就是溶解的食用盐"],
        scope=["盐的来源", "盐度平衡"],
        excluded_topics=["海洋化学实验教学"],
    )


def research_fixture() -> ResearchPacket:
    doc = SourceDocument(
        title="Ocean salinity",
        url="https://example.com/salinity",
        snippet="平均盐度约 35‰",
        publisher="Example",
    )
    return ResearchPacket(
        mechanisms=["岩石风化把离子带入河流，再进入海洋"],
        fact_cards=[
            FactCard(
                claim="海洋平均盐度约 35‰",
                narrative_value="给观众一个可记住的量级",
                confidence=0.9,
                risk="number",
                sources=[doc],
                verification_status="verified",
                payoff_critical=True,
            )
        ],
        people_events=["1872 年挑战者号考察"],
        concrete_scenes=["盐田里结晶的白色硬壳"],
        visual_details=["显微镜下的立方体盐晶"],
        uncertainties=["早期海洋盐度的绝对值仍有争议"],
        sources=[doc],
    )


def _draft(n: int, *, question: str | None = None, path: str | None = None) -> _PitchDraft:
    return _PitchDraft(
        investigation_question=question or f"问题 {n}",
        opening_scene=f"开场 {n}",
        evidence_path=path or f"证据路径 {n}",
        payoff=f"回报 {n}",
        why_it_works=f"为什么成立 {n}",
        estimated_duration_sec=180 + n,
        risks=[f"风险 {n}"],
    )


def _provider(*drafts: _PitchDraft) -> FakeModelProvider:
    return FakeModelProvider({"pitches": list(drafts)})


@pytest.fixture
def service() -> PitchService:
    return PitchService(_provider(_draft(1), _draft(2), _draft(3)))


@pytest.fixture
def original() -> StoryPitchSet:
    return PitchService(_provider(_draft(1), _draft(2), _draft(3))).generate(
        diagnosis_fixture(), research_fixture()
    )


# ---------------------------------------------------------------------------
# verbatim-from-brief tests
# ---------------------------------------------------------------------------


def test_pitches_use_distinct_questions_or_evidence_paths(service: PitchService) -> None:
    result = service.generate(diagnosis_fixture(), research_fixture())
    assert len(result.pitches) == 3
    assert service.effective_difference_rate(result.pitches) == 1.0


def test_single_pitch_regeneration_preserves_other_ids(original: StoryPitchSet) -> None:
    service = PitchService(_provider(_draft(9)))
    revised = service.regenerate(original, original.pitches[1].id, "不要历史路线")
    assert revised.pitches[0] == original.pitches[0]
    assert revised.pitches[2] == original.pitches[2]


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------


def test_generate_assigns_unique_stable_ids() -> None:
    result = PitchService(_provider(_draft(1), _draft(2), _draft(3))).generate(
        diagnosis_fixture(), research_fixture()
    )
    ids = [pitch.id for pitch in result.pitches]
    assert len(set(ids)) == 3
    assert all(ids)


def test_difference_rate_detects_duplicates() -> None:
    service = PitchService(_provider())
    pitch = StoryPitch(
        id="a",
        investigation_question="同一个问题",
        opening_scene="s",
        evidence_path="同一条路径",
        payoff="p",
        why_it_works="w",
        estimated_duration_sec=100,
        risks=[],
    )
    twin = pitch.model_copy(update={"id": "b"})
    assert service.effective_difference_rate([pitch, twin]) == 0.5


def test_colliding_pitch_is_regenerated_with_nudge() -> None:
    """A duplicate signature triggers a retry for the colliding entry only."""

    duplicate = _draft(1, question="问题 1", path="证据路径 1")
    provider = _provider(_draft(1), duplicate, _draft(2), _draft(3))
    service = PitchService(provider)
    result = service.generate(diagnosis_fixture(), research_fixture())

    assert service.effective_difference_rate(result.pitches) == 1.0
    # 4 calls: 3 pitches + 1 retry for the colliding second pitch.
    assert provider.responses["pitches"] == []


def test_persistent_collision_falls_back_to_uniqueness_suffix() -> None:
    """After max retries the service disambiguates rather than shipping a twin."""

    # 1 call for pitch 1, then 1 + 2 retries each for pitches 2 and 3.
    same = [_draft(1) for _ in range(7)]
    service = PitchService(_provider(*same))
    result = service.generate(diagnosis_fixture(), research_fixture())

    assert len(result.pitches) == 3
    assert service.effective_difference_rate(result.pitches) == 1.0


def test_system_prompt_forbids_canned_phrases_and_fixed_structure() -> None:
    for phrase in ("你以为", "这就有意思了", "离谱的是", "说白了", "关键是", "没了"):
        assert phrase in PITCH_SYSTEM
    assert "五段" in PITCH_SYSTEM
    assert "反转" in PITCH_SYSTEM


def test_module_level_generate_pitches_helper() -> None:
    provider = _provider(_draft(1), _draft(2), _draft(3))
    result = generate_pitches(diagnosis_fixture(), research_fixture(), provider)
    assert isinstance(result, StoryPitchSet)
    assert len(result.pitches) == 3


# ---------------------------------------------------------------------------
# regeneration
# ---------------------------------------------------------------------------


def test_regenerate_records_parent_and_feedback(original: StoryPitchSet) -> None:
    provider = _provider(_draft(9))
    revised = regenerate_pitch(original, original.pitches[1].id, "不要历史路线", provider)

    assert revised.id != original.id
    assert revised.parent_set_id == original.id
    assert revised.feedback == "不要历史路线"


def test_regenerate_replaces_only_the_target_payload(original: StoryPitchSet) -> None:
    target_id = original.pitches[1].id
    revised = PitchService(_provider(_draft(9))).regenerate(
        original, target_id, "不要历史路线"
    )

    assert revised.pitches[1].id == target_id
    assert revised.pitches[1].investigation_question == "问题 9"
    assert revised.pitches[1] != original.pitches[1]


def test_regenerate_rejects_unknown_pitch_id(original: StoryPitchSet) -> None:
    with pytest.raises(KeyError):
        PitchService(_provider(_draft(9))).regenerate(original, "no-such-id", "反馈")
