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


class StoryPitch(BaseModel):
    """One of three competing story options presented to a human editor.

    ``id`` is stable across regenerations: revising option 2 must not renumber
    options 1 and 3, otherwise "accept option 1" would be ambiguous.
    """

    id: str
    investigation_question: str
    opening_scene: str
    evidence_path: str
    payoff: str
    why_it_works: str
    estimated_duration_sec: int
    risks: list[str] = []


class StoryPitchSet(BaseModel):
    """A generation of pitches plus the revision lineage that produced it.

    ``payload_kind`` is the discriminator that tells :func:`validate_payload`
    apart from :class:`AcceptedPitch` — both share ``kind="pitches"`` so the
    head pointer tracks one stage, but downstream code must dispatch on a
    named tag instead of guessing by key presence.
    """

    payload_kind: Literal["pitch_set"] = "pitch_set"
    id: str
    pitches: list[StoryPitch]
    parent_set_id: str | None = None
    feedback: str | None = None
    created_at: datetime


PitchPayloadKind = Literal["pitch_set", "accepted_pitch"]


class AcceptedPitch(BaseModel):
    """The editor's choice: which pitch, plus any hand edits applied to it.

    ``payload_kind`` is the discriminator that tells :func:`validate_payload`
    apart from :class:`StoryPitchSet` — both share ``kind="pitches"`` so the
    head pointer tracks one stage, but downstream code must dispatch on a
    named tag instead of guessing by key presence.
    """

    payload_kind: Literal["accepted_pitch"] = "accepted_pitch"
    selected_pitch_id: str
    edited_pitch: StoryPitch | None = None


class NarrativeBeat(BaseModel):
    """One planned paragraph / beat of a script.

    ``id`` is a stable, service-assigned identifier so downstream revisions
    (drafts, comments, rewrites) can anchor to a beat without depending on
    array position. ``purpose`` distinguishes information beats from open
    questions (``"question"``); non-question beats must cite at least one
    :class:`FactCard` so the writer can never invent a fact the research
    stage never produced.
    """

    id: str
    purpose: str
    fact_card_ids: list[str]
    new_information: str
    next_question: str
    withheld_information: str


class NarrativePlan(BaseModel):
    """Ordered list of stable-id :class:`NarrativeBeat` for one script.

    ``pitch_id`` ties the plan back to the :class:`StoryPitch` the editor
    accepted, so the worker can refuse a plan whose pitch is no longer
    current.
    """

    payload_kind: Literal["narrative_plan"] = "narrative_plan"
    id: str
    pitch_id: str
    beats: list[NarrativeBeat]
    created_at: datetime


class DraftParagraph(BaseModel):
    """One written paragraph keyed to the beat it corresponds to.

    ``id`` MUST equal the id of the :class:`NarrativeBeat` it realises, so
    editorial comments can anchor to a beat across plan revisions and the
    speech stage can align subtitles to beats.
    """

    id: str
    text: str


class DraftRevision(BaseModel):
    """A revision of the script's draft text.

    ``narrative_plan_id`` ties the draft to the plan it was written from.
    ``parent_id`` and ``change_source`` are designed for Task 8's
    directive rewrite (``change_source="rewrite"``); an initial draft
    always has ``parent_id=None`` and ``change_source="initial"``.
    ``editorial_text`` is the joined paragraph text — it is the
    canonical string that downstream stages (speech, approval) consume.
    """

    payload_kind: Literal["draft"] = "draft"
    id: str
    narrative_plan_id: str
    paragraphs: list[DraftParagraph]
    editorial_text: str
    parent_id: str | None = None
    change_source: str
    author_note: str = ""
    created_at: datetime


class RepetitionReport(BaseModel):
    """Result of comparing a draft's fingerprints against recent drafts.

    ``must_replan`` is the only signal downstream stages act on — when
    True, the worker must re-run narrative planning rather than patch the
    existing draft (the brief forbids synonym-swap rewrites). The
    ``*_similarity`` fields expose WHICH fingerprint crossed the
    threshold so the worker / reviewer can see the shape that triggered
    the replan.
    """

    must_replan: bool
    rewrite_suggestions: list[str] = []
    opening_syntax_similarity: float = 0.0
    transition_distribution_similarity: float = 0.0
    reveal_position_similarity: float = 0.0
    ending_shape_similarity: float = 0.0
    comparison_pattern_similarity: float = 0.0
    misconception_correction_pattern_similarity: float = 0.0


@register("narrative")
def _validate_narrative(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate ``kind="narrative"`` payloads as :class:`NarrativePlan`."""

    return NarrativePlan.model_validate(payload).model_dump(mode="json")


@register("draft")
def _validate_draft(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate ``kind="draft"`` payloads as :class:`DraftRevision`.

    Strict validation is gated on the ``payload_kind="draft"``
    discriminator so that DraftRevision-shaped payloads are normalised
    through Pydantic (and any future schema fix lands on every read),
    while legacy / placeholder payloads (e.g. ``{"text": "..."}`` used
    by ``tests/test_jobs.py`` to exercise the artifact repository) pass
    through untouched. The discriminator is set by :class:`DraftRevision`
    itself, so any draft produced by :class:`~studio.content.writing.DraftService`
    is always validated.
    """

    if payload.get("payload_kind") == "draft":
        return DraftRevision.model_validate(payload).model_dump(mode="json")
    return dict(payload)


@register("pitches")
def _validate_pitches(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate both shapes stored under ``kind="pitches"``.

    A pitch artifact is either a generated set (``pitches`` present) or the
    acceptance record that closes the human gate (``payload_kind ==
    "accepted_pitch"``). They share a kind so the head pointer tracks a
    single stage. Discrimination is by the explicit ``payload_kind`` field;
    key-presence dispatch is unreliable because both shapes could in theory
    carry ``pitches`` keys under different schemas.
    """

    kind = payload.get("payload_kind")
    if kind == "accepted_pitch":
        return AcceptedPitch.model_validate(payload).model_dump(mode="json")
    return StoryPitchSet.model_validate(payload).model_dump(mode="json")


__all__ = [
    "AcceptedPitch",
    "DraftParagraph",
    "DraftRevision",
    "FactCard",
    "FactRisk",
    "NarrativeBeat",
    "NarrativePlan",
    "PitchPayloadKind",
    "RepetitionReport",
    "ResearchPacket",
    "SourceDocument",
    "StoryPitch",
    "StoryPitchSet",
    "TopicDiagnosis",
    "VerificationStatus",
    "register",
    "validate_payload",
]
