#!/usr/bin/env python3
"""Test render daemon's pending_jobs filter (regression test for T9 miss).

After the 4-stage pipeline change (script → director → render → narrate),
render must pick up `ready_shotlist` jobs, NOT `ready_script`. T9/T10/T11
added the director stage but the render daemon's pending_jobs() was left
filtering on `ready_script`, so the entire `ready_shotlist` cohort (incl.
v_affb0166) sat idle.

This test pins the correct filter so the bug can't reappear.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_render_jobs as rj  # noqa: E402


def _make_job(jobs_dir, job_id, status):
    path = jobs_dir / f"{job_id}.json"
    path.write_text(json.dumps({
        "id": job_id, "mode": "video", "status": status,
        "created_at": job_id,
    }, ensure_ascii=False), encoding="utf-8")


def test_render_pending_includes_ready_shotlist():
    """ready_shotlist jobs must be visible to render daemon."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        orig_jobs = rj.JOBS_DIR
        rj.JOBS_DIR = tmpdir / "jobs" / "video"
        rj.JOBS_DIR.mkdir(parents=True)
        try:
            _make_job(rj.JOBS_DIR, "v_a", "ready_shotlist")
            pending = rj.pending_jobs()
            ids = [j["id"] for j in pending]
            assert "v_a" in ids, \
                f"render must pick up ready_shotlist; got {ids}"
        finally:
            rj.JOBS_DIR = orig_jobs


def test_render_pending_excludes_ready_script():
    """ready_script is director's job — render must NOT pick it up."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        orig_jobs = rj.JOBS_DIR
        rj.JOBS_DIR = tmpdir / "jobs" / "video"
        rj.JOBS_DIR.mkdir(parents=True)
        try:
            _make_job(rj.JOBS_DIR, "v_b", "ready_script")
            pending = rj.pending_jobs()
            ids = [j["id"] for j in pending]
            assert "v_b" not in ids, \
                f"render must skip ready_script (that's director's job); got {ids}"
        finally:
            rj.JOBS_DIR = orig_jobs


def test_render_pending_excludes_already_rendered_and_final():
    """rendered/final jobs are downstream — not render's input."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        orig_jobs = rj.JOBS_DIR
        rj.JOBS_DIR = tmpdir / "jobs" / "video"
        rj.JOBS_DIR.mkdir(parents=True)
        try:
            for st in ("rendered", "final", "error", "pending"):
                _make_job(rj.JOBS_DIR, f"v_{st}", st)
            pending = rj.pending_jobs()
            ids = {j["id"] for j in pending}
            assert ids == set(), f"only ready_shotlist should be pending; got {ids}"
        finally:
            rj.JOBS_DIR = orig_jobs


if __name__ == "__main__":
    test_render_pending_includes_ready_shotlist()
    test_render_pending_excludes_ready_script()
    test_render_pending_excludes_already_rendered_and_final()
    print("\n✅ all 3 render_pending_filter tests passed")