"""Stage dispatcher and worker loop.

A ``StageHandler`` is a pure function from a :class:`WorkerContext` to the
list of artifact IDs the stage produced. The :class:`StageDispatcher` wires
the handler to the :class:`~studio.jobs.LeaseQueue` lifecycle:

1. claim one job,
2. call the handler,
3. on success: ``finish(job_id, token, output_artifact_id)``,
4. on failure: ``fail(job_id, token, code, message)``.

The :meth:`StageDispatcher.run` loop drives a long-running worker process and
is consumed by Task 14 to host the live Content Studio pipeline.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from studio.jobs import ClaimedJob, LeaseQueue, Stage


@dataclass(frozen=True)
class WorkerContext:
    """Read-only context passed to a stage handler."""

    job_id: str
    project_id: str
    stage: Stage
    input_artifact_ids: list[str]
    now: datetime
    db_session: Session


StageHandler = Callable[[WorkerContext], list[str]]


@dataclass
class DispatchUnknownStage(Exception):
    """Raised when a worker attempts to dispatch a stage with no registered handler."""

    stage: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"no handler registered for stage {self.stage!r}"


class StageDispatcher:
    """Claim-handle-finish lifecycle glue between :class:`LeaseQueue` and handlers."""

    def __init__(
        self,
        handlers: dict[Stage, StageHandler],
        queue: LeaseQueue,
    ) -> None:
        self._handlers = dict(handlers)
        self._queue = queue

    # ------------------------------------------------------------------
    def dispatch_once(self, worker_id: str, now: datetime) -> bool:
        """Claim a single job, run its handler, and finish/fail. Returns True if a job ran."""

        claimed = self._queue.claim_next(worker_id, now)
        if claimed is None:
            return False
        self._handle_claimed(claimed, now)
        return True

    # ------------------------------------------------------------------
    def run(
        self,
        worker_id: str,
        heartbeats: list[tuple[ClaimedJob, datetime]],
        shutdown_event: threading.Event,
        tick_seconds: float = 2.0,
    ) -> Iterator[bool]:
        """Yield one True/False per dispatched job until ``shutdown_event`` is set.

        ``heartbeats`` is a mutable buffer the worker loop writes to; callers
        that want heartbeats to fire (the long-running worker process in
        Task 14) just keep a reference and schedule a periodic extender thread
        against the latest entry. Tests can pass ``[]`` to disable heartbeats.
        """

        while not shutdown_event.is_set():
            now = datetime.now(timezone.utc)
            try:
                processed = self.dispatch_once(worker_id, now)
            except Exception:
                # Dispatcher never crashes the loop — recovery + the next
                # tick will pick up any orphaned jobs.
                processed = False
            if processed:
                yield True
            else:
                yield False
            if not shutdown_event.wait(tick_seconds):
                continue

    # ------------------------------------------------------------------
    def _handle_claimed(self, claimed: ClaimedJob, now: datetime) -> None:
        handler = self._handlers.get(claimed.stage)
        if handler is None:
            # No handler registered for this stage — treat as a hard failure
            # with a stable error code so the operator can spot the
            # misconfiguration. Covers both "stage is registered in the enum
            # but this dispatcher lacks a handler" and "stage value from the
            # DB is not a valid enum member".
            self._queue.fail(
                claimed.id,
                claimed.token,
                "handler_missing",
                f"no handler registered for stage {claimed.stage.value!r}",
            )
            return

        session = self._queue._session  # noqa: SLF001 - dispatcher owns the queue's session
        ctx = WorkerContext(
            job_id=claimed.id,
            project_id=claimed.project_id,
            stage=claimed.stage,
            input_artifact_ids=list(claimed.input_artifact_ids),
            now=now,
            db_session=session,
        )
        try:
            outputs = handler(ctx)
        except Exception as exc:
            self._queue.fail(
                claimed.id,
                claimed.token,
                "handler_error",
                _short_error(exc),
            )
            return

        if not outputs:
            self._queue.fail(
                claimed.id,
                claimed.token,
                "handler_empty",
                "handler returned no artifact ids",
            )
            return
        # Single artifact per job is the current contract; the list exists so
        # future multi-output stages (e.g. multi-language translations) can
        # add additional rows without breaking the dispatcher signature.
        self._queue.finish(claimed.id, claimed.token, outputs[0])


def _short_error(exc: BaseException) -> str:
    msg = str(exc).strip()
    if not msg:
        return exc.__class__.__name__
    return f"{exc.__class__.__name__}: {msg}"[:512]


__all__ = [
    "DispatchUnknownStage",
    "StageDispatcher",
    "StageHandler",
    "WorkerContext",
]
