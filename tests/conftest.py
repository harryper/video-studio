"""Pytest configuration: pin every test run to a fresh, isolated database.

This must be imported before any application code touches ``Settings`` so that
``Settings.database_url`` points at the per-test SQLite file rather than the
real development database.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from studio import db as studio_db
from studio.artifacts import ArtifactRepository
from studio.config import Settings
from studio.models import Base, Project


@pytest.fixture(autouse=True)
def isolated_database(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[str, None, None]:
    """Point the engine at ``tmp_path/test.db`` before the app imports ``Settings``."""

    db_path = tmp_path / "test.db"
    url = f"sqlite+pysqlite:///{db_path}"

    # pydantic-settings reads env vars prefixed with ``STUDIO_`` when a new
    # ``Settings()`` is instantiated. Force both env vars so any code path
    # that constructs ``Settings()`` ends up on the per-test database.
    monkeypatch.setenv("STUDIO_DATABASE_URL", url)
    monkeypatch.setenv("CONTENT_STUDIO_DB", str(db_path))

    # Reset any cached engine from a previous test so the new URL takes effect.
    studio_db.reset_engine()
    engine = studio_db.get_engine(url)
    Base.metadata.create_all(engine)

    yield str(db_path)

    studio_db.reset_engine()
    if db_path.exists():
        db_path.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()


@pytest.fixture
def session(isolated_database: str) -> Generator[Session, None, None]:
    engine = studio_db.get_engine()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture
def repo(session: Session) -> ArtifactRepository:
    return ArtifactRepository(session)


@pytest.fixture
def project(session: Session) -> Project:
    proj = Project(id="proj-1", title="Test Project")
    session.add(proj)
    session.commit()
    session.refresh(proj)
    return proj