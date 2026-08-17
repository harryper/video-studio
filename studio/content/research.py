"""Research packet service.

The research stage turns a :class:`TopicDiagnosis` into a
:class:`ResearchPacket`. Two model interactions happen back-to-back:

1. **Expand** — ask the model for the candidate facts and the supporting
   raw material (mechanisms, scenes, visuals, uncertainties, …). The
   model also flags which candidate facts carry risky data
   (numbers / dates / superlatives / absolutes) so we know where to
   spend search budget.
2. **Classify** — ask the model for a per-fact risk profile (risk
   category + optional softened wording + confidence + narrative value
   + payoff flag). The classifier is the source of truth for
   ``verification_status``; the expansion's flag list only decides
   what to search.

Every high-risk claim is queried against the configured search provider
so at least one source backs it (or the model explicitly softens /
drops it). Ordinary background facts are accepted without search —
they're cheap, common knowledge the writer can state freely.

Verification hierarchy:

* ``ordinary`` risk  → ``verified`` (no source needed).
* high-risk + ≥1 source  → ``verified``.
* high-risk + softened wording → ``softened`` (claim field is the rewrite).
* high-risk + no source, no softening → ``dropped``.
* ``unverified`` is the safety-net for facts the classifier missed
  entirely; finalize() rejects ``payoff_critical`` ones via
  :class:`UnverifiedCentralClaim`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel

from studio.providers.base import ModelProvider, SearchProvider
from studio.schemas import FactCard, ResearchPacket, SourceDocument, TopicDiagnosis


class UnverifiedCentralClaim(Exception):
    """Raised when a ``payoff_critical`` fact has ``verification_status='unverified'``.

    The packet's central payoff — the tension the diagnosis promised —
    is unsupported. The writer must not be handed a packet shaped
    like that, because every later stage would have to make promises
    the script cannot keep.
    """


class _RiskClassification(BaseModel):
    """Per-fact risk profile returned by the classifier call."""

    claim: str
    risk: Literal["number", "date", "superlative", "absolute", "ordinary"]
    softened_claim: str | None = None
    confidence: float
    narrative_value: str
    payoff_critical: bool = False


class _ExpansionDraft(BaseModel):
    """Output of the expand call."""

    candidate_facts: list[str]
    high_risk_claims: list[str]
    mechanisms: list[str]
    people_events: list[str]
    concrete_scenes: list[str]
    visual_details: list[str]
    uncertainties: list[str]


class _ClassificationDraft(BaseModel):
    """Output of the classify call."""

    classifications: list[_RiskClassification]


def build_research_packet(
    diagnosis: TopicDiagnosis,
    model: ModelProvider,
    search: SearchProvider,
) -> ResearchPacket:
    """Build a :class:`ResearchPacket` from a :class:`TopicDiagnosis`.

    Two model calls in order, both keyed on ``operation="research"``.
    Searches run between the two calls so the per-fact risk profile is
    applied to claims that already have sources attached.
    """

    expansion = model.generate(
        _ExpansionDraft,
        _EXPANSION_SYSTEM,
        _expansion_user_prompt(diagnosis),
        operation="research",
    )

    # Search every high-risk claim exactly once. Capped at ``limit=3``
    # per the brief so a single rich claim can't drown the source list.
    sources_by_claim: dict[str, list[SourceDocument]] = {}
    for claim in expansion.high_risk_claims:
        sources_by_claim[claim] = list(search.search(claim, limit=3))

    classification = model.generate(
        _ClassificationDraft,
        _CLASSIFICATION_SYSTEM,
        _classification_user_prompt(
            diagnosis, expansion.candidate_facts, expansion.high_risk_claims
        ),
        operation="research",
    )

    classifications_by_claim = {c.claim: c for c in classification.classifications}

    fact_cards: list[FactCard] = []
    collected_sources: list[SourceDocument] = []
    for claim in expansion.candidate_facts:
        profile = classifications_by_claim.get(claim)
        card = _build_card(claim, profile, sources_by_claim.get(claim, []))
        fact_cards.append(card)
        collected_sources.extend(card.sources)

    # The packet's top-level ``sources`` is the flat, deduped roll-up.
    return ResearchPacket(
        mechanisms=list(expansion.mechanisms),
        fact_cards=fact_cards,
        people_events=list(expansion.people_events),
        concrete_scenes=list(expansion.concrete_scenes),
        visual_details=list(expansion.visual_details),
        uncertainties=list(expansion.uncertainties),
        sources=_dedupe_sources(collected_sources),
    )


def _build_card(
    claim: str,
    profile: _RiskClassification | None,
    sources: list[SourceDocument],
) -> FactCard:
    """Resolve a candidate claim into a :class:`FactCard` with status."""

    if profile is None:
        # Classifier missed this fact entirely. The expansion flagged it
        # as a candidate, but we have no risk profile, no confidence, and
        # no narrative value to attach. Two consequences:
        #
        # 1. ``verification_status="unverified"`` — finalize() will reject
        #    the packet if the fact is payoff-critical.
        # 2. ``payoff_critical=True`` — we cannot trust the classifier's
        #    silence to mean "non-central". The model that failed to
        #    classify this fact also failed to mark it non-central, so
        #    downstream stages must assume the worst: this claim might
        #    be the one the script's tension depends on. Forcing the
        #    flag trips UnverifiedCentralClaim at finalize() rather than
        #    silently shipping an unsupported central claim to the writer.
        return FactCard(
            claim=claim,
            narrative_value="",
            confidence=0.0,
            risk="ordinary",
            sources=[],
            verification_status="unverified",
            payoff_critical=True,
        )

    if profile.risk == "ordinary":
        return FactCard(
            claim=claim,
            narrative_value=profile.narrative_value,
            confidence=profile.confidence,
            risk="ordinary",
            sources=[],
            verification_status="verified",
            payoff_critical=profile.payoff_critical,
        )

    rewritten = profile.softened_claim
    has_source = bool(sources)
    if has_source:
        status: Literal["verified", "softened", "dropped", "unverified"] = "verified"
        final_claim = claim
    elif rewritten:
        status = "softened"
        final_claim = rewritten
    else:
        status = "dropped"
        final_claim = claim

    return FactCard(
        claim=final_claim,
        narrative_value=profile.narrative_value,
        confidence=profile.confidence,
        risk=profile.risk,
        sources=sources,
        verification_status=status,
        payoff_critical=profile.payoff_critical,
    )


def _dedupe_sources(sources: Iterable[SourceDocument]) -> list[SourceDocument]:
    seen: dict[str, SourceDocument] = {}
    for doc in sources:
        if doc.url not in seen:
            seen[doc.url] = doc
    return list(seen.values())


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------


_EXPANSION_SYSTEM = """你是科普短视频研究员。你的任务是扩展候选素材，给后续写稿提供原料。

严禁返回钩子、开场、结论、叙事结构或脚本片段。诊断阶段已经界定题材，本阶段只产出"事实 + 视觉 + 人物/事件 + 不确定性"。

响应必须严格符合 schema：

- candidate_facts: 候选事实字符串列表（纯事实陈述，不要带句末立论）。
- high_risk_claims: candidate_facts 中包含数字、日期、最高级或绝对表述的子集（仅字符串列表，逐字与 candidate_facts 一致）。
- mechanisms: 本期可解释的因果机制列表（字符串列表）。
- people_events: 可在脚本中出现的人物或事件名 / 线索列表。
- concrete_scenes: 可拍摄的具体场景列表（不抽象）。
- visual_details: 屏幕可呈现的视觉细节列表（材质、颜色、镜头角度）。
- uncertainties: 写作时不能下结论的开放问题列表。"""


_CLASSIFICATION_SYSTEM = """你是科普短视频事实审核员。对每一项候选事实，给出风险与处理建议。

风险类别：
- number: 包含具体数字或单位。
- date: 包含具体日期或年代。
- superlative: 包含"最/首次/唯一"等最高级表述。
- absolute: 包含"从不/永远/全部/没有"等绝对表述。
- ordinary: 背景常识，不需要溯源。

对每一个 claim 给出：
- risk: 上述 5 类之一。
- softened_claim: 若存在绝对表述且无法被来源支持，给出去绝对化的重写（如 "海洋盐度从不改变" → "海洋盐度在长时间尺度上保持大致稳定"）。否则为 null。
- confidence: 0-1 的可信度。
- narrative_value: 这条事实对观众有什么用（一句话）。
- payoff_critical: 这条事实是否直接支撑诊断阶段给出的 central_tension。

响应必须严格符合 schema（classifications 列表）。"""


def _expansion_user_prompt(diagnosis: object) -> str:
    """Render the expand call's user prompt from a ``TopicDiagnosis``."""

    return (
        "请基于以下诊断展开候选素材：\n"
        f"核心问题：{getattr(diagnosis, 'core_question', '')}\n"
        f"核心张力：{getattr(diagnosis, 'central_tension', '')}\n"
        f"目标观众已知道的：{getattr(diagnosis, 'audience_prior_knowledge', '')}\n"
        f"常见误解：{', '.join(getattr(diagnosis, 'misconceptions', []))}\n"
        f"本期覆盖：{', '.join(getattr(diagnosis, 'scope', []))}\n"
        f"本期不涉及：{', '.join(getattr(diagnosis, 'excluded_topics', []))}\n"
    )


def _classification_user_prompt(
    diagnosis: object,
    candidate_facts: list[str],
    high_risk_claims: list[str],
) -> str:
    """Render the classify call's user prompt."""

    bullets = "\n".join(f"- {fact}" for fact in candidate_facts)
    high_risk = "\n".join(f"- {claim}" for claim in high_risk_claims) or "- (无)"
    return (
        "请审核以下候选事实。central_tension 是脚本必须兑现的张力。\n\n"
        f"central_tension：{getattr(diagnosis, 'central_tension', '')}\n\n"
        f"候选事实：\n{bullets}\n\n"
        f"其中需要在搜索引擎中追溯的高风险事实：\n{high_risk}\n"
    )


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


class ResearchService:
    """Validator that downstream stages call before consuming a packet.

    Today the only check is ``payoff_critical`` × ``unverified``. Future
    stages (review, speech) may add checks here without touching the
    packet builder.
    """

    def finalize(self, packet: ResearchPacket) -> None:
        """Raise :class:`UnverifiedCentralClaim` if the payoff is unverified.

        The check is intentionally narrow: it walks ``fact_cards`` once
        and raises as soon as it finds an unsupported card whose
        absence would break the script's promised tension.
        """

        for card in packet.fact_cards:
            if card.payoff_critical and card.verification_status == "unverified":
                raise UnverifiedCentralClaim(
                    f"payoff-critical fact is unverified: {card.claim!r}"
                )


__all__ = [
    "ResearchService",
    "UnverifiedCentralClaim",
    "build_research_packet",
]
