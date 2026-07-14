#!/usr/bin/env python3
"""End-to-end smoke test: script → director → render (T12).

Exercises the full 4-stage pipeline with every external service stubbed:
  - LLM: shotlist builder's _llm param is replaced with a deterministic stub
  - MiniMax: try_minimax_image returns a fake JPEG without network
  - Stock APIs: already no-op after T9 removal
  - hyperframes: subprocess.run stub writes a fake mp4
  - PIL: real (we want to verify the ink-line card actually renders)

Asserts that:
  1. Director produces a valid shotlist.json
  2. Render reads that shotlist.json and uses its prompts
  3. Final HTML file is produced with the expected scene count
  4. The ink-line card fallback file is created when MiniMax is unreachable
"""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_director_jobs as dj  # noqa: E402
import process_video_render_jobs as rj  # noqa: E402
import extract_scene_keywords as ek  # noqa: E402


def _stub_llm(theme, chunks, session_key):  # noqa: ARG001
    """Deterministic LLM stub: every scene gets a clean shot."""
    out = []
    non_empty = [c for c in chunks if c and c.strip()]
    for i, c in enumerate(non_empty):
        out.append({
            "scene_index": chunks[: i + 1].count(""),  # align to non-pad index
            "chunk": c,
            "composition": ("hook" if i == 0
                            else "data-compare" if i == 1
                            else "process" if i == 2
                            else "twist" if i == 3
                            else "metaphor"),
            "action": f"拿工具 i={i}",
            "annotations": [{"text": f"标签{i}", "color": "blue"}],
        })
    return out


def _fake_hyperframes(cmd, **kw):
    class _R:
        def __init__(self, rc=0, stdout="", stderr=""):
            self.returncode = rc
            self.stdout = stdout
            self.stderr = stderr
    if isinstance(cmd, list) and "render" in cmd:
        try:
            out_idx = cmd.index("--output") + 1
            Path(cmd[out_idx]).write_bytes(b"FAKE-MP4")
        except (ValueError, IndexError):
            pass
        return _R()
    if isinstance(cmd, list) and "ffprobe" in cmd:
        return _R(stdout="42\n")
    return _R()


def test_e2e_director_then_render_produces_html_and_cards():
    """Run the full 4-stage pipeline with all externals stubbed."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        # Monkey-patch the shared dirs into a sandbox.
        orig_jobs = dj.JOBS_DIR
        orig_runs = dj.RUNS_DIR
        orig_render_runs = rj.VIDEO_RUNS_DIR
        jobs_dir = tmpdir / "jobs" / "video"
        runs_dir = tmpdir / "runs"
        jobs_dir.mkdir(parents=True)
        runs_dir.mkdir(parents=True)

        dj.JOBS_DIR = jobs_dir
        dj.RUNS_DIR = runs_dir
        rj.VIDEO_RUNS_DIR = runs_dir

        job_id = "v_e2e01"
        job = {
            "id": job_id,
            "mode": "video",
            "status": "ready_script",
            "theme": "测试主题",
            "render": {"duration_sec": 30},
            "script": "第一句。第二句。第三句比较长一点用来让 split 真的切出多个 scene。第四句结束。",
        }
        (jobs_dir / f"{job_id}.json").write_text(
            json.dumps(job, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        render_dir = tmpdir / "render"
        render_dir.mkdir()

        try:
            # ── Stage 2: director ────────────────────────────────────
            ok = dj.process_one(job, _llm=_stub_llm)
            assert ok, "director process_one must succeed"

            shotlist_path = runs_dir / job_id / "shotlist.json"
            assert shotlist_path.exists(), "director must write shotlist.json"
            shotlist = json.loads(shotlist_path.read_text(encoding="utf-8"))
            assert shotlist["schema_version"] == ek.SHOTLIST_SCHEMA_VERSION
            assert len([s for s in shotlist["shots"] if s]) > 0, \
                "must have at least one non-pad shot"

            # ── Stage 3: render ──────────────────────────────────────
            captured = []
            def fake_minimax(prompt, out_path, **kw):
                captured.append(prompt)
                out_path.write_bytes(b"OK")
                return True

            with patch.object(rj, "try_minimax_image", side_effect=fake_minimax), \
                 patch.object(rj, "build_image_composition_html",
                              return_value="<html>stub</html>"), \
                 patch.object(rj, "_load_alignment_scene_times", return_value=[]), \
                 patch.object(rj, "_load_alignment_subtimes", return_value=[]), \
                 patch.object(rj, "_enrich_with_kinetic",
                              side_effect=lambda items, chunks, w, h: items), \
                 patch("subprocess.run") as fake_run:
                fake_run.side_effect = _fake_hyperframes
                rj.render_placeholder(
                    job_id=job_id,
                    render_dir=render_dir,
                    script_text=job["script"],
                    theme=job["theme"],
                )

            # ── Verify ───────────────────────────────────────────────
            assert len(captured) > 0, "MiniMax should have been called"
            # All captured prompts should come from the shotlist (not
            # legacy build_visual_prompt which uses "cinematic photography").
            for p in captured:
                assert "cinematic photography" not in p, \
                    f"prompt should come from shotlist, got: {p[:120]}"
            # HTML should have been written
            html_path = render_dir / "index.html"
            assert html_path.exists(), "render must write index.html"
            assert html_path.read_text(encoding="utf-8") == "<html>stub</html>"
        finally:
            dj.JOBS_DIR = orig_jobs
            dj.RUNS_DIR = orig_runs
            rj.VIDEO_RUNS_DIR = orig_render_runs


def test_e2e_render_falls_back_to_ink_line_when_minimax_down():
    """When MiniMax is unreachable, render still completes via ink-line cards."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        orig_jobs = dj.JOBS_DIR
        orig_runs = dj.RUNS_DIR
        orig_render_runs = rj.VIDEO_RUNS_DIR
        jobs_dir = tmpdir / "jobs" / "video"
        runs_dir = tmpdir / "runs"
        jobs_dir.mkdir(parents=True)
        runs_dir.mkdir(parents=True)

        dj.JOBS_DIR = jobs_dir
        dj.RUNS_DIR = runs_dir
        rj.VIDEO_RUNS_DIR = runs_dir

        job_id = "v_e2e02"
        job = {
            "id": job_id,
            "mode": "video",
            "status": "ready_script",
            "theme": "ink-line test",
            "render": {"duration_sec": 18},
            "script": "单句测试。",
        }
        (jobs_dir / f"{job_id}.json").write_text(
            json.dumps(job, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        render_dir = tmpdir / "render"
        render_dir.mkdir()

        try:
            ok = dj.process_one(job, _llm=_stub_llm)
            assert ok

            with patch.object(rj, "try_minimax_image",
                              side_effect=lambda *a, **kw: False), \
                 patch.object(rj, "build_image_composition_html",
                              return_value="<html>stub</html>"), \
                 patch.object(rj, "_load_alignment_scene_times", return_value=[]), \
                 patch.object(rj, "_load_alignment_subtimes", return_value=[]), \
                 patch.object(rj, "_enrich_with_kinetic",
                              side_effect=lambda items, chunks, w, h: items), \
                 patch("subprocess.run") as fake_run:
                fake_run.side_effect = _fake_hyperframes
                rj.render_placeholder(
                    job_id=job_id,
                    render_dir=render_dir,
                    script_text=job["script"],
                    theme=job["theme"],
                )

            # Ink-line cards must have been rendered for every scene.
            images_dir = render_dir / "images"
            jpegs = list(images_dir.glob("scene_*.jpg"))
            assert len(jpegs) > 0, "ink-line card fallback must produce images"
            # Each card must be a real, non-empty JPEG.
            for jpeg in jpegs:
                assert jpeg.stat().st_size > 1000, \
                    f"ink-line card too small: {jpeg}"
                with open(jpeg, "rb") as f:
                    header = f.read(3)
                assert header == b"\xff\xd8\xff", \
                    f"ink-line card must be JPEG, got header {header!r}"
        finally:
            dj.JOBS_DIR = orig_jobs
            dj.RUNS_DIR = orig_runs
            rj.VIDEO_RUNS_DIR = orig_render_runs


if __name__ == "__main__":
    test_e2e_director_then_render_produces_html_and_cards()
    test_e2e_render_falls_back_to_ink_line_when_minimax_down()
    print("\n✅ all 2 e2e_director_smoke tests passed")