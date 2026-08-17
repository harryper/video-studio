"""Durable background-job queue for Content Studio stages.

Every long-running pipeline stage (diagnosis, research, pitches, narrative,
draft, rewrite, speech, approval) is enqueued as a ``StageJob`` and claimed by
a worker process. The queue provides:

* FIFO claim ordering with optimistic-concurrency: the claim ``UPDATE`` is
  guarded by ``WHERE status='queued'`` so two workers that both read the same
  row cannot both succeed in turning it into ``running``.
* Cryptographically random lease tokens so a finished job cannot be written by
  any worker other than the one that holds the lease.
* Time-bounded leases (default 900 s) that ``recover_expired`` reaps back to
  ``queued`` until a job reaches ``MAX_ATTEMPTS``, at which point it is marked
  ``failed``.
* A "one running stage per project" rule so a single project never has two
  workers competing on the same pipeline concurrently.
"""

from __future__ import annotations

import enum
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from studio.models import StageJob

LEASE_SECONDS = 900
MAX_ATTEMPTS = 3


class Stage(str, enum.Enum):
    """Pipeline stages that may be enqueued."""

    DIAGNOSIS = "diagnosis"
    RESEARCH = "research"
    PITCHES = "pitches"
    NARRATIVE = "narrative"
    DRAFT = "draft"
    REWRITE = "rewrite"
    SPEECH = "speech"
    APPROVAL = "approval"


class StaleLease(Exception):
    """Raised when a worker operates on a job it no longer owns."""


class JobNotClaimed(Exception):
    """Raised when an operation requires a leased job that is not running."""


class MaxAttemptsReached(Exception):
    """Raised when a job cannot be claimed because it has hit MAX_ATTEMPTS."""


def _validate_lease(job: StageJob | None, job_id: str, token: str) -> None:
    """Raise ``StaleLease`` with a precise reason when the lease is not held."""

    if job is None:
        raise StaleLease(f"job {job_id} not found")
    if job.status != "running":
        raise StaleLease(
            f"job {job_id} is in status {job.status!r}; lease operations require running"
        )
    if job.lease_token != token:
        raise StaleLease(f"job {job_id} lease_token mismatch")


@dataclass(frozen=True)
class ClaimedJob:
    """Lease handle returned to a worker that successfully claimed a job."""

    id: str
    project_id: str
    stage: Stage
    attempts: int
    token: str
    lease_expires_at: datetime
    input_artifact_ids: list[str] = field(default_factory=list)


class LeaseQueue:
    """Repository for ``stage_jobs`` rows plus their lease lifecycle."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # enqueue
    # ------------------------------------------------------------------
    def enqueue(
        self,
        project_id: str,
        stage: Stage,
        input_artifact_ids: list[str],
    ) -> StageJob:
        """Insert a fresh ``queued`` job. Caller owns the surrounding transaction."""

        job = StageJob(
            project_id=project_id,
            stage=stage.value if isinstance(stage, Stage) else str(stage),
            status="queued",
            attempts=0,
            lease_token=None,
            lease_expires_at=None,
            input_artifact_ids=list(input_artifact_ids),
            output_artifact_id=None,
            error_code=None,
            error_message=None,
        )
        self._session.add(job)
        self._session.flush()
        return job

    # ------------------------------------------------------------------
    # claim
    # ------------------------------------------------------------------
    def claim_next(self, worker_id: str, now: datetime) -> ClaimedJob | None:
        """Claim the oldest queued job not blocked by another running job.

        Concurrency safety is achieved with optimistic concurrency: the SELECT
        finds the next claimable row, then an ``UPDATE`` guarded by
        ``WHERE status='queued'`` flips it to ``running``. If two workers race
        for the same row only one UPDATE succeeds (``rowcount == 1``); the
        loser sees ``rowcount == 0`` and reports no claim. This is equivalent
        to ``BEGIN IMMEDIATE`` for SQLite FIFO correctness without needing to
        fight SQLAlchemy's implicit transaction.
        """

        token = secrets.token_urlsafe(32)
        new_expiry = now + timedelta(seconds=LEASE_SECONDS)

        try:
            candidate_stmt = (
                select(StageJob.id)
                .where(StageJob.status == "queued")
                .where(
                    StageJob.project_id.notin_(
                        select(StageJob.project_id).where(StageJob.status == "running")
                    )
                )
                .order_by(StageJob.created_at.asc(), StageJob.id.asc())
                .limit(1)
            )
            candidate_id = self._session.execute(candidate_stmt).scalar_one_or_none()
            if candidate_id is None:
                return None

            claim_stmt = (
                update(StageJob)
                .where(StageJob.id == candidate_id, StageJob.status == "queued")
                .values(
                    status="running",
                    lease_token=token,
                    lease_expires_at=new_expiry,
                    attempts=StageJob.attempts + 1,
                )
            )
            result = self._session.execute(claim_stmt)
            if result.rowcount != 1:
                # Lost the race to another worker. Roll back to drop the failed
                # UPDATE; the caller may retry.
                self._session.rollback()
                return None
            self._session.commit()

            # Re-read the freshly-claimed row so we return authoritative state
            # (the UPDATE above mutated the row in-place but the session may
            # have cached a stale snapshot before the write).
            claimed = self._session.get(StageJob, candidate_id)
            assert claimed is not None

            return ClaimedJob(
                id=claimed.id,
                project_id=claimed.project_id,
                stage=Stage(claimed.stage),
                attempts=claimed.attempts,
                token=token,
                lease_expires_at=new_expiry,
                input_artifact_ids=list(claimed.input_artifact_ids or []),
            )
        except Exception:
            try:
                self._session.rollback()
            except Exception:  # pragma: no cover - rollback is best-effort
                pass
            raise

    # ------------------------------------------------------------------
    # heartbeat
    # ------------------------------------------------------------------
    def heartbeat(self, job_id: str, token: str, now: datetime) -> None:
        """Extend the lease by ``LEASE_SECONDS`` if the caller still owns it."""

        job = self._session.get(StageJob, job_id)
        _validate_lease(job, job_id, token)
        job.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        self._session.commit()

    # ------------------------------------------------------------------
    # finish / fail
    # ------------------------------------------------------------------
    def finish(
        self,
        job_id: str,
        token: str,
        output_artifact_id: str,
    ) -> None:
        """Mark a leased job as finished and attach its output artifact."""

        job = self._session.get(StageJob, job_id)
        _validate_lease(job, job_id, token)
        job.status = "finished"
        job.output_artifact_id = output_artifact_id
        job.lease_token = None
        job.lease_expires_at = None
        self._session.commit()

    def fail(
        self,
        job_id: str,
        token: str,
        code: str,
        message: str,
    ) -> None:
        """Mark a leased job as failed and clear its lease."""

        job = self._session.get(StageJob, job_id)
        _validate_lease(job, job_id, token)
        job.status = "failed"
        job.error_code = code
        job.error_message = message
        job.lease_token = None
        job.lease_expires_at = None
        self._session.commit()

    # ------------------------------------------------------------------
    # cancel
    # ------------------------------------------------------------------
    def cancel(self, job_id: str) -> None:
        """Cancel a queued job. Running jobs must be failed, not cancelled."""

        job = self._session.get(StageJob, job_id)
        if job is None:
            raise JobNotClaimed(f"job {job_id} not found")
        if job.status != "queued":
            raise JobNotClaimed(
                f"job {job_id} is in status {job.status!r}; only queued jobs can be cancelled"
            )
        job.status = "cancelled"
        self._session.commit()

    # ------------------------------------------------------------------
    # recovery
    # ------------------------------------------------------------------
    def recover_expired(self, now: datetime) -> list[str]:
        """Reap expired leases. Under MAX_ATTEMPTS -> queued; at the limit -> failed."""

        stmt = (
            select(StageJob)
            .where(StageJob.status == "running")
            .where(StageJob.lease_expires_at <= now)
        )
        expired = list(self._session.execute(stmt).scalars().all())
        touched: list[str] = []
        for job in expired:
            if job.attempts < MAX_ATTEMPTS:
                job.status = "queued"
                job.lease_token = None
                job.lease_expires_at = None
            else:
                job.status = "failed"
                job.error_code = "lease_expired"
                job.error_message = "attempts_exceeded"
                job.lease_token = None
                job.lease_expires_at = None
            touched.append(job.id)
        if touched:
            self._session.commit()
        return touched


__all__ = [
    "ClaimedJob",
    "JobNotClaimed",
    "LEASE_SECONDS",
    "LeaseQueue",
    "MAX_ATTEMPTS",
    "MaxAttemptsReached",
    "Stage",
    "StaleLease",
]
