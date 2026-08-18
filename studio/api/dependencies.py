"""Shared FastAPI dependencies for Content Studio.

Centralising ``get_session`` here means the SSE generator, the projects
router, and the pre-existing stages / comments routers all draw from the same
session-factory configuration. New routes should depend on these helpers
rather than building their own engine.
"""

from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from studio import db as studio_db
from studio.config import Settings


def get_engine() -> Engine:
    """Return the cached engine — see :func:`studio.db.get_engine`."""

    return studio_db.get_engine()


def get_settings() -> Settings:
    """Read a fresh ``Settings`` instance on every request.

    pydantic-settings reads env vars at instantiation, so deferring the call
    lets tests set ``STUDIO_*`` env vars via ``monkeypatch`` before the first
    request lands.
    """

    return Settings()


def get_session_factory(
    settings: Settings = Depends(get_settings),  # noqa: B008
    engine: Engine = Depends(get_engine),  # noqa: B008
) -> sessionmaker[Session]:
    """Build a per-request ``sessionmaker`` bound to the cached engine."""

    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session(
    factory: sessionmaker[Session] = Depends(get_session_factory),  # noqa: B008
) -> Generator[Session, None, None]:
    """Yield a request-scoped SQLAlchemy ``Session`` and close it on exit."""

    session = factory()
    try:
        yield session
    finally:
        session.close()