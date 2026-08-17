"""Narrative-plan service tests.

The narrative stage turns an accepted :class:`StoryPitch` plus a
:class:`ResearchPacket` into a :class:`NarrativePlan` — an ordered list of
``≥ 3`` stable-id beats that the writer can follow without inventing
facts the research stage never produced.

Contract pinned here:

* ``beats`` carry ``purpose``, ``new_information`` and (unless the beat's
  purpose is ``"question"``) at least one ``fact_card_id``.
* Beat IDs are stable (``b1``, ``b2``, …) and unique within a plan.
* ``plan.pitch_id`` equals ``pitch.id``.
* Duration derives from a spoken-character baseline (Chinese ~3 chars/sec)
  and the helper flags when the estimate falls outside 60–360 seconds.
* System prompts forbid canned phrases, mandatory five-act structure, and
  mandatory reversal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel

from studio.content.narratives import (
    NARRATIVE_SYSTEM,
    AntiTemplateViolation,
    NarrativeService,
    plan_narrative,
)
from studio.providers.fake import FakeModelProvider
from studio.schemas import (
    FactCard,
    NarrativeBeat,
    NarrativePlan,
    ResearchPacket,
    SourceDocument,
    StoryPitch,
)

# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


class _BeatDraft(BaseModel):
    """Model output for one beat (the service assigns the stable ``id``)."""

    purpose: str
    fact_card_ids: list[str]
    new_information: str
    next_question: str
    withheld_information: str


class _BeatList(BaseModel):
    """Wrapper around the model's beats list (matches service expectation)."""

    beats: list[_BeatDraft]


def _doc() -> SourceDocument:
    return SourceDocument(
        title="Ocean salinity",
        url="https://example.com/salinity",
        snippet="平均盐度约 35‰",
        publisher="Example",
    )


def _research() -> ResearchPacket:
    return ResearchPacket(
        mechanisms=["岩石风化把离子带入河流，再进入海洋"],
        fact_cards=[
            FactCard(
                claim="海洋平均盐度约 35‰",
                narrative_value="给观众一个可记住的量级",
                confidence=0.9,
                risk="number",
                sources=[_doc()],
                verification_status="verified",
                payoff_critical=True,
            ),
            FactCard(
                claim="河流每年带入海洋约 4×10⁸ 吨溶解物",
                narrative_value="量化持续输入",
                confidence=0.85,
                risk="number",
                sources=[_doc()],
                verification_status="verified",
                payoff_critical=False,
            ),
            FactCard(
                claim="盐结晶需要蒸发",
                narrative_value="机制支撑",
                confidence=0.8,
                risk="ordinary",
                sources=[],
                verification_status="verified",
                payoff_critical=False,
            ),
        ],
        people_events=["1872 年挑战者号考察"],
        concrete_scenes=["盐田里结晶的白色硬壳"],
        visual_details=["显微镜下的立方体盐晶"],
        uncertainties=["早期海洋盐度的绝对值仍有争议"],
        sources=[_doc()],
    )


def _pitch() -> StoryPitch:
    return StoryPitch(
        id="pitch-1",
        investigation_question="为什么河流把盐送进海洋，海水却没有越来越咸？",
        opening_scene="盐田里结晶的白色硬壳",
        evidence_path="从风化输入到盐度平衡",
        payoff="理解海洋是一个长期平衡的系统",
        why_it_works="把观众熟悉的盐田结晶放进开场，再扩展到全球盐预算",
        estimated_duration_sec=180,
        risks=[],
    )


def _beat(
    purpose: str,
    new_info: str,
    *,
    fact_card_ids: list[str] | None = None,
    next_question: str = "下一步要查什么？",
    withheld: str = "尚未揭示的关键",
) -> _BeatDraft:
    return _BeatDraft(
        purpose=purpose,
        fact_card_ids=fact_card_ids or [],
        new_information=new_info,
        next_question=next_question,
        withheld_information=withheld,
    )


def _provider(*beats: _BeatDraft) -> FakeModelProvider:
    return FakeModelProvider({"narrative": [_BeatList(beats=list(beats))]})


@pytest.fixture
def service() -> NarrativeService:
    return NarrativeService(_provider(_beat("setup", "开场", fact_card_ids=["海洋平均盐度约 35‰"]),
                                      _beat("evidence", "证据 1", fact_card_ids=["河流每年带入海洋约 4×10⁸ 吨溶解物"]),
                                      _beat("question", "提出开放问题")))


@pytest.fixture
def plan(service: NarrativeService) -> NarrativePlan:
    return service.plan(_pitch(), _research())


class _RecordingProvider(FakeModelProvider):
    """Records every ``generate`` call so we can assert on the prompt."""

    def __init__(self, responses: dict[str, list[Any]] | None = None) -> None:
        super().__init__(responses)
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        schema: type[BaseModel],
        system: str,
        prompt: str,
        *,
        operation: str,
    ) -> BaseModel:
        self.calls.append(
            {
                "schema": schema,
                "system": system,
                "prompt": prompt,
                "operation": operation,
            }
        )
        return super().generate(schema, system, prompt, operation=operation)


# ---------------------------------------------------------------------------
# verbatim-from-brief tests
# ---------------------------------------------------------------------------


def test_every_beat_advances_investigation(plan: NarrativePlan) -> None:
    assert all(beat.purpose and beat.new_information for beat in plan.beats)
    assert all(beat.fact_card_ids for beat in plan.beats if beat.purpose != "question")


# ---------------------------------------------------------------------------
# structural invariants
# ---------------------------------------------------------------------------


def test_plan_has_at_least_three_beats(plan: NarrativePlan) -> None:
    assert len(plan.beats) >= 3


def test_beat_ids_are_stable_and_unique(plan: NarrativePlan) -> None:
    ids = [beat.id for beat in plan.beats]
    assert all(ids), "every beat must have a non-empty id"
    assert len(set(ids)) == len(ids), "beat ids must be unique"


def test_plan_pitch_id_matches_pitch(plan: NarrativePlan) -> None:
    pitch = _pitch()
    assert plan.pitch_id == pitch.id


def test_plan_uses_distinct_beat_id_pattern(plan: NarrativePlan) -> None:
    """Beat IDs follow the documented ``bN`` pattern (stable across replays)."""

    assert plan.beats[0].id == "b1"
    assert plan.beats[1].id == "b2"
    assert plan.beats[2].id == "b3"


# ---------------------------------------------------------------------------
# duration estimate
# ---------------------------------------------------------------------------


def test_estimate_duration_chinese_baseline() -> None:
    """Chinese ~3 chars/sec baseline: 180 chars ⇒ ~60 seconds."""

    service = NarrativeService(_provider(_beat("setup", "x", fact_card_ids=["a"]),
                                          _beat("evidence", "y", fact_card_ids=["a"]),
                                          _beat("question", "z")))
    plan = NarrativePlan(
        id="p",
        pitch_id="pitch-1",
        beats=[
            NarrativeBeat(
                id="b1",
                purpose="setup",
                fact_card_ids=["a"],
                new_information="x" * 60,  # 60 chars
                next_question="q",
                withheld_information="w",
            ),
            NarrativeBeat(
                id="b2",
                purpose="evidence",
                fact_card_ids=["a"],
                new_information="y" * 60,  # 60 chars
                next_question="q",
                withheld_information="w",
            ),
            NarrativeBeat(
                id="b3",
                purpose="evidence",
                fact_card_ids=["a"],
                new_information="z" * 60,  # 60 chars
                next_question="q",
                withheld_information="w",
            ),
        ],
        created_at=datetime.now(UTC),
    )

    seconds, in_range = service.estimate_duration_sec(plan)
    assert seconds == 60  # 180 chars / 3 chars per sec
    assert in_range is True


def test_estimate_duration_out_of_range_warns() -> None:
    """A 12-character beat (4 seconds) is below the 60-second floor."""

    service = NarrativeService(_provider(_beat("setup", "x", fact_card_ids=["a"]),
                                          _beat("evidence", "y", fact_card_ids=["a"]),
                                          _beat("question", "z")))
    plan = NarrativePlan(
        id="p",
        pitch_id="pitch-1",
        beats=[
            NarrativeBeat(
                id="b1",
                purpose="setup",
                fact_card_ids=["a"],
                new_information="x" * 4,
                next_question="q",
                withheld_information="w",
            ),
            NarrativeBeat(
                id="b2",
                purpose="evidence",
                fact_card_ids=["a"],
                new_information="y" * 4,
                next_question="q",
                withheld_information="w",
            ),
            NarrativeBeat(
                id="b3",
                purpose="evidence",
                fact_card_ids=["a"],
                new_information="z" * 4,
                next_question="q",
                withheld_information="w",
            ),
        ],
        created_at=datetime.now(UTC),
    )

    seconds, in_range = service.estimate_duration_sec(plan)
    assert seconds == 4
    assert in_range is False


def test_estimate_duration_works_on_draft_revision() -> None:
    """``estimate_duration_sec`` accepts a DraftRevision too."""

    from studio.schemas import DraftParagraph, DraftRevision

    service = NarrativeService(_provider(_beat("setup", "x", fact_card_ids=["a"]),
                                          _beat("evidence", "y", fact_card_ids=["a"]),
                                          _beat("question", "z")))
    draft = DraftRevision(
        id="d1",
        narrative_plan_id="p",
        paragraphs=[DraftParagraph(id="b1", text="a" * 90)],
        editorial_text="a" * 90,
        change_source="initial",
        created_at=datetime.now(UTC),
    )
    seconds, in_range = service.estimate_duration_sec(draft)
    assert seconds == 30  # 90 chars / 3 chars per sec
    assert in_range is False


# ---------------------------------------------------------------------------
# finalize
# ---------------------------------------------------------------------------


def test_finalize_rejects_plan_with_too_few_beats() -> None:
    service = NarrativeService(_provider(_beat("setup", "x", fact_card_ids=["a"]),
                                          _beat("evidence", "y", fact_card_ids=["a"])))
    tiny_plan = NarrativePlan(
        id="p",
        pitch_id="pitch-1",
        beats=[
            NarrativeBeat(
                id="b1",
                purpose="setup",
                fact_card_ids=["a"],
                new_information="x",
                next_question="q",
                withheld_information="w",
            ),
            NarrativeBeat(
                id="b2",
                purpose="evidence",
                fact_card_ids=["a"],
                new_information="y",
                next_question="q",
                withheld_information="w",
            ),
        ],
        created_at=datetime.now(UTC),
    )

    with pytest.raises(AntiTemplateViolation):
        service.finalize(tiny_plan)


def test_finalize_rejects_duplicate_beat_ids() -> None:
    service = NarrativeService(_provider(_beat("setup", "x", fact_card_ids=["a"]),
                                          _beat("evidence", "y", fact_card_ids=["a"]),
                                          _beat("question", "z")))
    dup_plan = NarrativePlan(
        id="p",
        pitch_id="pitch-1",
        beats=[
            NarrativeBeat(id="b1", purpose="setup", fact_card_ids=["a"], new_information="x", next_question="q", withheld_information="w"),
            NarrativeBeat(id="b1", purpose="evidence", fact_card_ids=["a"], new_information="y", next_question="q", withheld_information="w"),
            NarrativeBeat(id="b2", purpose="question", fact_card_ids=[], new_information="z", next_question="q", withheld_information="w"),
        ],
        created_at=datetime.now(UTC),
    )

    with pytest.raises(AntiTemplateViolation):
        service.finalize(dup_plan)


def test_finalize_rejects_non_question_beat_without_facts() -> None:
    service = NarrativeService(_provider(_beat("setup", "x"),
                                          _beat("evidence", "y", fact_card_ids=["a"]),
                                          _beat("question", "z")))
    bad_plan = NarrativePlan(
        id="p",
        pitch_id="pitch-1",
        beats=[
            NarrativeBeat(id="b1", purpose="setup", fact_card_ids=[], new_information="x", next_question="q", withheld_information="w"),
            NarrativeBeat(id="b2", purpose="evidence", fact_card_ids=["a"], new_information="y", next_question="q", withheld_information="w"),
            NarrativeBeat(id="b3", purpose="question", fact_card_ids=[], new_information="z", next_question="q", withheld_information="w"),
        ],
        created_at=datetime.now(UTC),
    )

    with pytest.raises(AntiTemplateViolation):
        service.finalize(bad_plan)


# ---------------------------------------------------------------------------
# system prompt
# ---------------------------------------------------------------------------


def test_narrative_system_prompt_forbids_canned_phrases_and_fixed_structure() -> None:
    for phrase in ("你以为", "这就有意思了", "离谱的是", "说白了", "关键是", "没了"):
        assert phrase in NARRATIVE_SYSTEM
    assert "五段" in NARRATIVE_SYSTEM
    assert "反转" in NARRATIVE_SYSTEM


def test_narrative_provider_receives_pitch_and_research() -> None:
    beats = [
        _beat("setup", "开场", fact_card_ids=["海洋平均盐度约 35‰"]),
        _beat("evidence", "证据 1", fact_card_ids=["河流每年带入海洋约 4×10⁸ 吨溶解物"]),
        _beat("question", "提出开放问题"),
    ]
    provider = _RecordingProvider({"narrative": [_BeatList(beats=beats)]})
    service = NarrativeService(provider)
    service.plan(_pitch(), _research())

    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["operation"] == "narrative"
    assert call["system"] == NARRATIVE_SYSTEM
    # The user prompt should include the pitch's investigation question and
    # the research packet's fact-card claims, so the model can pick from them.
    prompt = call["prompt"]
    assert _pitch().investigation_question in prompt
    for card in _research().fact_cards:
        assert card.claim in prompt


# ---------------------------------------------------------------------------
# module-level helper
# ---------------------------------------------------------------------------


def test_plan_narrative_module_helper(plan: NarrativePlan) -> None:
    beats = [
        _beat("setup", "开场", fact_card_ids=["海洋平均盐度约 35‰"]),
        _beat("evidence", "证据 1", fact_card_ids=["河流每年带入海洋约 4×10⁸ 吨溶解物"]),
        _beat("question", "提出开放问题"),
    ]
    provider = _provider(*beats)
    result = plan_narrative(_pitch(), _research(), provider)
    assert isinstance(result, NarrativePlan)
    assert len(result.beats) == 3
    assert result.pitch_id == _pitch().id