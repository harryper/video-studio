"""Research packet service tests.

The research stage turns a ``TopicDiagnosis`` into a ``ResearchPacket``
that downstream stages (narrative, draft, review) build on:

* ``mechanisms`` — causal chains the script will explain
* ``fact_cards`` — claim + risk + source + verification status
* ``people_events`` — names and dates to weave in
* ``concrete_scenes`` — physical situations to film
* ``visual_details`` — textures, angles, colors that earn on screen
* ``uncertainties`` — open questions the script must not claim
* ``sources`` — flat list of every cited ``SourceDocument``

The verification contract:

* ``ordinary`` risk → ``verified`` without sources.
* ``number`` / ``date`` / ``superlative`` / ``absolute`` → searched.
  * At least one source → ``verified``.
  * Model softened wording → ``softened``.
  * No source, not softened → ``dropped``.
* Any ``payoff_critical`` claim with ``verification_status="unverified"``
  fails ``ResearchService.finalize`` with :class:`UnverifiedCentralClaim`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from studio.content.research import (
    ResearchService,
    UnverifiedCentralClaim,
    _ClassificationDraft,
    _ExpansionDraft,
    build_research_packet,
)
from studio.providers.fake import FakeModelProvider
from studio.schemas import FactCard, ResearchPacket, SourceDocument, TopicDiagnosis

# FakeSearchProvider lives in tests/conftest.py per the brief.
from tests.conftest import FakeSearchProvider

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _diagnosis() -> TopicDiagnosis:
    return TopicDiagnosis(
        core_question="为什么海水是咸的？",
        audience_prior_knowledge="普通观众，知道有海洋但不熟悉化学",
        central_tension="为什么河流入海却不把盐冲淡",
        misconceptions=["海水=溶解的食用盐", "海洋一直这么咸"],
        scope=["盐的来源", "盐度平衡"],
        excluded_topics=["海洋化学实验教学"],
    )


def _doc(url: str) -> SourceDocument:
    return SourceDocument(
        title=f"Title for {url}",
        url=url,
        snippet=f"Snippet for {url}",
        publisher="Example",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def service() -> ResearchService:
    return ResearchService()


def _make_classification_draft(items: list[dict[str, object]]) -> _ClassificationDraft:
    from studio.content.research import _RiskClassification

    return _ClassificationDraft(
        classifications=[
            _RiskClassification.model_validate(item) for item in items
        ]
    )


def _build_packet(
    *,
    candidate_facts: list[str],
    high_risk_claims: list[str],
    classifications_payload: list[dict[str, object]],
    search_canned: dict[str, list[SourceDocument]] | None = None,
    mechanisms: list[str] | None = None,
    people_events: list[str] | None = None,
    concrete_scenes: list[str] | None = None,
    visual_details: list[str] | None = None,
    uncertainties: list[str] | None = None,
) -> ResearchPacket:
    expansion = _ExpansionDraft(
        candidate_facts=list(candidate_facts),
        high_risk_claims=list(high_risk_claims),
        mechanisms=list(mechanisms or ["m"]),
        people_events=list(people_events or []),
        concrete_scenes=list(concrete_scenes or []),
        visual_details=list(visual_details or []),
        uncertainties=list(uncertainties or []),
    )
    classification = _make_classification_draft(classifications_payload)
    model = FakeModelProvider({"research": [expansion, classification]})
    search = FakeSearchProvider(search_canned or {})
    return build_research_packet(_diagnosis(), model, search)


@pytest.fixture
def packet() -> ResearchPacket:
    """Default packet: one numeric high-risk (verified) + one ordinary."""

    numeric_claim = "海洋的平均盐度约为 35‰"
    ordinary_claim = "海水喝起来是咸的"
    return _build_packet(
        candidate_facts=[numeric_claim, ordinary_claim],
        high_risk_claims=[numeric_claim],
        classifications_payload=[
            {
                "claim": numeric_claim,
                "risk": "number",
                "confidence": 0.9,
                "narrative_value": "关键数字冲击",
            },
            {
                "claim": ordinary_claim,
                "risk": "ordinary",
                "confidence": 0.95,
                "narrative_value": "日常感知",
            },
        ],
        search_canned={numeric_claim: [_doc("https://example.com/a")]},
    )


# ---------------------------------------------------------------------------
# verbatim-from-brief tests
# ---------------------------------------------------------------------------


def test_research_flags_high_risk_claims(packet: ResearchPacket) -> None:
    """Brief body — verbatim.

    A high-risk claim (number / date / superlative / absolute) must be
    flagged as such and end up ``verified`` after search returns at
    least one source.
    """

    assert packet.fact_cards[0].risk in {"number", "date", "superlative", "absolute"}
    assert packet.fact_cards[0].verification_status == "verified"


def test_unverified_central_claim_is_rejected(
    service: ResearchService,
) -> None:
    """Brief body — verbatim.

    A ``payoff_critical`` fact card that the research stage left with
    ``verification_status="unverified"`` must trip
    :class:`UnverifiedCentralClaim` during finalize — the writer must
    not be handed a packet whose central payoff is unsupported.
    """

    payload_packet = ResearchPacket(
        mechanisms=["盐随河流入海"],
        fact_cards=[
            FactCard(
                claim="海洋盐度恒定不变",
                narrative_value="反常识钩子",
                confidence=0.4,
                risk="absolute",
                verification_status="unverified",
                sources=[],
                payoff_critical=True,
            )
        ],
        people_events=[],
        concrete_scenes=[],
        visual_details=[],
        uncertainties=["盐度历史变化"],
        sources=[],
    )
    with pytest.raises(UnverifiedCentralClaim):
        service.finalize(payload_packet)


# ---------------------------------------------------------------------------
# additional contract tests
# ---------------------------------------------------------------------------


def test_ordinary_facts_may_omit_sources() -> None:
    """An ``ordinary`` risk claim needs no source to be ``verified``."""

    packet = _build_packet(
        candidate_facts=["海水喝起来是咸的"],
        high_risk_claims=[],
        classifications_payload=[
            {
                "claim": "海水喝起来是咸的",
                "risk": "ordinary",
                "confidence": 0.95,
                "narrative_value": "日常感知",
            }
        ],
    )

    assert packet.fact_cards[0].risk == "ordinary"
    assert packet.fact_cards[0].verification_status == "verified"
    assert packet.fact_cards[0].sources == []


def test_search_provider_called_only_for_high_risk_claims() -> None:
    """Search runs once per high-risk claim and never for ``ordinary``."""

    numeric_claim = "海洋平均盐度 35‰"
    ordinary_claim = "海水喝起来是咸的"
    search = FakeSearchProvider({numeric_claim: [_doc("https://example.com/x")]})
    expansion = _ExpansionDraft(
        candidate_facts=[numeric_claim, ordinary_claim],
        high_risk_claims=[numeric_claim],
        mechanisms=["m"],
        people_events=[],
        concrete_scenes=[],
        visual_details=[],
        uncertainties=[],
    )
    classification = _make_classification_draft(
        [
            {
                "claim": numeric_claim,
                "risk": "number",
                "confidence": 0.9,
                "narrative_value": "关键数字",
            },
            {
                "claim": ordinary_claim,
                "risk": "ordinary",
                "confidence": 0.95,
                "narrative_value": "日常感知",
            },
        ]
    )
    model = FakeModelProvider({"research": [expansion, classification]})
    build_research_packet(_diagnosis(), model, search)

    assert search.calls == [numeric_claim]


def test_packet_includes_sources_for_high_risk_claims() -> None:
    """Every ``verified`` high-risk card carries the search results."""

    numeric_claim = "海洋平均盐度 35‰"
    src = _doc("https://example.com/ocean")
    packet = _build_packet(
        candidate_facts=[numeric_claim],
        high_risk_claims=[numeric_claim],
        classifications_payload=[
            {
                "claim": numeric_claim,
                "risk": "number",
                "confidence": 0.9,
                "narrative_value": "数字冲击",
            }
        ],
        search_canned={numeric_claim: [src]},
    )

    assert len(packet.fact_cards) == 1
    card = packet.fact_cards[0]
    assert card.risk == "number"
    assert card.verification_status == "verified"
    assert card.sources == [src]
    assert src in packet.sources


def test_softening_rewrites_absolute_to_qualified() -> None:
    """Model softens an ``absolute`` claim → ``softened`` with qualified text."""

    original = "海洋的盐度从不改变"
    softened = "海洋的盐度在数百万年的时间尺度上保持大致稳定"
    packet = _build_packet(
        candidate_facts=[original],
        high_risk_claims=[original],
        classifications_payload=[
            {
                "claim": original,
                "risk": "absolute",
                "softened_claim": softened,
                "confidence": 0.6,
                "narrative_value": "纠正误解",
            }
        ],
        search_canned={},  # no sources for this claim
    )

    assert len(packet.fact_cards) == 1
    card = packet.fact_cards[0]
    assert card.verification_status == "softened"
    assert card.claim == softened
    assert "从不改变" not in card.claim
