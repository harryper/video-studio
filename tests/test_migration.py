"""Migration smoke test: ``alembic upgrade head`` produces the expected schema."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


EXPECTED_TABLES = {
    "projects",
    "artifacts",
    "project_artifact_heads",
    "stage_jobs",
    "editorial_comments",
    "alembic_version",
}


def _is_unique(project_id_kind_revision_index: tuple) -> bool:
    """Return True if the index is unique on (project_id, kind, revision)."""

    # PRAGMA index_info returns rows of (seqno, cid, name).
    cols = {row[2] for row in project_id_kind_revision_index}
    return cols == {"project_id", "kind", "revision"}


def test_upgrade_creates_all_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "migration.db"
    os.environ["CONTENT_STUDIO_DB"] = str(db_path)
    # Alembic reads the ini from the project root regardless of cwd via absolute path.
    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    command.upgrade(config, "head")

    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = {row[0] for row in rows}
        assert EXPECTED_TABLES.issubset(table_names), (
            f"missing tables: {EXPECTED_TABLES - table_names}"
        )

        index_rows = con.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='artifacts'"
        ).fetchall()
        # The auto-index created by the (project_id, kind, revision) UniqueConstraint
        # may be named ``sqlite_autoindex_artifacts_<n>``. Verify its columns instead.
        matching = [
            info
            for name, _ in index_rows
            for info in [con.execute(f"PRAGMA index_info({name})").fetchall()]
            if _is_unique(tuple(info))
        ]
        assert matching, (
            "no unique index over (project_id, kind, revision) on artifacts"
        )
    finally:
        con.close()