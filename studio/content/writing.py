"""Draft-writing service.

Turns a :class:`NarrativePlan` + :class:`ResearchPacket` into a
:class:`DraftRevision` whose ``paragraphs[*].id`` matches
``plan.beats[*].id`` so editorial comments can anchor to a beat across
revisions.

The service trusts the model to produce one paragraph per beat in
order; the service assigns paragraph IDs from the plan's beats in
sequence and rejects drafts whose editorial text contains banned
phrases or whose paragraphs are empty.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel

from studio.providers.base import ModelProvider
from studio.schemas import (
    DraftParagraph,
    DraftRevision,
    NarrativePlan,
    ResearchPacket,
)

DRAFT_SYSTEM = """你是科普短视频作者。你只依据批准的叙事路线图和已验证的研究材料写稿，不在路线图之外发明新事实。

严禁使用以下套路化表达（原文及其变体一律不许出现）：
- "你以为……其实……"
- "这就有意思了"
- "离谱的是"
- "说白了"
- "关键是"
- "没了"

不要套用固定结构：不存在必须的五段式（五段结构），也不需要强行安排反转。结构由叙事路线图决定，本阶段只负责把每段写得有画面感和具体细节。

响应必须是严格符合 schema 的 JSON 对象，字段：
- paragraphs: 字符串列表，长度等于路线图的 beats 数，每项对应一个 beat，按顺序一一对应。"""


class AntiTemplateViolation(Exception):
    """Raised when a draft violates an anti-template invariant."""


class _ParagraphDraft(BaseModel):
    """Model output for one paragraph (text only; service assigns the id)."""

    text: str


class _DraftDraft(BaseModel):
    """Model output for the full draft."""

    paragraphs: list[_ParagraphDraft]


def _user_prompt(plan: NarrativePlan, research: ResearchPacket) -> str:
    beat_lines = "\n".join(
        f"- {beat.id}（purpose={beat.purpose}, "
        f"new_information={beat.new_information}, "
        f"withheld={beat.withheld_information}）"
        for beat in plan.beats
    )
    facts = "\n".join(
        f"[{i}] {card.claim}" for i, card in enumerate(research.fact_cards)
    ) or "[0] (无)"
    return (
        "请按以下叙事路线图为每一段写一段旁白。\n\n"
        f"pitch_id：{plan.pitch_id}\n\n"
        "beats（顺序即段落顺序，paragraphs 列表长度必须相同）：\n"
        f"{beat_lines}\n\n"
        "可用事实（仅用于核对，不要逐条朗读）：\n"
        f"{facts}\n"
    )


class DraftService:
    """Generates and validates :class:`DraftRevision` artifacts."""

    def __init__(self, provider: ModelProvider) -> None:
        self._provider = provider

    # ------------------------------------------------------------------
    # generation
    # ------------------------------------------------------------------
    def draft(self, plan: NarrativePlan, research: ResearchPacket) -> DraftRevision:
        """One model call: write one paragraph per beat, in order."""

        prompt = _user_prompt(plan, research)
        draft = self._provider.generate(
            _DraftDraft, DRAFT_SYSTEM, prompt, operation="draft"
        )

        if len(draft.paragraphs) != len(plan.beats):
            raise AntiTemplateViolation(
                f"draft paragraphs ({len(draft.paragraphs)}) must match "
                f"plan beats ({len(plan.beats)})"
            )

        paragraphs = [
            DraftParagraph(id=beat.id, text=para.text)
            for beat, para in zip(plan.beats, draft.paragraphs)
        ]
        revision = DraftRevision(
            id=str(uuid.uuid4()),
            narrative_plan_id=plan.id,
            paragraphs=paragraphs,
            editorial_text="\n\n".join(p.text for p in paragraphs),
            parent_id=None,
            change_source="initial",
            created_at=datetime.now(UTC),
        )
        self.finalize(revision, plan)
        return revision

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------
    def finalize(self, revision: DraftRevision, plan: NarrativePlan) -> None:
        """Raise :class:`AntiTemplateViolation` on any rule violation."""

        beat_ids = {beat.id for beat in plan.beats}
        for paragraph in revision.paragraphs:
            if paragraph.id not in beat_ids:
                raise AntiTemplateViolation(
                    f"draft paragraph id {paragraph.id!r} is not in plan beat ids "
                    f"{sorted(beat_ids)!r}"
                )
            if not paragraph.text.strip():
                raise AntiTemplateViolation(
                    f"draft paragraph {paragraph.id!r} is empty"
                )

        lowered = revision.editorial_text
        for banned in _BANNED_PHRASES:
            if banned in lowered:
                raise AntiTemplateViolation(
                    f"draft editorial_text contains banned phrase {banned!r}"
                )


_BANNED_PHRASES = (
    "你以为",
    "这就有意思了",
    "离谱的是",
    "说白了",
    "关键是",
    "没了",
)


def write_draft(
    plan: NarrativePlan, research: ResearchPacket, provider: ModelProvider
) -> DraftRevision:
    return DraftService(provider).draft(plan, research)


__all__ = [
    "DRAFT_SYSTEM",
    "AntiTemplateViolation",
    "DraftService",
    "write_draft",
]