"""Server-Sent Events stream + session helper endpoints.

The SSE generator at ``GET /api/projects/{id}/events`` emits one event per
``StageJob`` row for the project, plus periodic heartbeat comments. The
client may send ``Last-Event-ID`` to skip events whose id it has already
seen; since :attr:`~studio.models.StageJob.id` is a uuid4, comparison is
done by membership rather than ordering.

Endpoints:

* ``POST /api/session`` — login; mint a session cookie and CSRF token.
* ``POST /api/session/logout`` — drop the cookie.
* ``GET  /api/csrf`` — return the per-session CSRF token (requires session).
* ``GET  /api/projects/{id}/events`` — SSE stream of job progress.

Implementation note: the generator is a synchronous Python generator so
:func:`fastapi.testclient.TestClient` can drive it without the async/thread
pool plumbing that ``anyio`` adds. The generator ``time.sleep``s between
polls, so the worker thread is released back to the pool on every tick.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from studio.api.auth import (
    SessionInfo,
    check_password,
    clear_session_cookie,
    mint_session,
    require_session,
    set_session_cookie,
)
from studio.api.dependencies import get_session_factory, get_settings
from studio.config import Settings
from studio.models import Project, StageJob

router = APIRouter(tags=["events"])


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------


class LoginBody(BaseModel):
    password: str


class JobProgress(BaseModel):
    """Per-job snapshot streamed through SSE."""

    id: str
    job_id: str
    stage: str
    status: str
    ts: datetime
    attempt: int
    error_code: str | None
    error_message: str | None


class LogoutResponse(BaseModel):
    ok: bool


class CsrfResponse(BaseModel):
    csrf_token: str


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _snapshot(job: StageJob) -> JobProgress:
    return JobProgress(
        id=job.id,
        job_id=job.id,
        stage=job.stage,
        status=job.status,
        ts=job.updated_at,
        attempt=job.attempts,
        error_code=job.error_code,
        error_message=job.error_message,
    )


def _format_event(progress: JobProgress) -> str:
    payload = progress.model_dump(mode="json")
    return (
        f"id: {progress.id}\n"
        f"event: progress\n"
        f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
    )


def _format_comment(text: str) -> str:
    return f": {text}\n\n"


# ---------------------------------------------------------------------------
# session endpoints
# ---------------------------------------------------------------------------


@router.post("/api/session")
def login(
    body: LoginBody,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """Mint a session cookie on password match; returns CSRF token in body.

    Status defaults to 200 (not 204) because the CSRF token must travel back
    to the SPA in the response body — the cookie itself is HttpOnly and the
    SPA cannot read it. A 204 with a body is a 200 in disguise, so the
    canonical contract is 200 + JSON.
    """

    if not check_password(settings, body.password):
        raise HTTPException(status_code=401, detail="invalid credentials")
    cookie_value, csrf_token = mint_session(settings)
    set_session_cookie(response, cookie_value, settings)
    return {"csrf_token": csrf_token}


@router.post("/api/session/logout")
def logout(
    response: Response,
    settings: Settings = Depends(get_settings),
) -> LogoutResponse:
    clear_session_cookie(response, settings)
    return LogoutResponse(ok=True)


@router.get("/api/csrf", dependencies=[Depends(require_session)])
def get_csrf(session: SessionInfo = Depends(require_session)) -> CsrfResponse:
    return CsrfResponse(csrf_token=session.csrf_token)


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


def _load_jobs(factory: sessionmaker[Session], project_id: str) -> tuple[bool, list[StageJob]]:
    """Fetch the project's jobs. Returns ``(project_exists, jobs)``."""

    session = factory()
    try:
        project = session.get(Project, project_id)
        if project is None:
            return False, []
        stmt = (
            select(StageJob)
            .where(StageJob.project_id == project_id)
            .order_by(StageJob.created_at.asc(), StageJob.id.asc())
        )
        return True, list(session.execute(stmt).scalars())
    finally:
        session.close()


def _sse_stream(
    project_id: str,
    last_event_id: str | None,
    factory: sessionmaker[Session],
    poll_interval_seconds: float,
    heartbeat_seconds: float,
    max_runtime_seconds: float,
    request: Request,
) -> Iterator[str]:
    """Yield SSE-encoded strings until the client disconnects or the runtime cap hits.

    The generator is intentionally synchronous so :func:`TestClient` can drive
    it without :mod:`anyio`'s async thread plumbing. ``Request.is_disconnected``
    is async; we approximate the disconnect signal with the
    ``request._is_disconnected`` flag (set by Starlette once the receive
    channel closes) and bail out when it flips.

    Per-job state (``stage``/``status``/``attempts``/``error_code``) is tracked
    in memory: we emit a fresh event whenever it changes so the client can
    observe progress updates without re-replaying already-seen snapshots.
    """

    yield _format_comment("connected")

    seen_ids: set[str] = set()
    state: dict[str, tuple[str, str, int, str | None]] = {}
    skip_initial = last_event_id is not None
    last_heartbeat = time.monotonic()
    started_at = time.monotonic()

    while True:
        if getattr(request, "_is_disconnected", False):
            return
        if (
            max_runtime_seconds > 0
            and (time.monotonic() - started_at) >= max_runtime_seconds
        ):
            yield _format_comment("closing")
            return
        exists, jobs = _load_jobs(factory, project_id)
        if not exists:
            payload = json.dumps(
                {"code": "not_found", "message": f"project {project_id} not found"},
                separators=(",", ":"),
            )
            yield f"event: error\ndata: {payload}\n\n"
            return

        emitted = False
        for job in jobs:
            signature = (job.stage, job.status, job.attempts, job.error_code)
            previous = state.get(job.id)
            if skip_initial and job.id == last_event_id:
                seen_ids.add(job.id)
                state[job.id] = signature
                continue
            if job.id in seen_ids and previous == signature:
                # Unchanged since we last emitted — drop.
                continue
            seen_ids.add(job.id)
            state[job.id] = signature
            yield _format_event(_snapshot(job))
            emitted = True

        # After the first tick we've replayed the backlog; from then on only
        # new / changed jobs emit.
        skip_initial = False

        now_ts = time.monotonic()
        if emitted:
            last_heartbeat = now_ts
        elif (now_ts - last_heartbeat) >= heartbeat_seconds:
            yield _format_comment("heartbeat")
            last_heartbeat = now_ts

        if getattr(request, "_is_disconnected", False):
            return
        time.sleep(poll_interval_seconds)


@router.get(
    "/api/projects/{project_id}/events",
    dependencies=[Depends(require_session)],
)
def project_events(
    project_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Stream job progress for ``project_id`` as ``text/event-stream``."""

    poll_seconds = max(0.05, settings.sse_poll_interval_ms / 1000.0)
    heartbeat_seconds = max(0.5, settings.sse_heartbeat_ms / 1000.0)
    max_runtime_seconds = max(0.0, settings.sse_max_runtime_ms / 1000.0)

    return StreamingResponse(
        _sse_stream(
            project_id,
            last_event_id,
            factory,
            poll_seconds,
            heartbeat_seconds,
            max_runtime_seconds,
            request,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = ["JobProgress", "router"]