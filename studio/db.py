"""Database engine and per-connection PRAGMAs for Content Studio.

The engine is built lazily from ``Settings.database_url`` and cached so that
tests can monkey-patch the URL and request a fresh engine via
``reset_engine()``. Every new SQLite connection gets ``foreign_keys=ON``,
``busy_timeout=5000`` and, for file-backed URLs, ``journal_mode=WAL``.
"""

from __future__ import annotations

import os
from typing import Optional

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine.interfaces import DBAPIConnection

from studio.config import Settings


_engine: Optional[Engine] = None


def resolve_database_url(explicit: Optional[str] = None) -> str:
    """Pick the database URL in priority order: arg > env > settings."""

    if explicit:
        return explicit
    env = os.environ.get("CONTENT_STUDIO_DB")
    if env:
        return f"sqlite+pysqlite:///{env}"
    return Settings().database_url


def _is_in_memory(url: str) -> bool:
    """True iff this is an in-memory SQLite URL (matched against the path)."""

    return "/:memory:" in url or url.endswith(":memory:")


def _attach_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: DBAPIConnection, _record) -> None:  # noqa: ARG001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        if not _is_in_memory(str(engine.url)):
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def get_engine(url: Optional[str] = None) -> Engine:
    """Return (and cache) the global SQLAlchemy engine for Content Studio."""

    global _engine
    if _engine is not None and url is None:
        return _engine

    target = url or resolve_database_url()
    new_engine = create_engine(
        target,
        future=True,
        connect_args={"check_same_thread": False},
    )
    _attach_pragmas(new_engine)
    if url is None:
        _engine = new_engine
    return new_engine


def reset_engine() -> None:
    """Dispose and clear the cached engine — tests use this to isolate state."""

    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


__all__ = ["get_engine", "reset_engine", "resolve_database_url"]