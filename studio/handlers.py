"""Stage handlers that wire every Content Studio stage to a service.

The handlers are deliberately thin: each one is a ``StageHandler`` that
loads the inputs it needs from the worker's :class:`WorkerContext` (artifact
ids → payloads), calls the matching service, and writes the resulting
artifact as the job's head revision. The set of handlers is registered with
:class:`~studio.worker.StageDispatcher` by
:func:`build_default_handlers` so the worker / CLI / e2e test can drive
the full pipeline through the same code path.

The handlers take a :class:`HandlerContext` (provider, search, redis-fact
records) built once for the test/process. This collapses the worker
boundary into a single function-with-context so the e2e test can run the
whole pipeline in-process without standing up a real worker process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from studio.artifacts import ArtifactRepository
from studio.content.diagnosis import diagnose_topic
from studio.content.narratives import NarrativeService
from studio.content.pitches import PitchService
from studio.content.research import ResearchService, build_research_packet
from studio.content.review import ReviewService, approve_draft
from studio.content.speech import SpeechService
from studio.content.writing import DraftService
from studio.jobs import LeaseQueue, Stage
from studio.providers.base import ModelProvider, SearchProvider
from studio.schemas import (
    AcceptedPitch,
    DraftRevision,
    FactCard,
    NarrativePlan,
    ResearchPacket,
    StoryPitch,
    StoryPitchSet,
    TopicDiagnosis,
)
from studio.worker import StageDispatcher, StageHandler, WorkerContext


@dataclass
class HandlerContext:
    """Dependencies shared by every handler in a single worker process."""

    provider: ModelProvider
    search: SearchProvider
    session_factory: Any  # sessionmaker[Session] — typed loosely to avoid circular import


def _load_payloads(
    session: Session, artifact_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Resolve artifact ids into their payload dicts."""

    repo = ArtifactRepository(session)
    return {aid: repo.get(aid).payload for aid in artifact_ids if repo.get(aid)}


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


def _handle_diagnosis(ctx: WorkerContext, hctx: HandlerContext) -> list[str]:
    session = hctx.session_factory()
    try:
        topic = _topic_for_project(session, ctx.project_id)
    finally:
        session.close()
    raw = diagnose_topic(topic=topic, provider=hctx.provider)
    diagnosis = raw if isinstance(raw, TopicDiagnosis) else TopicDiagnosis.model_validate(raw)
    repo = ArtifactRepository(ctx.db_session)
    artifact = repo.create(
        ctx.project_id, "diagnosis", diagnosis.model_dump(mode="json")
    )
    repo.accept(ctx.project_id, artifact.id)
    ctx.db_session.commit()
    enqueue_next(hctx, ctx.project_id, Stage.RESEARCH, [artifact.id])
    return [artifact.id]


def _topic_for_project(session: Session, project_id: str) -> str:
    """Return the project's topic — the prompt for the diagnosis stage."""

    from studio.models import Project

    project = session.get(Project, project_id)
    return (project.title if project else "") or ""


def _handle_research(ctx: WorkerContext, hctx: HandlerContext) -> list[str]:
    repo = ArtifactRepository(ctx.db_session)
    payloads = _load_payloads(ctx.db_session, ctx.input_artifact_ids)
    diagnosis_payload = next(iter(payloads.values()), None)
    if diagnosis_payload is None:
        raise ValueError("research stage requires a diagnosis input")
    diagnosis = TopicDiagnosis.model_validate(diagnosis_payload)
    packet = build_research_packet(diagnosis, hctx.provider, hctx.search)
    ResearchService().finalize(packet)
    artifact = repo.create(
        ctx.project_id, "research", packet.model_dump(mode="json")
    )
    repo.accept(ctx.project_id, artifact.id)
    ctx.db_session.commit()
    enqueue_next(hctx, ctx.project_id, Stage.PITCHES, [artifact.id])
    return [artifact.id]


def _handle_pitches(ctx: WorkerContext, hctx: HandlerContext) -> list[str]:
    repo = ArtifactRepository(ctx.db_session)
    diagnosis = TopicDiagnosis.model_validate(
        repo.current(ctx.project_id, "diagnosis").payload
    )
    research = ResearchPacket.model_validate(
        repo.current(ctx.project_id, "research").payload
    )
    pitch_set = PitchService(hctx.provider).generate(diagnosis, research)
    payload = pitch_set.model_dump(mode="json")
    payload["payload_kind"] = "pitch_set"
    artifact = repo.create(ctx.project_id, "pitches", payload)
    repo.accept(ctx.project_id, artifact.id)
    ctx.db_session.commit()
    return [artifact.id]


def _handle_narrative(ctx: WorkerContext, hctx: HandlerContext) -> list[str]:
    repo = ArtifactRepository(ctx.db_session)
    accepted_pitch = None
    for aid in ctx.input_artifact_ids:
        artifact = repo.get(aid)
        if artifact is None:
            continue
        payload = artifact.payload or {}
        if payload.get("payload_kind") == "accepted_pitch":
            accepted_pitch = AcceptedPitch.model_validate(payload)
            break
    if accepted_pitch is None:
        raise ValueError("narrative stage requires an accepted_pitch input")
    # The pitch set the editor chose from is the parent of the accepted
    # record, but the handler rebuilds the link by walking revisions of
    # kind="pitches" newest-first and picking the first accepted set
    # whose payload is ``payload_kind == "pitch_set"``.
    pitch_set_payload = None
    for candidate in repo.list_revisions(ctx.project_id, "pitches"):
        if not candidate.payload:
            continue
        if candidate.payload.get("payload_kind") == "pitch_set":
            pitch_set_payload = candidate.payload
            break
    if pitch_set_payload is None:
        raise ValueError("narrative stage requires a pitch_set")
    pitch_set = StoryPitchSet.model_validate(pitch_set_payload)
    chosen = next(
        (p for p in pitch_set.pitches if p.id == accepted_pitch.selected_pitch_id),
        None,
    )
    if chosen is None:
        raise ValueError(
            f"selected pitch {accepted_pitch.selected_pitch_id!r} not in set "
            f"{pitch_set.id!r}"
        )
    pitch = accepted_pitch.edited_pitch or chosen
    research = ResearchPacket.model_validate(repo.current(ctx.project_id, "research").payload)
    plan = NarrativeService(hctx.provider).plan(pitch, research)
    payload = plan.model_dump(mode="json")
    payload["payload_kind"] = "narrative_plan"
    artifact = repo.create(ctx.project_id, "narrative", payload)
    repo.accept(ctx.project_id, artifact.id)
    ctx.db_session.commit()
    enqueue_next(hctx, ctx.project_id, Stage.DRAFT, [artifact.id])
    return [artifact.id]


def _handle_draft(ctx: WorkerContext, hctx: HandlerContext) -> list[str]:
    repo = ArtifactRepository(ctx.db_session)
    payloads = _load_payloads(ctx.db_session, ctx.input_artifact_ids)
    plan = NarrativePlan.model_validate(next(iter(payloads.values())))
    research = ResearchPacket.model_validate(
        repo.current(ctx.project_id, "research").payload
    )
    revision = DraftService(hctx.provider).draft(plan, research)
    payload = revision.model_dump(mode="json")
    payload["payload_kind"] = "draft"
    artifact = repo.create(ctx.project_id, "draft", payload)
    repo.accept(ctx.project_id, artifact.id)
    return [artifact.id]


def _handle_rewrite(ctx: WorkerContext, hctx: HandlerContext) -> list[str]:
    """No-op: the HTTP route handles rewrite directly. The handler still
    exists so the dispatch loop never fails with ``handler_missing``."""
    repo = ArtifactRepository(ctx.db_session)
    if ctx.input_artifact_ids:
        return ctx.input_artifact_ids
    return [repo.current(ctx.project_id, "draft").id]


def _handle_speech(ctx: WorkerContext, hctx: HandlerContext) -> list[str]:
    """Derive a SpeechPlan from the approved_script. The job is only
    enqueued by the e2e harness; production wires a separate flow."""
    repo = ArtifactRepository(ctx.db_session)
    from studio.schemas import SpeechPlan, ApprovedScript

    approved = ApprovedScript.model_validate(
        repo.current(ctx.project_id, "approved_script").payload
    )
    plan = SpeechService(hctx.provider).build(approved)
    artifact = repo.create(
        ctx.project_id, "speech_plan", plan.model_dump(mode="json")
    )
    repo.accept(ctx.project_id, artifact.id)
    return [artifact.id]


def _handle_approval(ctx: WorkerContext, hctx: HandlerContext) -> list[str]:
    """Approval is triggered by the HTTP route handler; the worker handler
    is a no-op pass-through so the queue can carry the job without
    crashing."""
    repo = ArtifactRepository(ctx.db_session)
    head = repo.current(ctx.project_id, "approved_script")
    if head is not None:
        return [head.id]
    draft = repo.current(ctx.project_id, "draft")
    return [draft.id]


def enqueue_next(
    hctx: HandlerContext, project_id: str, stage: Stage, input_ids: list[str]
) -> None:
    """Enqueue a downstream stage job from a handler.

    Uses a fresh session because the handler's own session may already
    have pending writes that shouldn't be flushed prematurely.
    """

    session = hctx.session_factory()
    try:
        LeaseQueue(session).enqueue(project_id, stage, input_ids)
        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def build_default_handlers(hctx: HandlerContext) -> dict[Stage, StageHandler]:
    """Return the canonical handler map for the e2e / CLI harness."""

    def _bind(handler):
        def _wrapped(ctx: WorkerContext) -> list[str]:
            return handler(ctx, hctx)

        return _wrapped

    return {
        Stage.DIAGNOSIS: _bind(_handle_diagnosis),
        Stage.RESEARCH: _bind(_handle_research),
        Stage.PITCHES: _bind(_handle_pitches),
        Stage.NARRATIVE: _bind(_handle_narrative),
        Stage.DRAFT: _bind(_handle_draft),
        Stage.REWRITE: _bind(_handle_rewrite),
        Stage.SPEECH: _bind(_handle_speech),
        Stage.APPROVAL: _bind(_handle_approval),
    }


def build_dispatcher(
    hctx: HandlerContext,
    session: Session,
) -> StageDispatcher:
    """Build a :class:`StageDispatcher` for the supplied session."""

    return StageDispatcher(build_default_handlers(hctx), LeaseQueue(session))


__all__ = [
    "HandlerContext",
    "build_default_handlers",
    "build_dispatcher",
]
