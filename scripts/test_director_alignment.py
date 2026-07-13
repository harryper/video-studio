#!/usr/bin/env python3
"""Unit tests for director daemon alignment + cascade trigger logic.

Run: python3 scripts/test_director_alignment.py

No LLM call (LLM is stubbed), no real trigger file writes (paths monkey-patched).
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_director_jobs as dj  # noqa: E402
import extract_scene_keywords as ek  # noqa: E402


def _stub_llm(theme, chunks, session_key):  # noqa: ARG001
    return [{
        "scene_index": i,
        "chunk": c,
        "composition": "hook" if i == 0 else "process",
        "action": f"action {i}",
        "annotations": [],
    } for i, c in enumerate(chunks) if c.strip()]


def test_process_one_writes_status_ready_shotlist():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        orig_jobs = dj.JOBS_DIR
        orig_runs = dj.RUNS_DIR
        orig_render_trigger = dj.RENDER_TRIGGER
        dj.JOBS_DIR = tmpdir / "jobs" / "video"
        dj.RUNS_DIR = tmpdir / "runs"
        dj.RENDER_TRIGGER = tmpdir / ".video-render-trigger"
        dj.JOBS_DIR.mkdir(parents=True)
        dj.RUNS_DIR.mkdir(parents=True)

        try:
            job = {
                "id": "v_align01",
                "mode": "video",
                "status": "ready_script",
                "theme": "测试",
                "render": {"duration_sec": 30},
                "script": "第一句。第二句。",
            }
            job_path = dj.JOBS_DIR / "v_align01.json"
            job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2),
                                encoding="utf-8")

            ok = dj.process_one(job, _llm=_stub_llm)
            assert ok, "process_one 必须成功"

            updated = json.loads(job_path.read_text(encoding="utf-8"))
            assert updated["status"] == "ready_shotlist"
            assert updated.get("error") in (None, "")

            shotlist_path = dj.RUNS_DIR / "v_align01" / "shotlist.json"
            assert shotlist_path.exists()
            shotlist = json.loads(shotlist_path.read_text(encoding="utf-8"))
            assert shotlist["schema_version"] == ek.SHOTLIST_SCHEMA_VERSION
        finally:
            dj.JOBS_DIR = orig_jobs
            dj.RUNS_DIR = orig_runs
            dj.RENDER_TRIGGER = orig_render_trigger


def test_cascade_touches_render_trigger_when_ready_shotlist():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        orig_jobs = dj.JOBS_DIR
        orig_render_trigger = dj.RENDER_TRIGGER
        orig_director_trigger = dj.DIRECTOR_TRIGGER
        dj.JOBS_DIR = tmpdir / "jobs" / "video"
        dj.RENDER_TRIGGER = tmpdir / ".video-render-trigger"
        dj.DIRECTOR_TRIGGER = tmpdir / ".video-director-trigger"
        dj.JOBS_DIR.mkdir(parents=True)

        try:
            (dj.JOBS_DIR / "v_cascade01.json").write_text(
                json.dumps({
                    "id": "v_cascade01", "mode": "video",
                    "status": "ready_shotlist",
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            dj._scan_and_touch_triggers()
            assert dj.RENDER_TRIGGER.exists(), "ready_shotlist → render trigger 必须被 touch"
        finally:
            dj.JOBS_DIR = orig_jobs
            dj.RENDER_TRIGGER = orig_render_trigger
            dj.DIRECTOR_TRIGGER = orig_director_trigger


def test_cascade_does_not_touch_when_only_ready_script():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        orig_jobs = dj.JOBS_DIR
        orig_render_trigger = dj.RENDER_TRIGGER
        dj.JOBS_DIR = tmpdir / "jobs" / "video"
        dj.RENDER_TRIGGER = tmpdir / ".video-render-trigger"
        dj.JOBS_DIR.mkdir(parents=True)

        try:
            (dj.JOBS_DIR / "v_no01.json").write_text(
                json.dumps({
                    "id": "v_no01", "mode": "video",
                    "status": "ready_script",
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            dj._scan_and_touch_triggers()
            assert not dj.RENDER_TRIGGER.exists()
        finally:
            dj.JOBS_DIR = orig_jobs
            dj.RENDER_TRIGGER = orig_render_trigger


def test_pending_jobs_filters_ready_script_only():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        orig_jobs = dj.JOBS_DIR
        dj.JOBS_DIR = tmpdir / "jobs" / "video"
        dj.JOBS_DIR.mkdir(parents=True)

        try:
            statuses = ["pending", "ready_script", "ready_shotlist", "rendered", "final"]
            for st in statuses:
                (dj.JOBS_DIR / f"v_{st}.json").write_text(
                    json.dumps({"id": f"v_{st}", "mode": "video", "status": st,
                                "created_at": st}, ensure_ascii=False),
                    encoding="utf-8",
                )
            pending = dj.pending_jobs()
            assert len(pending) == 1
            assert pending[0]["status"] == "ready_script"
        finally:
            dj.JOBS_DIR = orig_jobs


if __name__ == "__main__":
    test_process_one_writes_status_ready_shotlist()
    test_cascade_touches_render_trigger_when_ready_shotlist()
    test_cascade_does_not_touch_when_only_ready_script()
    test_pending_jobs_filters_ready_script_only()
    print("\n✅ all 4 director_alignment tests passed")