"""Project lifecycle, reopen handshake, retry/cancel, and auth/error-envelope routes.

Every test here runs offline against the per-test SQLite database. The
``authed_client`` fixture handles ``POST /api/session`` and exposes the CSRF
token as ``authed_client._csrf_token``; mutating requests must carry the
``X-CSRF-Token`` header.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from studio.artifacts import ArtifactRepository
from studio.jobs import LeaseQueue, Stage
from studio.models import Project, StageJob
from studio.schemas import (
    DraftParagraph,
    DraftRevision,
    NarrativeBeat,
    NarrativePlan,
    StoryPitch,
    StoryPitchSet,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(authed_client: TestClient) -> TestClient:
    """Alias so this module reads like the rest of the API tests."""
    return authed_client


def _pitch(n: int) -> StoryPitch:
    return StoryPitch(
        id=str(uuid4()),
        investigation_question=f"问题 {n}",
        opening_scene=f"开场 {n}",
        evidence_path=f"证据路径 {n}",
        payoff=f"回报 {n}",
        why_it_works=f"为什么成立 {n}",
        estimated_duration_sec=180 + n,
        risks=[],
    )


@pytest.fixture
def pitch_set() -> StoryPitchSet:
    return StoryPitchSet(
        id=str(uuid4()),
        pitches=[_pitch(1), _pitch(2), _pitch(3)],
        created_at=datetime(2026, 8, 18, tzinfo=UTC),
    )


@pytest.fixture
def drafted_project(
    session: Session, project: Project, pitch_set: StoryPitchSet
) -> dict[str, Any]:
    """Project with diagnosis+research+pitches+accepted_pitch+narrative+draft.

    Returns the seeded ``Project`` plus a dict of artifact ids keyed by kind so
    tests can refer to specific revisions.
    """

    repo = ArtifactRepository(session)
    ids: dict[str, str] = {}

    diagnosis = repo.create(
        project.id, "diagnosis", {"core_question": "为什么海水是咸的？"}
    )
    repo.accept(project.id, diagnosis.id)
    ids["diagnosis"] = diagnosis.id

    research = repo.create(project.id, "research", {"mechanisms": ["风化"]})
    repo.accept(project.id, research.id)
    ids["research"] = research.id

    pitch_artifact = repo.create(
        project.id, "pitches", pitch_set.model_dump(mode="json")
    )
    repo.accept(project.id, pitch_artifact.id)
    ids["pitch_set"] = pitch_artifact.id

    accepted = repo.create(
        project.id,
        "pitches",
        {
            "payload_kind": "accepted_pitch",
            "selected_pitch_id": pitch_set.pitches[0].id,
            "edited_pitch": None,
        },
        parent_id=pitch_artifact.id,
    )
    repo.accept(project.id, accepted.id)
    ids["accepted_pitch"] = accepted.id

    plan = NarrativePlan(
        id="plan-1",
        pitch_id=pitch_set.pitches[0].id,
        beats=[
            NarrativeBeat(
                id="p1",
                purpose="setup",
                fact_card_ids=[],
                new_information="...",
                next_question="...",
                withheld_information="",
            ),
            NarrativeBeat(
                id="p2",
                purpose="payoff",
                fact_card_ids=[],
                new_information="...",
                next_question="",
                withheld_information="",
            ),
        ],
        created_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    narrative = repo.create(
        project.id, "narrative", plan.model_dump(mode="json")
    )
    repo.accept(project.id, narrative.id)
    ids["narrative"] = narrative.id

    draft = DraftRevision(
        id="draft-1",
        narrative_plan_id="plan-1",
        paragraphs=[
            DraftParagraph(id="p1", text="第一段。"),
            DraftParagraph(id="p2", text="第二段。"),
        ],
        editorial_text="第一段。\n\n第二段。",
        change_source="initial",
        created_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    draft_artifact = repo.create(
        project.id, "draft", draft.model_dump(mode="json")
    )
    repo.accept(project.id, draft_artifact.id)
    ids["draft"] = draft_artifact.id
    session.commit()
    return {"project": project, "ids": ids}


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_project_queues_diagnosis(client: TestClient) -> None:
    response = client.post(
        "/api/projects",
        json={"title": "测试", "topic": "糖为什么是战略物资"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["stage"] == "diagnosis_queued"
    assert body["job_id"]


def test_create_project_requires_title_and_topic(client: TestClient) -> None:
    assert (
        client.post("/api/projects", json={"topic": "台风"}).status_code == 422
    )
    assert (
        client.post("/api/projects", json={"title": "t"}).status_code == 422
    )


# ---------------------------------------------------------------------------
# list / filter
# ---------------------------------------------------------------------------


def test_project_list_returns_recent_first(
    client: TestClient, session: Session
) -> None:
    """Listing is by ``updated_at desc``; the freshly-created project wins."""

    older = Project(id="older", title="旧", topic="旧主题", updated_at=datetime(2026, 1, 1, tzinfo=UTC))
    session.add(older)
    session.commit()

    response = client.post(
        "/api/projects", json={"title": "新", "topic": "新主题"}
    )
    assert response.status_code == 201
    new_id = response.json()["id"]

    listing = client.get("/api/projects")
    assert listing.status_code == 200
    body = listing.json()
    assert [p["id"] for p in body] == [new_id, "older"]


def test_project_list_filters_by_latest_stage(
    client: TestClient, session: Session, project: Project
) -> None:
    """``?stage=`` matches the most recent job's stage for each project."""

    LeaseQueue(session).enqueue(project.id, Stage.RESEARCH, [])
    session.commit()

    other = Project(id="other", title="Other", topic="其他")
    session.add(other)
    session.commit()
    LeaseQueue(session).enqueue("other", Stage.DRAFT, [])
    session.commit()

    draft_response = client.get("/api/projects", params={"stage": "draft"})
    assert draft_response.status_code == 200
    assert [p["id"] for p in draft_response.json()] == ["other"]

    research_response = client.get("/api/projects", params={"stage": "research"})
    assert research_response.status_code == 200
    assert [p["id"] for p in research_response.json()] == ["proj-1"]


def test_project_list_filters_by_latest_job_status(
    client: TestClient, session: Session, project: Project
) -> None:
    LeaseQueue(session).enqueue(project.id, Stage.DRAFT, [])
    session.commit()

    response = client.get("/api/projects", params={"status": "queued"})
    assert response.status_code == 200
    assert any(p["id"] == "proj-1" for p in response.json())


# ---------------------------------------------------------------------------
# artifact history
# ---------------------------------------------------------------------------


def test_artifact_history_returns_revisions_in_order(
    client: TestClient, session: Session, project: Project
) -> None:
    repo = ArtifactRepository(session)
    first = repo.create(project.id, "diagnosis", {"core_question": "first"})
    second = repo.create(
        project.id, "diagnosis", {"core_question": "second"}, parent_id=first.id
    )
    third = repo.create(
        project.id, "diagnosis", {"core_question": "third"}, parent_id=second.id
    )
    repo.accept(project.id, third.id)
    session.commit()

    response = client.get(f"/api/projects/{project.id}/artifacts")
    assert response.status_code == 200
    body = response.json()
    diagnosis_rows = [a for a in body if a["kind"] == "diagnosis"]
    assert [a["id"] for a in diagnosis_rows] == [first.id, second.id, third.id]
    assert [a["is_head"] for a in diagnosis_rows] == [False, False, True]


# ---------------------------------------------------------------------------
# pitch / regenerate / reopen handshake
# ---------------------------------------------------------------------------


def test_pitch_regenerate_without_set_enqueues_job(
    client: TestClient, session: Session, project: Project
) -> None:
    repo = ArtifactRepository(session)
    d = repo.create(project.id, "diagnosis", {"core_question": "q"})
    repo.accept(project.id, d.id)
    r = repo.create(project.id, "research", {"mechanisms": ["x"]})
    repo.accept(project.id, r.id)
    session.commit()

    response = client.post(f"/api/projects/{project.id}/pitch/regenerate")
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    session.expire_all()
    job = session.get(StageJob, job_id)
    assert job is not None
    assert job.stage == Stage.PITCHES.value


def test_pitch_regenerate_with_existing_set_reports_invalidates(
    client: TestClient, drafted_project: dict[str, Any]
) -> None:
    project = drafted_project["project"]
    response = client.post(f"/api/projects/{project.id}/pitch/regenerate")
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "confirmation_required"
    assert "narrative" in body["invalidates"]
    assert "draft" in body["invalidates"]


def test_upstream_change_reports_invalidated_artifacts(
    client: TestClient, drafted_project: dict[str, Any]
) -> None:
    project = drafted_project["project"]
    response = client.post(f"/api/projects/{project.id}/pitch/reopen")
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "confirmation_required"
    assert set(body["invalidates"]) == {"narrative", "draft"}


def test_reopen_confirmation_mismatch_returns_409(
    client: TestClient, drafted_project: dict[str, Any]
) -> None:
    project = drafted_project["project"]
    response = client.post(
        f"/api/projects/{project.id}/pitch/reopen",
        headers={"X-Confirm-Invalidates": "narrative"},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "confirmation_mismatch"


def test_reopen_with_correct_confirmation_clears_downstream(
    client: TestClient, session: Session, drafted_project: dict[str, Any]
) -> None:
    project = drafted_project["project"]
    ids = drafted_project["ids"]
    response = client.post(
        f"/api/projects/{project.id}/pitch/reopen",
        headers={"X-Confirm-Invalidates": "narrative,draft"},
    )
    assert response.status_code == 200

    repo = ArtifactRepository(session)
    session.expire_all()
    assert repo.current(project.id, "narrative") is None
    assert repo.current(project.id, "draft") is None
    # The pitch set head is left intact — only downstream heads clear.
    assert repo.current(project.id, "pitches").id == ids["accepted_pitch"]


def test_reopen_without_downstream_is_noop(
    client: TestClient, session: Session, project: Project, pitch_set: StoryPitchSet
) -> None:
    repo = ArtifactRepository(session)
    pitch = repo.create(project.id, "pitches", pitch_set.model_dump(mode="json"))
    repo.accept(project.id, pitch.id)
    session.commit()

    response = client.post(f"/api/projects/{project.id}/pitch/reopen")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# retry / cancel
# ---------------------------------------------------------------------------


def test_retry_only_valid_on_failed_job(
    client: TestClient, session: Session, project: Project
) -> None:
    queue = LeaseQueue(session)
    failed = queue.enqueue(project.id, Stage.DRAFT, [])
    session.commit()

    response = client.post(
        f"/api/projects/{project.id}/jobs/{failed.id}/retry"
    )
    assert response.status_code == 409

    # Now mark it failed and retry should succeed.
    failed.status = "failed"
    failed.error_code = "x"
    failed.error_message = "y"
    session.commit()

    response = client.post(
        f"/api/projects/{project.id}/jobs/{failed.id}/retry"
    )
    assert response.status_code == 200
    session.expire_all()
    refreshed = session.get(StageJob, failed.id)
    assert refreshed.status == "queued"
    assert refreshed.error_code is None
    assert refreshed.attempts == 0  # attempts starts at 0 on enqueue; never bumped


def test_cancel_only_valid_on_queued_job(
    client: TestClient, session: Session, project: Project
) -> None:
    queue = LeaseQueue(session)
    queued = queue.enqueue(project.id, Stage.DRAFT, [])
    running = queue.enqueue(project.id, Stage.RESEARCH, [])
    running.status = "running"
    session.commit()

    response = client.post(
        f"/api/projects/{project.id}/jobs/{running.id}/cancel"
    )
    assert response.status_code == 409

    response = client.post(
        f"/api/projects/{project.id}/jobs/{queued.id}/cancel"
    )
    assert response.status_code == 200
    session.expire_all()
    assert session.get(StageJob, queued.id).status == "cancelled"


def test_retry_requires_job_to_belong_to_project(
    client: TestClient, session: Session, project: Project
) -> None:
    queue = LeaseQueue(session)
    other = Project(id="proj-other", title="other", topic="t")
    session.add(other)
    session.commit()
    failed = queue.enqueue("proj-other", Stage.DRAFT, [])
    failed.status = "failed"
    session.commit()

    response = client.post(
        f"/api/projects/{project.id}/jobs/{failed.id}/retry"
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# auth & error envelope
# ---------------------------------------------------------------------------


def test_mutating_api_requires_session_and_csrf(
    monkeypatch: pytest.MonkeyPatch, isolated_database: str
) -> None:
    """No session -> 401; session without CSRF -> 403; session+CSRF -> success."""

    from fastapi.testclient import TestClient

    from studio.api.app import create_app

    monkeypatch.setenv("STUDIO_CONTENT_STUDIO_PASSWORD", "test-password")
    client = TestClient(create_app())

    # 401: no session.
    no_session = client.post(
        "/api/projects", json={"title": "t", "topic": "台风"}
    )
    assert no_session.status_code == 401

    login = client.post("/api/session", json={"password": "test-password"})
    assert login.status_code == 200
    csrf = login.json()["csrf_token"]

    # 403: session cookie is present but no CSRF header.
    no_csrf = client.post(
        "/api/projects", json={"title": "t", "topic": "台风"}
    )
    assert no_csrf.status_code == 403

    # 201: session + CSRF.
    ok = client.post(
        "/api/projects",
        json={"title": "t", "topic": "台风"},
        headers={"X-CSRF-Token": csrf},
    )
    assert ok.status_code == 201


def test_get_requires_session(
    monkeypatch: pytest.MonkeyPatch, isolated_database: str
) -> None:
    from fastapi.testclient import TestClient

    from studio.api.app import create_app

    monkeypatch.setenv("STUDIO_CONTENT_STUDIO_PASSWORD", "test-password")
    client = TestClient(create_app())
    # Session login is required even for GET endpoints other than /api/health.
    assert client.get("/api/projects").status_code == 401


def test_login_rejects_wrong_password(
    monkeypatch: pytest.MonkeyPatch, isolated_database: str
) -> None:
    from fastapi.testclient import TestClient

    from studio.api.app import create_app

    monkeypatch.setenv("STUDIO_CONTENT_STUDIO_PASSWORD", "test-password")
    client = TestClient(create_app())
    response = client.post("/api/session", json={"password": "wrong"})
    assert response.status_code == 401


def test_error_envelope_shape(
    client: TestClient, drafted_project: dict[str, Any]
) -> None:
    """Every non-2xx response uses the ``{code, message, details}`` envelope."""

    project = drafted_project["project"]
    response = client.post(f"/api/projects/{project.id}/pitch/reopen")
    assert response.status_code == 409
    body = response.json()
    assert set(body.keys()) >= {"code", "message", "invalidates"}
    assert body["code"] == "confirmation_required"


# ---------------------------------------------------------------------------
# project detail
# ---------------------------------------------------------------------------


def test_get_project_returns_full_record(client: TestClient) -> None:
    response = client.post(
        "/api/projects", json={"title": "测试", "topic": "台风"}
    )
    assert response.status_code == 201
    pid = response.json()["id"]

    response = client.get(f"/api/projects/{pid}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == pid
    assert body["title"] == "测试"
    assert body["topic"] == "台风"
    assert "created_at" in body
    assert "updated_at" in body


def test_get_unknown_project_404(client: TestClient) -> None:
    response = client.get("/api/projects/nope")
    assert response.status_code == 404