"""Review stage HTTP routes:

* ``POST/GET /api/projects/{id}/drafts/{draft_artifact_id}/comments``
* ``POST /api/projects/{id}/drafts/{draft_artifact_id}/rewrite``
* ``POST /api/projects/{id}/drafts/{draft_artifact_id}/approve``

All tests run offline against the per-test SQLite DB and a
``FakeModelProvider`` registered via ``stages.set_default_provider``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy.orm import Session

from studio.api.app import create_app
from studio.api.routes.stages import set_default_provider
from studio.artifacts import ArtifactRepository
from studio.models import EditorialComment as OrmEditorialComment
from studio.models import Project
from studio.providers.fake import FakeModelProvider
from studio.schemas import (
    DraftParagraph,
    DraftRevision,
    FactCard,
    NarrativeBeat,
    NarrativePlan,
    ResearchPacket,
    SourceDocument,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _RewriteParagraph(BaseModel):
    paragraph_id: str
    text: str


class _RewriteOutput(BaseModel):
    paragraphs: list[_RewriteParagraph]


def _draft() -> DraftRevision:
    return DraftRevision(
        id=str(uuid4()),
        narrative_plan_id="plan-1",
        paragraphs=[
            DraftParagraph(id="p1", text="第一段开场内容。"),
            DraftParagraph(id="p2", text="第二段正文，真正的原因在供应链。"),
            DraftParagraph(id="p3", text="第三段结尾。"),
        ],
        editorial_text="第一段开场内容。\n\n第二段正文，真正的原因在供应链。\n\n第三段结尾。",
        change_source="initial",
        created_at=datetime(2026, 8, 18, tzinfo=UTC),
    )


def _research() -> ResearchPacket:
    doc = SourceDocument(
        title="x", url="https://example.com/x", snippet="x", publisher="x"
    )
    return ResearchPacket(
        mechanisms=["m"],
        fact_cards=[
            FactCard(
                claim="c1",
                narrative_value="nv",
                confidence=0.9,
                risk="number",
                sources=[doc],
                verification_status="verified",
                payoff_critical=True,
            )
        ],
        people_events=[],
        concrete_scenes=[],
        visual_details=[],
        uncertainties=[],
        sources=[doc],
    )


def _plan() -> NarrativePlan:
    return NarrativePlan(
        id="plan-1",
        pitch_id="pitch-1",
        beats=[
            NarrativeBeat(
                id="p1",
                purpose="setup",
                fact_card_ids=["c1"],
                new_information="...",
                next_question="...",
                withheld_information="",
            ),
            NarrativeBeat(
                id="p2",
                purpose="payoff",
                fact_card_ids=["c1"],
                new_information="...",
                next_question="",
                withheld_information="",
            ),
        ],
        created_at=datetime(2026, 8, 18, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(isolated_database: str) -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def rewrite_provider() -> FakeModelProvider:
    """Default rewrite: p2 rewritten, protected span preserved."""

    provider = FakeModelProvider()
    provider.queue(
        "rewrite",
        _RewriteOutput(
            paragraphs=[
                _RewriteParagraph(
                    paragraph_id="p2",
                    text="重写后的第二段，真正的原因在供应链依然成立。",
                ),
            ]
        ),
    )
    set_default_provider(provider)
    yield provider
    set_default_provider(None)


@pytest.fixture
def seeded(
    session: Session, project, rewrite_provider: FakeModelProvider
) -> dict[str, str]:
    """Research + narrative + draft artifacts, each accepted as head."""

    repo = ArtifactRepository(session)
    ids: dict[str, str] = {}
    for kind, payload in (
        ("research", _research().model_dump(mode="json")),
        ("narrative", _plan().model_dump(mode="json")),
        ("draft", _draft().model_dump(mode="json")),
    ):
        artifact = repo.create(project.id, kind, payload)
        repo.accept(project.id, artifact.id)
        ids[kind] = artifact.id
    session.commit()
    return ids


# ---------------------------------------------------------------------------
# comments
# ---------------------------------------------------------------------------


def test_post_comment_creates_row(
    client: TestClient, session: Session, project, seeded: dict[str, str]
) -> None:
    response = client.post(
        f"/api/projects/{project.id}/drafts/{seeded['draft']}/comments",
        json={
            "paragraph_id": "p2",
            "start_offset": 0,
            "end_offset": 4,
            "kind": "comment",
            "body": "人工反馈",
            "ai_action": "rewrite",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["body"] == "人工反馈"
    assert body["ai_action"] == "rewrite"
    assert body["draft_artifact_id"] == seeded["draft"]

    rows = (
        session.query(OrmEditorialComment)
        .filter_by(draft_artifact_id=seeded["draft"])
        .all()
    )
    assert len(rows) == 1
    assert rows[0].body == "人工反馈"


def test_post_comment_rejects_bad_offsets(
    client: TestClient, project, seeded: dict[str, str]
) -> None:
    response = client.post(
        f"/api/projects/{project.id}/drafts/{seeded['draft']}/comments",
        json={
            "paragraph_id": "p2",
            "start_offset": 5,
            "end_offset": 5,  # end == start, no range
            "kind": "comment",
            "body": "人工反馈",
            "ai_action": "rewrite",
        },
    )
    assert response.status_code == 400


def test_post_comment_rejects_unknown_paragraph(
    client: TestClient, project, seeded: dict[str, str]
) -> None:
    response = client.post(
        f"/api/projects/{project.id}/drafts/{seeded['draft']}/comments",
        json={
            "paragraph_id": "nope",
            "start_offset": 0,
            "end_offset": 0,
            "kind": "comment",
            "body": "人工反馈",
            "ai_action": "rewrite",
        },
    )
    assert response.status_code == 404


def test_get_comments_returns_list(
    client: TestClient, project, seeded: dict[str, str]
) -> None:
    for body in ("第一条", "第二条"):
        client.post(
            f"/api/projects/{project.id}/drafts/{seeded['draft']}/comments",
            json={
                "paragraph_id": "p2",
                "start_offset": 0,
                "end_offset": 0,
                "kind": "comment",
                "body": body,
                "ai_action": "rewrite",
            },
        )

    response = client.get(
        f"/api/projects/{project.id}/drafts/{seeded['draft']}/comments"
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert {c["body"] for c in body} == {"第一条", "第二条"}


# ---------------------------------------------------------------------------
# rewrite
# ---------------------------------------------------------------------------


def test_rewrite_creates_new_draft_revision(
    client: TestClient,
    session: Session,
    project,
    seeded: dict[str, str],
    rewrite_provider: FakeModelProvider,
) -> None:
    response = client.post(
        f"/api/projects/{project.id}/drafts/{seeded['draft']}/rewrite"
    )
    assert response.status_code == 201
    body = response.json()
    new_id = body["artifact_id"]
    assert new_id != seeded["draft"]

    repo = ArtifactRepository(session)
    artifact = repo.get(new_id)
    assert artifact is not None
    assert artifact.kind == "draft"
    assert artifact.parent_id == seeded["draft"]
    assert artifact.payload["change_source"] == "rewrite"
    assert "真正的原因在供应链" in artifact.payload["editorial_text"]


def test_rewrite_uses_stored_comments(
    client: TestClient,
    session: Session,
    project,
    seeded: dict[str, str],
    rewrite_provider: FakeModelProvider,
) -> None:
    """The rewrite consumes all stored comments for the draft by default."""

    client.post(
        f"/api/projects/{project.id}/drafts/{seeded['draft']}/comments",
        json={
            "paragraph_id": "p2",
            "start_offset": 0,
            "end_offset": 0,
            "kind": "comment",
            "body": "重写 p2",
            "ai_action": "rewrite",
        },
    )
    response = client.post(
        f"/api/projects/{project.id}/drafts/{seeded['draft']}/rewrite"
    )
    assert response.status_code == 201


def test_rewrite_prompt_includes_paragraph_fact_card_ids(
    client: TestClient,
    session: Session,
    project,
    seeded: dict[str, str],
) -> None:
    """The rewrite prompt surfaces each paragraph's fact_card_ids.

    Without this the rewriter has no way to know which facts the
    paragraph was supposed to ground itself in — a "rewrite but keep
    the steel tariff figure" comment can't be honoured if the model
    doesn't know which fact card carries that figure.
    """

    class _RecordingProvider(FakeModelProvider):
        def __init__(self) -> None:
            super().__init__()
            self.prompts: list[str] = []

        def generate(self, schema, system, prompt, *, operation):
            self.prompts.append(prompt)
            return super().generate(schema, system, prompt, operation=operation)

    provider = _RecordingProvider()
    provider.queue(
        "rewrite",
        _RewriteOutput(
            paragraphs=[
                _RewriteParagraph(
                    paragraph_id="p2",
                    text="重写后的第二段。",
                )
            ]
        ),
    )
    set_default_provider(provider)
    try:
        client.post(
            f"/api/projects/{project.id}/drafts/{seeded['draft']}/comments",
            json={
                "paragraph_id": "p2",
                "start_offset": 0,
                "end_offset": 0,
                "kind": "comment",
                "body": "改写",
                "ai_action": "rewrite",
            },
        )
        response = client.post(
            f"/api/projects/{project.id}/drafts/{seeded['draft']}/rewrite"
        )
        assert response.status_code == 201
    finally:
        set_default_provider(None)

    assert any("关联事实卡片" in p for p in provider.prompts), (
        f"rewrite prompt must surface paragraph fact_card_ids; got: {provider.prompts}"
    )
    # The narrative plan in `seeded` lists fact_card_ids=["c1"] for both p1 and p2,
    # so the rewritten p2 prompt must mention c1.
    assert any("c1" in p for p in provider.prompts), (
        f"rewrite prompt must include the c1 fact id; got: {provider.prompts}"
    )


def test_rewrite_404_for_unknown_draft(
    client: TestClient, project, rewrite_provider: FakeModelProvider
) -> None:
    response = client.post(
        f"/api/projects/{project.id}/drafts/no-such-id/rewrite"
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


def test_approve_creates_approved_script_artifact(
    client: TestClient,
    session: Session,
    project,
    seeded: dict[str, str],
) -> None:
    response = client.post(
        f"/api/projects/{project.id}/drafts/{seeded['draft']}/approve"
    )
    assert response.status_code == 201
    artifact_id = response.json()["artifact_id"]

    repo = ArtifactRepository(session)
    artifact = repo.get(artifact_id)
    assert artifact is not None
    assert artifact.kind == "approved_script"
    assert artifact.payload["payload_kind"] == "approved_script"
    assert artifact.payload["draft_revision_id"] == seeded["draft"]
    assert artifact.payload["structure"] == ["p1", "p2", "p3"]
    assert "c1" in artifact.payload["fact_card_ids"]


def test_approve_moves_approved_script_head(
    client: TestClient,
    session: Session,
    project,
    seeded: dict[str, str],
) -> None:
    response = client.post(
        f"/api/projects/{project.id}/drafts/{seeded['draft']}/approve"
    )
    artifact_id = response.json()["artifact_id"]

    repo = ArtifactRepository(session)
    head = repo.current(project.id, "approved_script")
    assert head is not None
    assert head.id == artifact_id


def test_approve_marks_comments_processed(
    client: TestClient,
    session: Session,
    project,
    seeded: dict[str, str],
) -> None:
    client.post(
        f"/api/projects/{project.id}/drafts/{seeded['draft']}/comments",
        json={
            "paragraph_id": "p2",
            "start_offset": 0,
            "end_offset": 0,
            "kind": "comment",
            "body": "feedback",
            "ai_action": "rewrite",
        },
    )
    response = client.post(
        f"/api/projects/{project.id}/drafts/{seeded['draft']}/approve"
    )
    artifact_id = response.json()["artifact_id"]

    session.expire_all()
    rows = (
        session.query(OrmEditorialComment)
        .filter_by(draft_artifact_id=seeded["draft"])
        .all()
    )
    assert len(rows) == 1
    assert rows[0].processed_in_revision == artifact_id


def test_approve_refuses_when_newer_draft_exists(
    client: TestClient,
    session: Session,
    project,
    seeded: dict[str, str],
) -> None:
    """Approve must 409 when a newer draft revision exists."""

    repo = ArtifactRepository(session)
    newer = repo.create(project.id, "draft", _draft().model_dump(mode="json"))
    repo.accept(project.id, newer.id)
    session.commit()

    response = client.post(
        f"/api/projects/{project.id}/drafts/{seeded['draft']}/approve"
    )
    assert response.status_code == 409


def test_approve_404_for_unknown_draft(client: TestClient, project) -> None:
    response = client.post(
        f"/api/projects/{project.id}/drafts/no-such-id/approve"
    )
    assert response.status_code == 404


def test_approve_404_for_cross_project_draft(
    client: TestClient,
    session: Session,
    project,
    rewrite_provider: FakeModelProvider,
) -> None:
    """A draft from another project returns 404, not 400 — same as the comments route."""

    other = Project(id="proj-other", title="other")
    session.add(other)
    session.commit()
    other_repo = ArtifactRepository(session)
    other_draft = other_repo.create(
        other.id,
        "draft",
        _draft().model_dump(mode="json"),
    )
    session.commit()

    response = client.post(
        f"/api/projects/{project.id}/drafts/{other_draft.id}/approve"
    )
    assert response.status_code == 404