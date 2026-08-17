"""Narrative-plan service.

Turns an accepted :class:`StoryPitch` + :class:`ResearchPacket` into a
:class:`NarrativePlan`: an ordered list of stable-id
:class:`NarrativeBeat` entries.

The plan describes WHAT information each beat releases and WHY — it
deliberately does NOT contain prose. Writing happens in a separate
stage (``studio.content.writing``) so a faulty plan can be re-rolled
without discarding drafted text, and a faulty draft can be rewritten
without re-rolling the investigation structure.

Anti-template rules pinned here:

* Beat IDs are service-assigned and stable (``b1``, ``b2``, …) so
  downstream revisions can anchor to a beat across plan rewrites.
* Beats with a non-``"question"`` purpose must cite at least one
  :class:`FactCard` — beats that invent facts the research stage never
  produced are rejected by ``finalize``.
* The system prompt forbids canned phrases, mandatory five-act
  structure, and mandatory reversal.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel

from studio.providers.base import ModelProvider
from studio.schemas import (
    DraftRevision,
    NarrativeBeat,
    NarrativePlan,
    ResearchPacket,
    StoryPitch,
)

MIN_BEATS = 3
CHARS_PER_SECOND = 3  # spoken-character baseline for Chinese narration.
DURATION_MIN_SEC = 60
DURATION_MAX_SEC = 360


class AntiTemplateViolation(Exception):
    """Raised when a plan violates an anti-template invariant.

    Distinct from generic :class:`ValueError` so the worker / API layer
    can map this to a precise failure mode without parsing the message.
    """


class _BeatDraft(BaseModel):
    """Model output for one beat. The service assigns the stable ``id``."""

    purpose: str
    fact_card_ids: list[str]
    new_information: str
    next_question: str
    withheld_information: str


NARRATIVE_SYSTEM = """你是科普短视频叙事路线编辑。你只设计信息释放顺序，不写稿。

严禁使用以下套路化表达（原文及其变体一律不许出现）：
- "你以为……其实……"
- "这就有意思了"
- "离谱的是"
- "说白了"
- "关键是"
- "没了"

不要套用固定结构：不存在必须的五段式（五段结构），也不需要强行安排反转。结构由这个故事本身的证据链决定。

每个 beat 必须给出：
- purpose: 这一段在叙事里的作用（建议：setup / evidence / question / payoff / bridge 等可读标签）。
- fact_card_ids: 这一段会用到的事实编号列表（对应研究材料里 fact_cards 的索引，从 0 开始的整数字符串）。question 类 beat 可以为空。
- new_information: 这一段释放出的新信息（一句话）。
- next_question: 这一段之后留给下一段的问题。
- withheld_information: 这一段刻意没有揭示、留给后面的内容。

至少输出 3 个 beat。响应必须严格符合 schema。"""


def _beat_id(index: int) -> str:
    """Stable, sequential beat ID (service-owned, not model-generated)."""

    return f"b{index + 1}"


def _user_prompt(pitch: StoryPitch, research: ResearchPacket) -> str:
    facts = "\n".join(
        f"[{i}] {card.claim}" for i, card in enumerate(research.fact_cards)
    ) or "[0] (无)"
    return (
        "请基于以下选题与研究材料设计本集叙事路线图。\n\n"
        f"调查问题：{pitch.investigation_question}\n"
        f"开场镜头：{pitch.opening_scene}\n"
        f"证据链：{pitch.evidence_path}\n"
        f"回报：{pitch.payoff}\n\n"
        "可用事实（索引即 fact_card_id）：\n"
        f"{facts}\n"
    )


class NarrativeService:
    """Generates and validates :class:`NarrativePlan` artifacts."""

    def __init__(self, provider: ModelProvider) -> None:
        self._provider = provider

    # ------------------------------------------------------------------
    # generation
    # ------------------------------------------------------------------
    def plan(
        self, pitch: StoryPitch, research: ResearchPacket
    ) -> NarrativePlan:
        """One model call: produce a :class:`NarrativePlan` for ``pitch``."""

        prompt = _user_prompt(pitch, research)
        draft = self._provider.generate(
            _BeatList, NARRATIVE_SYSTEM, prompt, operation="narrative"
        )

        beats = [
            NarrativeBeat(id=_beat_id(i), **beat.model_dump())
            for i, beat in enumerate(draft.beats)
        ]
        plan = NarrativePlan(
            id=str(uuid.uuid4()),
            pitch_id=pitch.id,
            beats=beats,
            created_at=datetime.now(UTC),
        )
        self.finalize(plan)
        return plan

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------
    def finalize(self, plan: NarrativePlan) -> None:
        """Raise :class:`AntiTemplateViolation` if ``plan`` is invalid.

        Checks (each raises with a precise message so callers can log
        *which* rule failed):

        * at least ``MIN_BEATS`` beats;
        * all beat IDs unique and non-empty;
        * every beat has non-empty ``purpose`` and ``new_information``;
        * every non-``"question"`` beat has at least one ``fact_card_id``.
        """

        if len(plan.beats) < MIN_BEATS:
            raise AntiTemplateViolation(
                f"narrative plan needs at least {MIN_BEATS} beats, "
                f"got {len(plan.beats)}"
            )

        ids = [beat.id for beat in plan.beats]
        if len(set(ids)) != len(ids) or any(not i for i in ids):
            raise AntiTemplateViolation(
                f"narrative plan beat ids must be unique and non-empty; got {ids!r}"
            )

        for index, beat in enumerate(plan.beats):
            if not beat.purpose or not beat.new_information:
                raise AntiTemplateViolation(
                    f"beat {beat.id!r} (index {index}) missing purpose or new_information"
                )
            if beat.purpose != "question" and not beat.fact_card_ids:
                raise AntiTemplateViolation(
                    f"beat {beat.id!r} (purpose={beat.purpose!r}) must cite "
                    "at least one fact_card_id"
                )

    # ------------------------------------------------------------------
    # duration estimate
    # ------------------------------------------------------------------
    def estimate_duration_sec(
        self, target: NarrativePlan | DraftRevision
    ) -> tuple[int, bool]:
        """Spoken-character duration estimate (Chinese ~3 chars/sec).

        Returns ``(seconds, in_range)`` where ``in_range`` is True iff
        the estimate is within ``[DURATION_MIN_SEC, DURATION_MAX_SEC]``,
        so callers can warn when the script is far outside the
        platform's expected video length.
        """

        if isinstance(target, DraftRevision):
            total_chars = len(target.editorial_text)
        else:
            total_chars = sum(len(beat.new_information) for beat in target.beats)

        seconds = total_chars // CHARS_PER_SECOND
        in_range = DURATION_MIN_SEC <= seconds <= DURATION_MAX_SEC
        return seconds, in_range


class _BeatList(BaseModel):
    """Wrapper around the model's beats list."""

    beats: list[_BeatDraft]


def plan_narrative(
    pitch: StoryPitch, research: ResearchPacket, provider: ModelProvider
) -> NarrativePlan:
    return NarrativeService(provider).plan(pitch, research)


__all__ = [
    "NARRATIVE_SYSTEM",
    "AntiTemplateViolation",
    "NarrativeService",
    "plan_narrative",
]