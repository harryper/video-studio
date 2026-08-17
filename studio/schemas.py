"""Payload validation dispatch plus shared Pydantic schemas.

The ``register`` / ``validate_payload`` helpers underpin the artifact
repository (Task 2). Pydantic models for Content Studio's structured content
live here too so Tasks 5+ can import them from a single place without
inventing new modules for each task.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

_VALIDATORS: dict[str, Any] = {}


def register(kind: str) -> Any:
    """Decorator: register ``validator`` for ``kind`` payloads."""

    def decorator(validator: Any) -> Any:
        _VALIDATORS[kind] = validator
        return validator

    return decorator


def validate_payload(kind: str, payload: Any) -> dict[str, Any]:
    """Validate ``payload`` for the given ``kind``.

    Returns a normalised dict. Unknown kinds pass through untouched (Task 2
    rule: do NOT block unknown kinds).
    """

    if not isinstance(payload, dict):
        raise ValueError(
            f"artifact payload for kind {kind!r} must be a dict, got {type(payload).__name__}"
        )
    validator = _VALIDATORS.get(kind)
    if validator is None:
        return dict(payload)
    result = validator(payload)
    if isinstance(result, dict):
        return result
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return dict(result)


class TopicDiagnosis(BaseModel):
    """Result of the diagnosis stage.

    The diagnosis answers six questions about a topic before the script
    knows how it opens or what it concludes:

    * ``core_question`` — the single investigative question this episode
      answers.
    * ``audience_prior_knowledge`` — what the viewer already knows, so
      later stages can skip the exposition.
    * ``central_tension`` — the contradiction or unresolved surprise the
      script must pay off.
    * ``misconceptions`` — beliefs the script should address or correct.
    * ``scope`` — subtopics this episode covers.
    * ``excluded_topics`` — adjacent topics this episode explicitly
      leaves out.
    """

    core_question: str
    audience_prior_knowledge: str
    central_tension: str
    misconceptions: list[str]
    scope: list[str]
    excluded_topics: list[str]


class SourceDocument(BaseModel):
    """A search result returned by :class:`SearchProvider`."""

    title: str
    url: str
    snippet: str
    publisher: str
    published_at: datetime | None = None


FactRisk = Literal["number", "date", "superlative", "absolute", "ordinary"]
VerificationStatus = Literal["verified", "softened", "dropped", "unverified"]


class FactCard(BaseModel):
    """A single claim the script intends to make, with provenance.

    ``risk`` flags claims that need a source (``number`` / ``date`` /
    ``superlative`` / ``absolute``) versus background facts the writer
    can state freely (``ordinary``).

    ``payoff_critical=True`` flags the card whose status drives the
    central tension the script promises. If
    ``ResearchService.finalize`` finds any such card still
    ``unverified``, it raises :class:`UnverifiedCentralClaim` so the
    writer never sees a packet whose payoff is unsupported.
    """

    claim: str
    narrative_value: str
    confidence: float
    risk: FactRisk
    sources: list[SourceDocument] = []
    verification_status: VerificationStatus = "unverified"
    payoff_critical: bool = False


class ResearchPacket(BaseModel):
    """The structured research output every later stage builds on.

    * ``mechanisms`` — causal chains the script will explain.
    * ``fact_cards`` — claims with risk + sources + verification status.
    * ``people_events`` — names and dates to weave in.
    * ``concrete_scenes`` — physical situations to film.
    * ``visual_details`` — textures, angles, colors.
    * ``uncertainties`` — open questions the script must not claim.
    * ``sources`` — flat list of every cited :class:`SourceDocument`.
    """

    mechanisms: list[str]
    fact_cards: list[FactCard]
    people_events: list[str]
    concrete_scenes: list[str]
    visual_details: list[str]
    uncertainties: list[str]
    sources: list[SourceDocument]


__all__ = [
    "FactCard",
    "FactRisk",
    "ResearchPacket",
    "SourceDocument",
    "TopicDiagnosis",
    "VerificationStatus",
    "register",
    "validate_payload",
]
