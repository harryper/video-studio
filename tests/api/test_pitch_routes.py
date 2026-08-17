"""Pitch stage HTTP routes.

Three routes make the pitch stage a human gate:

* ``POST /api/projects/{id}/pitches/generate`` enqueues the background job
  (the worker loop itself is Task 14) with the diagnosis + research artifacts
  as its inputs.
* ``GET  /api/projects/{id}/pitches`` returns the current pitch set.
* ``POST /api/projects/{id}/pitches/{pitch_id}/accept`` records the editor's
  choice as a new artifact revision, points the head at it, and queues
  narrative planning.

Everything here runs against the per-test SQLite database from
``tests/conftest.py``; no model or search provider is called.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from studio.api.app import create_app
from studio.artifacts import ArtifactRepository
from studio.jobs import Stage
from studio.models import Project, StageJob
from studio.schemas import StoryPitch, StoryPitchSet


@pytest.fixture
def client(isolated_database: str) -> TestClient:
    return TestClient(create_app())


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
        created_at=datetime(2026, 8, 17, tzinfo=UTC),
    )


@pytest.fixture
def seeded(
    session: Session, project: Project, pitch_set: StoryPitchSet
) -> dict[str, str]:
    """Diagnosis + research + pitches artifacts, each accepted as head."""

    repo = ArtifactRepository(session)
    ids: dict[str, str] = {}
    for kind, payload in (
        ("diagnosis", {"core_question": "为什么海水是咸的？"}),
        ("research", {"mechanisms": ["风化"]}),
        ("pitches", pitch_set.model_dump(mode="json")),
    ):
        artifact = repo.create(project.id, kind, payload)
        repo.accept(project.id, artifact.id)
        ids[kind] = artifact.id
    session.commit()
    return ids


def _jobs(session: Session, stage: Stage) -> list[StageJob]:
    session.expire_all()
    return [j for j in session.query(StageJob).all() if j.stage == stage.value]


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


def test_generate_enqueues_pitches_job_with_input_artifacts(
    client: TestClient, session: Session, project: Project, seeded: dict[str, str]
) -> None:
    response = client.post(f"/api/projects/{project.id}/pitches/generate")

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    jobs = _jobs(session, Stage.PITCHES)
    assert [j.id for j in jobs] == [job_id]
    assert set(jobs[0].input_artifact_ids) == {seeded["diagnosis"], seeded["research"]}


def test_generate_requires_diagnosis_and_research(
    client: TestClient, project: Project
) -> None:
    response = client.post(f"/api/projects/{project.id}/pitches/generate")
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def test_get_pitches_returns_current_set(
    client: TestClient, project: Project, pitch_set: StoryPitchSet, seeded: dict[str, str]
) -> None:
    response = client.get(f"/api/projects/{project.id}/pitches")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == pitch_set.id
    assert [p["id"] for p in body["pitches"]] == [p.id for p in pitch_set.pitches]


def test_get_pitches_404_when_absent(client: TestClient, project: Project) -> None:
    assert client.get(f"/api/projects/{project.id}/pitches").status_code == 404


# ---------------------------------------------------------------------------
# accept
# ---------------------------------------------------------------------------


def test_accept_writes_artifact_and_enqueues_narrative(
    client: TestClient,
    session: Session,
    project: Project,
    pitch_set: StoryPitchSet,
    seeded: dict[str, str],
) -> None:
    chosen = pitch_set.pitches[1]
    response = client.post(f"/api/projects/{project.id}/pitches/{chosen.id}/accept")

    assert response.status_code == 201
    body = response.json()
    artifact_id = body["artifact_id"]

    repo = ArtifactRepository(session)
    session.expire_all()
    artifact = repo.get(artifact_id)
    assert artifact is not None
    assert artifact.kind == "pitches"
    assert artifact.payload == {"selected_pitch_id": chosen.id, "edited_pitch": None}
    # accept() moved the head pointer onto the new revision.
    assert repo.current(project.id, "pitches").id == artifact_id

    jobs = _jobs(session, Stage.NARRATIVE)
    assert [j.id for j in jobs] == [body["job_id"]]
    assert jobs[0].input_artifact_ids == [artifact_id]


def test_accept_stores_edited_pitch(
    client: TestClient,
    session: Session,
    project: Project,
    pitch_set: StoryPitchSet,
    seeded: dict[str, str],
) -> None:
    chosen = pitch_set.pitches[0]
    edited = chosen.model_copy(update={"payoff": "编辑改过的回报"})

    response = client.post(
        f"/api/projects/{project.id}/pitches/{chosen.id}/accept",
        json={"edited_pitch": edited.model_dump(mode="json")},
    )

    assert response.status_code == 201
    session.expire_all()
    artifact = ArtifactRepository(session).get(response.json()["artifact_id"])
    assert artifact.payload["edited_pitch"]["payoff"] == "编辑改过的回报"


def test_accept_rejects_unknown_pitch_id(
    client: TestClient, project: Project, seeded: dict[str, str]
) -> None:
    response = client.post(f"/api/projects/{project.id}/pitches/no-such-id/accept")
    assert response.status_code == 404


def test_accept_rejects_edited_pitch_id_mismatch(
    client: TestClient, project: Project, pitch_set: StoryPitchSet, seeded: dict[str, str]
) -> None:
    chosen = pitch_set.pitches[0]
    other = pitch_set.pitches[2]

    response = client.post(
        f"/api/projects/{project.id}/pitches/{chosen.id}/accept",
        json={"edited_pitch": other.model_dump(mode="json")},
    )
    assert response.status_code == 400
