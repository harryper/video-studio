"""Server-Sent Events stream for Content Studio.

The SSE handler at ``GET /api/projects/{id}/events`` must:

* Replay every job's current state on connect (or only those whose id is not
  in ``Last-Event-ID``).
* Emit an event whenever a job's status / attempts / error changes.
* Heartbeat with a comment frame so reverse proxies don't time the connection out.
* Stop streaming when the client disconnects.

These tests use very short poll / heartbeat intervals plus a max-runtime cap
(see ``STUDIO_SSE_MAX_RUNTIME_MS``) so the suite stays fast; :class:`TestClient`
buffers streaming bodies until the response ends, so the assertions read the
full body once the generator returns.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from studio.jobs import LeaseQueue, Stage
from studio.models import Project, StageJob

# ---------------------------------------------------------------------------
# SSE parsing helper
# ---------------------------------------------------------------------------


def _parse_sse(body: bytes) -> list[dict[str, Any]]:
    """Split a buffered SSE body into ``{comment|data|id|event}`` dicts."""

    events: list[dict[str, Any]] = []
    event: dict[str, Any] = {}
    for line in body.decode("utf-8").split("\n"):
        if line == "":
            if event:
                events.append(event)
                event = {}
            continue
        if line.startswith(":"):
            events.append({"comment": line[1:].strip()})
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if key == "data":
                try:
                    event[key] = json.loads(value)
                except json.JSONDecodeError:
                    event[key] = value
            else:
                event[key] = value
    if event:
        events.append(event)
    return events


def _event_body(client: TestClient, url: str, **kwargs) -> list[dict[str, Any]]:
    """Open the SSE stream, wait for it to close, then parse the buffered body."""

    start = time.monotonic()
    with client.stream("GET", url, **kwargs) as response:
        assert response.status_code == 200, response.read()
        body = b"".join(response.iter_bytes())
    elapsed = time.monotonic() - start
    return _parse_sse(body), elapsed


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_sse(
    monkeypatch: pytest.MonkeyPatch, isolated_database: str
) -> TestClient:
    """TestClient with shortened SSE poll / heartbeat intervals and a runtime cap."""

    from fastapi.testclient import TestClient

    from studio.api.app import create_app

    monkeypatch.setenv("STUDIO_CONTENT_STUDIO_PASSWORD", "test-password")
    monkeypatch.setenv("STUDIO_SSE_POLL_INTERVAL_MS", "50")
    monkeypatch.setenv("STUDIO_SSE_HEARTBEAT_MS", "200")
    monkeypatch.setenv("STUDIO_SSE_MAX_RUNTIME_MS", "1500")
    return TestClient(create_app())


@pytest.fixture
def authed_fast_sse(fast_sse: TestClient) -> TestClient:
    login = fast_sse.post("/api/session", json={"password": "test-password"})
    assert login.status_code == 200, login.text
    fast_sse._csrf_token = login.json()["csrf_token"]
    return fast_sse


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_sse_heartbeat_within_15s_of_idle(
    authed_fast_sse: TestClient, project: Project
) -> None:
    """Idle stream must emit a ``: heartbeat`` comment within ~1 s (test mode)."""

    events, _ = _event_body(
        authed_fast_sse, f"/api/projects/{project.id}/events"
    )
    comments = [e["comment"] for e in events if "comment" in e]
    assert "connected" in comments
    assert "heartbeat" in comments


def test_sse_replays_events_after_last_event_id(
    authed_fast_sse: TestClient, session: Session, project: Project
) -> None:
    """Reconnect with ``Last-Event-ID`` set skips the already-seen event."""

    job = LeaseQueue(session).enqueue(project.id, Stage.DIAGNOSIS, [])
    session.commit()

    # First connection: capture the queued event for our job.
    events, _ = _event_body(
        authed_fast_sse, f"/api/projects/{project.id}/events"
    )
    job_events = [
        e
        for e in events
        if "data" in e and isinstance(e.get("id"), str) and e["id"] == job.id
    ]
    assert len(job_events) == 1
    assert job_events[0]["data"]["status"] == "queued"

    # Reconnect with Last-Event-ID — the queued event must NOT be re-emitted.
    events, _ = _event_body(
        authed_fast_sse,
        f"/api/projects/{project.id}/events",
        headers={"Last-Event-ID": job.id},
    )
    assert all(e.get("id") != job.id for e in events)


def test_sse_streams_finished_status(
    authed_fast_sse: TestClient, session: Session, project: Project
) -> None:
    """When a job transitions to ``running``, the stream emits a new event.

    Drives the status change from a separate thread because the SSE handler
    blocks the test thread while it streams. We sleep briefly so the handler
    has time to observe the queued state, then mutate the row.
    """

    job = LeaseQueue(session).enqueue(project.id, Stage.DIAGNOSIS, [])
    session.commit()
    job_id = job.id

    # The status flip has to land inside the generator's polling window. The
    # handler is a sync generator that yields synchronously, so we mutate
    # from the same thread between the connection open and the buffer read
    # by piggy-backing on the response iterator's blocking nature: a small
    # thread does the mutation while the main thread waits for the response
    # to end (via the runtime cap).
    import threading

    def flip_status() -> None:
        time.sleep(0.3)
        session.expire_all()
        row = session.get(StageJob, job_id)
        row.status = "running"
        row.attempts = 1
        session.commit()

    worker = threading.Thread(target=flip_status)
    worker.start()
    events, _ = _event_body(
        authed_fast_sse, f"/api/projects/{project.id}/events"
    )
    worker.join()

    job_events = [
        e
        for e in events
        if "data" in e and isinstance(e.get("id"), str) and e["id"] == job_id
    ]
    seen_statuses = [e["data"]["status"] for e in job_events]
    assert "queued" in seen_statuses
    assert "running" in seen_statuses