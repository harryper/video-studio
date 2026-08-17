"""Artifact repository tests: append-only revisions and the current pointer."""

from __future__ import annotations

import pytest

from studio.artifacts import ArtifactRepository
from studio.models import Artifact, ImmutabilityError, Project


def test_artifacts_are_append_only(repo: ArtifactRepository, project: Project) -> None:
    first = repo.create(project.id, "story_pitch_set", {"pitches": [1, 2, 3]})
    second = repo.create(
        project.id,
        "story_pitch_set",
        {"pitches": [4, 5, 6]},
        parent_id=first.id,
    )
    assert (first.revision, second.revision) == (1, 2)
    assert repo.get(first.id).payload == {"pitches": [1, 2, 3]}


def test_accept_sets_only_current_pointer(
    repo: ArtifactRepository, project: Project
) -> None:
    artifact = repo.create(project.id, "story_pitch_set", {"pitches": []})
    repo.accept(project.id, artifact.id)
    assert repo.current(project.id, "story_pitch_set").id == artifact.id


def test_accept_overwrites_previous_current_pointer(
    repo: ArtifactRepository, project: Project
) -> None:
    first = repo.create(project.id, "story_pitch_set", {"pitches": [1]})
    repo.accept(project.id, first.id)
    second = repo.create(project.id, "story_pitch_set", {"pitches": [2]})
    repo.accept(project.id, second.id)

    head = repo.current(project.id, "story_pitch_set")
    assert head is not None
    assert head.id == second.id
    # Earlier artifact remains queryable but is no longer the head.
    assert repo.get(first.id).id == first.id


def test_immutability_blocks_payload_update(
    repo: ArtifactRepository, project: Project
) -> None:
    artifact = repo.create(project.id, "story_pitch_set", {"pitches": [1]})

    with pytest.raises(ImmutabilityError):
        artifact.payload = {"pitches": [2]}


def test_before_update_backstop_blocks_payload_at_flush(
    repo: ArtifactRepository,
    project: Project,
    session,
) -> None:
    """The mapper-event backstop must trigger even when ``__setattr__`` is bypassed."""

    artifact = repo.create(project.id, "story_pitch_set", {"pitches": [1]})
    session.commit()
    session.refresh(artifact)
    assert artifact.payload == {"pitches": [1]}

    # Bypass the Python ``__setattr__`` guard so the mapper event is the only
    # line of defence left; ``session.flush()`` must raise.
    object.__setattr__(artifact, "payload", {"pitches": [999]})

    with pytest.raises(ImmutabilityError):
        session.flush()


def test_list_revisions_orders_newest_first(
    repo: ArtifactRepository, project: Project
) -> None:
    a = repo.create(project.id, "story_pitch_set", {"pitches": [1]})
    b = repo.create(project.id, "story_pitch_set", {"pitches": [2]})
    c = repo.create(project.id, "story_pitch_set", {"pitches": [3]})

    revisions: list[Artifact] = repo.list_revisions(project.id, "story_pitch_set")
    assert [r.revision for r in revisions] == [c.revision, b.revision, a.revision]