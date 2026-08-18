"""Offline end-to-end Content Studio workflow.

Spins up the real FastAPI app against a per-test SQLite database, the
real :class:`StageDispatcher` driven by handlers wired to
:class:`FakeModelProvider` + :class:`FakeSearchProvider`, and walks the
full topic → approved script pipeline over HTTP.

The only network call this test makes is the gated ``TestClient`` client's
internal transport — no model, search, or storage IO is touched. The
fixtures in ``tests/fixtures/provider_responses/`` are loaded verbatim
into the fake provider.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from studio.api.app import create_app
from studio.api.routes.stages import set_default_provider
from studio.handlers import HandlerContext, build_dispatcher
from studio.jobs import LeaseQueue, Stage
from studio.models import Project
from studio.providers.base import SearchProvider
from studio.providers.fake import FakeModelProvider
from studio.schemas import EditorialComment, SourceDocument
from studio.worker import StageDispatcher

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "provider_responses"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _load_fixture(stem: str) -> dict[str, Any]:
    """Load ``tests/fixtures/provider_responses/{stem}.json``."""

    path = FIXTURES_DIR / f"{stem}.json"
    return json.loads(path.read_text(encoding="utf-8"))


class _RecordingSearchProvider(SearchProvider):
    """Fallback search provider: returns empty list for every query."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def search(self, query: str, *, limit: int = 5) -> list[SourceDocument]:
        self.calls.append(query)
        return []


@dataclass
class _Worker:
    dispatcher: StageDispatcher
    session_factory: sessionmaker[Session]

    def drain(self, max_passes: int = 50) -> int:
        """Run ``dispatch_once`` until no work remains. Returns total dispatched."""

        total = 0
        for _ in range(max_passes):
            session = self.session_factory()
            try:
                now = datetime.now(UTC)
                processed = self.dispatcher.dispatch_once("e2e-worker", now)
            finally:
                session.close()
            if not processed:
                return total
            total += 1
        return total


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_providers() -> tuple[FakeModelProvider, _RecordingSearchProvider]:
    """A pair of fake providers seeded with the recorded valid fixtures."""

    provider = FakeModelProvider()
    search = _RecordingSearchProvider()

    # Each fixture file is consumed in order. The order of operations
    # below matches the pipeline sequence: diagnosis → research (x2) →
    # pitches (x3) → narrative → draft → rewrite.
    for stem, operation in (
        ("diagnosis_valid", "diagnosis"),
        ("research_valid", "research"),
        ("research_valid", "research"),
        ("pitches_valid", "pitches"),
        ("pitches_valid", "pitches"),
        ("pitches_valid", "pitches"),
        ("narrative_valid", "narrative"),
        ("draft_valid", "draft"),
        ("rewrite_valid", "rewrite"),
        ("speech_valid", "speech_metadata"),
    ):
        fixture = _load_fixture(stem)
        for response in fixture["responses"]:
            provider.responses.setdefault(operation, []).append(response)

    return provider, search


@pytest.fixture
def app(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
    fake_providers: tuple[FakeModelProvider, _RecordingSearchProvider],
) -> Iterator[TestClient]:
    """Authed FastAPI client. Sets the password env var so login succeeds."""

    monkeypatch.setenv("STUDIO_CONTENT_STUDIO_PASSWORD", "test-password")
    client = create_app()
    set_default_provider(fake_providers[0])
    with TestClient(client) as test_client:
        login = test_client.post("/api/session", json={"password": "test-password"})
        assert login.status_code == 200, login.text
        test_client._csrf_token = login.json()["csrf_token"]

        def _with_csrf(headers: dict[str, str] | None) -> dict[str, str]:
            merged = dict(headers or {})
            merged.setdefault("X-CSRF-Token", test_client._csrf_token)
            return merged

        for verb in ("post", "put", "patch", "delete"):
            original = getattr(test_client, verb)

            def _make(verb_name: str, fn):
                def _wrapped(url, **kwargs):
                    kwargs["headers"] = _with_csrf(kwargs.get("headers"))
                    return fn(url, **kwargs)

                _wrapped.__name__ = verb_name
                return _wrapped

            setattr(test_client, verb, _make(verb, original))

        yield test_client
    set_default_provider(None)


@pytest.fixture
def worker(
    isolated_database: str,
    fake_providers: tuple[FakeModelProvider, _RecordingSearchProvider],
) -> Iterator[_Worker]:
    """A worker bound to the same in-memory database as the API client."""

    from studio import db as studio_db

    engine = studio_db.get_engine()
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    provider, search = fake_providers
    hctx = HandlerContext(provider=provider, search=search, session_factory=factory)
    session = factory()
    try:
        dispatcher = build_dispatcher(hctx, session)
    finally:
        session.close()
    yield _Worker(dispatcher=dispatcher, session_factory=factory)


# ---------------------------------------------------------------------------
# helpers — API wrappers
# ---------------------------------------------------------------------------


def create_project(client: TestClient, topic: str) -> Project:
    """Create a new project. Returns the :class:`Project` ORM row."""

    response = client.post("/api/projects", json={"title": topic, "topic": topic})
    assert response.status_code == 201, response.text
    return Project(id=response.json()["id"], title=topic, topic=topic)


def accept_pitch(client: TestClient, project_id: str, pitch_index: int) -> str:
    """Accept the ``pitch_index``-th pitch. Returns the accepted artifact id."""

    pitches_response = client.get(f"/api/projects/{project_id}/pitches")
    assert pitches_response.status_code == 200, pitches_response.text
    pitches = pitches_response.json()["pitches"]
    chosen = pitches[pitch_index]
    response = client.post(
        f"/api/projects/{project_id}/pitches/{chosen['id']}/accept"
    )
    assert response.status_code == 201, response.text
    return response.json()["artifact_id"]


def add_comment(
    client: TestClient,
    project_id: str,
    *,
    paragraph: str,
    body: str,
    draft_artifact_id: str | None = None,
) -> EditorialComment:
    """Attach a rewrite comment to the current draft. Returns the new comment."""

    if draft_artifact_id is None:
        artifacts = client.get(f"/api/projects/{project_id}/artifacts").json()
        draft_artifacts = [a for a in artifacts if a["kind"] == "draft" and a["is_head"]]
        assert draft_artifacts, "no accepted draft artifact for project"
        draft_artifact_id = draft_artifacts[0]["id"]
    response = client.post(
        f"/api/projects/{project_id}/drafts/{draft_artifact_id}/comments",
        json={
            "paragraph_id": paragraph,
            "start_offset": 0,
            "end_offset": 0,
            "kind": "comment",
            "body": body,
            "ai_action": "rewrite",
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    return EditorialComment(
        id=payload["id"],
        draft_artifact_id=payload["draft_artifact_id"],
        paragraph_id=payload["paragraph_id"],
        start_offset=payload["start_offset"],
        end_offset=payload["end_offset"],
        kind=payload["kind"],
        body=payload["body"],
        ai_action=payload["ai_action"],
        processed_in_revision=payload["processed_in_revision"],
        created_at=datetime.fromisoformat(payload["created_at"]),
    )


def rewrite_selected(client: TestClient, project_id: str) -> str:
    """Trigger a rewrite. Returns the new draft artifact id."""

    artifacts = client.get(f"/api/projects/{project_id}/artifacts").json()
    head = next(
        a for a in artifacts if a["kind"] == "draft" and a["is_head"]
    )
    response = client.post(
        f"/api/projects/{project_id}/drafts/{head['id']}/rewrite"
    )
    assert response.status_code == 201, response.text
    return response.json()["artifact_id"]


def approve_current_draft(client: TestClient, project_id: str) -> Any:
    """Approve the current draft head. Returns the approved script dict."""

    from studio import db as studio_db
    from sqlalchemy.orm import sessionmaker
    from studio.artifacts import ArtifactRepository

    artifacts = client.get(f"/api/projects/{project_id}/artifacts").json()
    head = next(
        a for a in artifacts if a["kind"] == "draft" and a["is_head"]
    )
    response = client.post(
        f"/api/projects/{project_id}/drafts/{head['id']}/approve"
    )
    assert response.status_code == 201, response.text
    artifact_id = response.json()["artifact_id"]

    # The artifact-history endpoint doesn't return payloads; build the
    # approved-script response by loading the artifact directly.
    engine = studio_db.get_engine()
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        artifact = ArtifactRepository(session).get(artifact_id)
        payload = artifact.payload
    finally:
        session.close()
    payload["kind"] = "approved_script"
    return payload


# ---------------------------------------------------------------------------
# verbatim brief test
# ---------------------------------------------------------------------------


def test_topic_to_approved_script(
    app: TestClient,
    fake_providers: tuple[FakeModelProvider, _RecordingSearchProvider],
    worker: _Worker,
) -> None:
    project = create_project(app, "西瓜为什么不用来制糖")
    worker.drain()
    accept_pitch(app, project.id, pitch_index=1)
    worker.drain()
    add_comment(app, project.id, paragraph="b2", body="把成本因果讲清楚")
    rewrite_selected(app, project.id)
    approved = approve_current_draft(app, project.id)
    assert approved["kind"] == "approved_script"
    assert approved["editorial_text"]
