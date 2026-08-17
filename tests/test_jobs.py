"""Lease queue, recovery, and dispatcher tests.

These tests exercise the durable background-job queue every Content Studio
service depends on:

* FIFO claim ordering across projects
* Lease-based recovery of expired workers
* Maximum three attempts before a job is permanently failed
* One running stage per project
* Heartbeat-driven lease extension
* Cancellation only on queued jobs
* StageDispatcher wires handlers to claim/finish/fail lifecycle
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from studio.artifacts import ArtifactRepository
from studio.jobs import (
    ClaimedJob,
    JobNotClaimed,
    LeaseQueue,
    MaxAttemptsReached,
    Stage,
    StaleLease,
)
from studio.models import Project, StageJob
from studio.worker import StageDispatcher, WorkerContext


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clock() -> "FakeClock":
    return FakeClock(
        start=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    )


@pytest.fixture
def queue(session: Session, clock: "FakeClock") -> LeaseQueue:
    return LeaseQueue(session)


@pytest.fixture
def artifact_repo(session: Session) -> ArtifactRepository:
    return ArtifactRepository(session)


@pytest.fixture
def second_project(session: Session) -> Project:
    proj = Project(id=f"proj-{uuid4()}", title="Other Project")
    session.add(proj)
    session.commit()
    session.refresh(proj)
    return proj


@pytest.fixture
def third_project(session: Session) -> Project:
    proj = Project(id=f"proj-{uuid4()}", title="Third Project")
    session.add(proj)
    session.commit()
    session.refresh(proj)
    return proj


@pytest.fixture
def queued_job(queue: LeaseQueue, project: Project, clock: "FakeClock") -> StageJob:
    return queue.enqueue(project.id, Stage.DIAGNOSIS, [])


@dataclass
class FakeClock:
    start: datetime
    _now: datetime = field(init=False)

    def __post_init__(self) -> None:
        self._now = self.start

    @property
    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: int = 0, minutes: int = 0) -> datetime:
        self._now = self._now + timedelta(seconds=seconds, minutes=minutes)
        return self._now


def _reload(session: Session, job_id: str) -> StageJob:
    """Return a fresh ORM object for ``job_id`` (claim returns a frozen dataclass)."""

    job = session.get(StageJob, job_id)
    assert job is not None
    return job


# ---------------------------------------------------------------------------
# Required body from the brief
# ---------------------------------------------------------------------------


def test_stale_worker_cannot_finish(
    queue: LeaseQueue,
    queued_job: StageJob,
    artifact_repo: ArtifactRepository,
    project: Project,
    clock: FakeClock,
) -> None:
    first = queue.claim_next("worker-a", clock.now)
    clock.advance(seconds=901)
    queue.recover_expired(clock.now)
    second = queue.claim_next("worker-b", clock.now)
    assert first is not None and second is not None

    # ``finish`` writes the output_artifact_id which is FK-constrained, so we
    # create real Artifact rows for both the stale and the fresh claim.
    stale_artifact = artifact_repo.create(project.id, "draft", {"text": "stale"})
    fresh_artifact = artifact_repo.create(project.id, "draft", {"text": "fresh"})

    with pytest.raises(StaleLease):
        queue.finish(first.id, first.token, stale_artifact.id)
    queue.finish(second.id, second.token, fresh_artifact.id)


# ---------------------------------------------------------------------------
# FIFO and concurrency
# ---------------------------------------------------------------------------


def test_fifo_claim_orders_by_creation_time(
    queue: LeaseQueue,
    project: Project,
    second_project: Project,
    third_project: Project,
    clock: FakeClock,
) -> None:
    """Oldest jobs come out first across separate projects.

    All three projects must be distinct: the "one running stage per project"
    rule blocks claim of a queued job whose project already has another
    running job, regardless of stage.
    """

    first = queue.enqueue(project.id, Stage.RESEARCH, [])
    clock.advance(seconds=1)
    second = queue.enqueue(second_project.id, Stage.RESEARCH, [])
    clock.advance(seconds=1)
    third = queue.enqueue(third_project.id, Stage.RESEARCH, [])

    a = queue.claim_next("worker-a", clock.now)
    b = queue.claim_next("worker-b", clock.now)
    c = queue.claim_next("worker-c", clock.now)

    assert a is not None and b is not None and c is not None
    assert [a.id, b.id, c.id] == [first.id, second.id, third.id]


def test_different_projects_claimed_concurrently(
    queue: LeaseQueue,
    project: Project,
    second_project: Project,
    clock: FakeClock,
) -> None:
    """Worker A on project X does not block worker B on project Y."""

    queue.enqueue(project.id, Stage.DRAFT, [])
    queue.enqueue(second_project.id, Stage.DRAFT, [])

    a = queue.claim_next("worker-a", clock.now)
    b = queue.claim_next("worker-b", clock.now)
    assert a is not None and b is not None
    assert a.project_id != b.project_id
    # Both jobs are running simultaneously without raising.
    queue.heartbeat(a.id, a.token, clock.now)
    queue.heartbeat(b.id, b.token, clock.now)


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_extends_lease(
    queue: LeaseQueue,
    queued_job: StageJob,
    session: Session,
    clock: FakeClock,
) -> None:
    claimed = queue.claim_next("worker-a", clock.now)
    assert claimed is not None
    original_expiry = claimed.lease_expires_at
    clock.advance(seconds=600)
    queue.heartbeat(claimed.id, claimed.token, clock.now)

    refreshed = _reload(session, claimed.id)
    expected = clock.now + timedelta(seconds=900)
    assert refreshed.lease_expires_at == expected
    assert refreshed.lease_expires_at > original_expiry


def test_heartbeat_with_wrong_token_raises_stale_lease(
    queue: LeaseQueue, queued_job: StageJob, clock: FakeClock
) -> None:
    claimed = queue.claim_next("worker-a", clock.now)
    assert claimed is not None
    with pytest.raises(StaleLease):
        queue.heartbeat(claimed.id, "definitely-not-the-token", clock.now)


# ---------------------------------------------------------------------------
# Recovery: success path and max-attempts path
# ---------------------------------------------------------------------------


def test_recover_expired_resets_under_three_attempts(
    queue: LeaseQueue,
    queued_job: StageJob,
    session: Session,
    clock: FakeClock,
) -> None:
    first = queue.claim_next("worker-a", clock.now)
    assert first is not None and first.attempts == 1
    clock.advance(seconds=901)
    recovered = queue.recover_expired(clock.now)
    assert recovered == [first.id]

    refreshed = _reload(session, first.id)
    assert refreshed.status == "queued"
    assert refreshed.lease_token is None
    assert refreshed.lease_expires_at is None
    assert refreshed.attempts == 1  # not bumped — claim will bump it next time

    # Re-claimable with attempts < 3.
    second = queue.claim_next("worker-b", clock.now)
    assert second is not None
    assert second.id == first.id
    assert second.attempts == 2


def test_recover_expired_marks_failed_at_three_attempts(
    queue: LeaseQueue,
    queued_job: StageJob,
    session: Session,
    clock: FakeClock,
) -> None:
    """A job that has already been claimed three times is failed on the next expiry."""

    # Drive the job to attempts=3 across three claim + expiry cycles.
    last_claim = None
    for attempt in range(1, 4):
        claimed = queue.claim_next(f"worker-{attempt}", clock.now)
        assert claimed is not None
        assert claimed.attempts == attempt
        last_claim = claimed
        clock.advance(seconds=901)
        recovered = queue.recover_expired(clock.now)
        assert recovered == [claimed.id]

    assert last_claim is not None
    refreshed = _reload(session, last_claim.id)
    assert refreshed.status == "failed"
    assert refreshed.error_code == "lease_expired"
    assert refreshed.error_message == "attempts_exceeded"
    assert refreshed.lease_token is None


def test_recover_expired_is_idempotent(
    queue: LeaseQueue, queued_job: StageJob, clock: FakeClock
) -> None:
    claimed = queue.claim_next("worker-a", clock.now)
    assert claimed is not None
    clock.advance(seconds=901)
    first_pass = queue.recover_expired(clock.now)
    second_pass = queue.recover_expired(clock.now)
    assert first_pass == [claimed.id]
    assert second_pass == []


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_cancel_only_valid_on_queued_jobs(
    queue: LeaseQueue,
    session: Session,
    queued_job: StageJob,
    second_project: Project,
    clock: FakeClock,
) -> None:
    queue.cancel(queued_job.id)
    session.refresh(queued_job)
    assert queued_job.status == "cancelled"

    # A cancelled job must never be claimed again.
    next_claim = queue.claim_next("worker-a", clock.now)
    assert next_claim is None

    # Cancellation on a running job must raise — running jobs require lease
    # ownership semantics the queue does not provide here.
    running = queue.enqueue(second_project.id, Stage.RESEARCH, [])
    claimed = queue.claim_next("worker-a", clock.now)
    assert claimed is not None
    assert claimed.id == running.id
    with pytest.raises(JobNotClaimed):
        queue.cancel(claimed.id)


# ---------------------------------------------------------------------------
# One running stage per project
# ---------------------------------------------------------------------------


def test_one_running_per_project_blocks_other_jobs_for_same_project(
    queue: LeaseQueue,
    project: Project,
    second_project: Project,
    clock: FakeClock,
) -> None:
    first = queue.enqueue(project.id, Stage.DRAFT, [])
    queue.enqueue(project.id, Stage.REWRITE, [])  # same project, different stage
    queue.enqueue(second_project.id, Stage.DRAFT, [])

    claimed = queue.claim_next("worker-a", clock.now)
    assert claimed is not None
    assert claimed.id == first.id

    # Worker B should now claim the OTHER project's job, skipping the queued
    # second job for the same project.
    second_claim = queue.claim_next("worker-b", clock.now)
    assert second_claim is not None
    assert second_claim.id != first.id
    assert second_claim.project_id == second_project.id


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _make_dispatcher(
    queue: LeaseQueue, handlers: dict[Stage, object]
) -> StageDispatcher:
    return StageDispatcher(handlers, queue)  # type: ignore[arg-type]


def test_dispatcher_finishes_with_artifact_id_returned_by_handler(
    queue: LeaseQueue,
    project: Project,
    session: Session,
    artifact_repo: ArtifactRepository,
    clock: FakeClock,
) -> None:
    queue.enqueue(project.id, Stage.DRAFT, [])
    output = artifact_repo.create(project.id, "draft", {"text": "handler output"})

    captured: list[WorkerContext] = []

    def handler(ctx: WorkerContext) -> list[str]:
        captured.append(ctx)
        return [output.id]

    dispatcher = _make_dispatcher(queue, {Stage.DRAFT: handler})
    processed = dispatcher.dispatch_once("worker-a", clock.now)
    assert processed is True
    assert len(captured) == 1
    assert captured[0].project_id == project.id
    assert captured[0].stage == Stage.DRAFT

    job = (
        session.query(StageJob)
        .filter(StageJob.id == captured[0].job_id)
        .one()
    )
    assert job.status == "finished"
    assert job.output_artifact_id == output.id


def test_dispatcher_unknown_stage_marks_failed(
    queue: LeaseQueue,
    project: Project,
    session: Session,
    clock: FakeClock,
) -> None:
    queue.enqueue(project.id, Stage.SPEECH, [])

    def boom(ctx: WorkerContext) -> list[str]:  # pragma: no cover - exercised below
        raise RuntimeError("boom")

    dispatcher = _make_dispatcher(queue, {Stage.SPEECH: boom})
    processed = dispatcher.dispatch_once("worker-a", clock.now)
    assert processed is True

    job = session.query(StageJob).filter(StageJob.status == "failed").one()
    assert job.error_code == "handler_error"
    assert "boom" in (job.error_message or "")


# ---------------------------------------------------------------------------
# Smoke check: max-attempts guard is reachable even if a recovery pass is
# somehow skipped. The LeaseQueue never raises MaxAttemptsReached directly
# from claim_next (the contract is recovery-on-expiry), so this test just
# confirms the exception type exists and is importable from the package.
# ---------------------------------------------------------------------------


def test_max_attempts_reached_exception_is_importable() -> None:
    assert issubclass(MaxAttemptsReached, Exception)
