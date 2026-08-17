"""Story-pitch generation and single-pitch revision.

The pitch stage exists so the human editor picks the story *before* anyone
writes a sentence of it. That only works if the three options are genuinely
different: two pitches that ask the same investigation question along the same
evidence path are one option wearing two hats. So the service measures the
difference rate itself and regenerates only the colliding entry, nudged toward
another evidence path.

Regeneration is deliberately per-pitch. The editor rejecting option 2 has
already made up their mind about options 1 and 3; re-rolling those would
destroy information. A revision therefore returns a NEW set that carries the
untouched pitches (same IDs, same payloads), the feedback, and a pointer to
its parent set.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel

from studio.providers.base import ModelProvider
from studio.schemas import ResearchPacket, StoryPitch, StoryPitchSet, TopicDiagnosis

PITCH_COUNT = 3
MAX_COLLISION_RETRIES = 2


PITCH_SYSTEM = """你是科普短视频的选题策划。你的任务是提出一个可以拍成一集的调查式故事方案。

严禁使用以下套路化表达（原文及其变体一律不许出现）：
- "你以为……其实……"
- "这就有意思了"
- "离谱的是"
- "说白了"
- "关键是"
- "没了"

不要套用固定结构：不存在必须的五段式（五段结构），也不需要强行安排反转。结构由这个故事本身的证据链决定。

响应必须是严格符合 schema 的 JSON 对象，字段：
- investigation_question: 这一集要调查的具体问题（一句话，必须比诊断阶段的核心问题更聚焦）。
- opening_scene: 第一个镜头看到的具体场景（写画面，不写口播）。
- evidence_path: 从开场推进到结论所依赖的证据链（说明依次拿出哪些证据）。
- payoff: 观众看完后拿到的回报（新认知，不是情绪）。
- why_it_works: 为什么这条路线对这个题材成立（一到两句）。
- estimated_duration_sec: 预计成片时长（秒）。
- risks: 这条路线的风险列表（素材不足、容易滑向说教、证据不够硬等）。"""


class _PitchDraft(BaseModel):
    """Model output for one pitch. The service — not the model — owns the ID."""

    investigation_question: str
    opening_scene: str
    evidence_path: str
    payoff: str
    why_it_works: str
    estimated_duration_sec: int
    risks: list[str] = []


def _signature(pitch: StoryPitch | _PitchDraft) -> tuple[str, str]:
    """Normalised ``(question, evidence_path)`` pair used for collision checks."""

    return (
        "".join(pitch.investigation_question.split()).lower(),
        "".join(pitch.evidence_path.split()).lower(),
    )


class PitchService:
    """Generates and revises pitch sets against an injected model provider."""

    def __init__(self, provider: ModelProvider) -> None:
        self._provider = provider

    # ------------------------------------------------------------------
    # generation
    # ------------------------------------------------------------------
    def generate(
        self,
        diagnosis: TopicDiagnosis,
        research: ResearchPacket,
        provider: ModelProvider | None = None,
    ) -> StoryPitchSet:
        """Produce ``PITCH_COUNT`` pitches with distinct signatures."""

        model = provider or self._provider
        base_prompt = _user_prompt(diagnosis, research)

        pitches: list[StoryPitch] = []
        for index in range(PITCH_COUNT):
            taken = {_signature(p) for p in pitches}
            draft = model.generate(
                _PitchDraft, PITCH_SYSTEM, base_prompt, operation="pitches"
            )
            attempts = 0
            while _signature(draft) in taken and attempts < MAX_COLLISION_RETRIES:
                draft = model.generate(
                    _PitchDraft,
                    PITCH_SYSTEM,
                    f"{base_prompt}\n{_nudge(pitches)}",
                    operation="pitches",
                )
                attempts += 1
            if _signature(draft) in taken:
                # The model kept returning the same route. Disambiguate rather
                # than hand the editor two identical options.
                draft = draft.model_copy(
                    update={
                        "investigation_question": (
                            f"{draft.investigation_question}（备选视角 {index + 1}）"
                        )
                    }
                )
            pitches.append(StoryPitch(id=str(uuid.uuid4()), **draft.model_dump()))

        return StoryPitchSet(
            id=str(uuid.uuid4()),
            pitches=pitches,
            created_at=datetime.now(UTC),
        )

    def effective_difference_rate(self, pitches: list[StoryPitch]) -> float:
        """Fraction of distinct normalised signatures. 1.0 = every pitch differs."""

        if not pitches:
            return 0.0
        return len({_signature(p) for p in pitches}) / len(pitches)

    # ------------------------------------------------------------------
    # revision
    # ------------------------------------------------------------------
    def regenerate(
        self,
        pitch_set: StoryPitchSet,
        pitch_id: str,
        feedback: str,
        provider: ModelProvider | None = None,
    ) -> StoryPitchSet:
        """Replace exactly one pitch, keeping the others byte-for-byte."""

        model = provider or self._provider
        target = next((p for p in pitch_set.pitches if p.id == pitch_id), None)
        if target is None:
            raise KeyError(f"pitch {pitch_id!r} is not in set {pitch_set.id!r}")

        others = [p for p in pitch_set.pitches if p.id != pitch_id]
        draft = model.generate(
            _PitchDraft,
            PITCH_SYSTEM,
            _revision_prompt(target, feedback, others),
            operation="pitches",
        )
        replacement = StoryPitch(id=pitch_id, **draft.model_dump())

        return StoryPitchSet(
            id=str(uuid.uuid4()),
            pitches=[
                replacement if p.id == pitch_id else p for p in pitch_set.pitches
            ],
            parent_set_id=pitch_set.id,
            feedback=feedback,
            created_at=datetime.now(UTC),
        )


def generate_pitches(
    diagnosis: TopicDiagnosis, research: ResearchPacket, provider: ModelProvider
) -> StoryPitchSet:
    return PitchService(provider).generate(diagnosis, research)


def regenerate_pitch(
    pitch_set: StoryPitchSet, pitch_id: str, feedback: str, provider: ModelProvider
) -> StoryPitchSet:
    return PitchService(provider).regenerate(pitch_set, pitch_id, feedback)


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------


def _user_prompt(diagnosis: TopicDiagnosis, research: ResearchPacket) -> str:
    facts = "\n".join(f"- {card.claim}" for card in research.fact_cards) or "- (无)"
    return (
        "请基于以下诊断与研究素材提出一条故事路线。\n\n"
        f"核心问题：{diagnosis.core_question}\n"
        f"核心张力：{diagnosis.central_tension}\n"
        f"观众已知：{diagnosis.audience_prior_knowledge}\n"
        f"常见误解：{', '.join(diagnosis.misconceptions)}\n"
        f"本期覆盖：{', '.join(diagnosis.scope)}\n"
        f"本期不涉及：{', '.join(diagnosis.excluded_topics)}\n\n"
        f"可用机制：\n" + "\n".join(f"- {m}" for m in research.mechanisms) + "\n\n"
        f"可用事实：\n{facts}\n\n"
        f"可拍场景：\n" + "\n".join(f"- {s}" for s in research.concrete_scenes) + "\n\n"
        "不能下结论的开放问题：\n"
        + "\n".join(f"- {u}" for u in research.uncertainties)
        + "\n"
    )


def _nudge(existing: list[StoryPitch]) -> str:
    used = "\n".join(
        f"- 问题：{p.investigation_question} / 证据链：{p.evidence_path}"
        for p in existing
    )
    return (
        "以下路线已经被占用，必须换一条不同的证据链（evidence_path），"
        f"并且调查问题也要不同：\n{used}"
    )


def _revision_prompt(
    target: StoryPitch, feedback: str, others: list[StoryPitch]
) -> str:
    return (
        "请重做以下这一条故事路线。\n\n"
        f"原调查问题：{target.investigation_question}\n"
        f"原证据链：{target.evidence_path}\n\n"
        f"编辑反馈（必须满足）：{feedback}\n\n"
        + _nudge(others)
    )


__all__ = [
    "MAX_COLLISION_RETRIES",
    "PITCH_COUNT",
    "PITCH_SYSTEM",
    "PitchService",
    "generate_pitches",
    "regenerate_pitch",
]
