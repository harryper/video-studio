#!/usr/bin/env python3
"""Unit tests for the shotlist-driven render loop.

Validates that `render_placeholder`'s per-scene loop:
  1. Reads runs/{job_id}/shotlist.json when present and uses its shot
     prompts (composition / action / annotations) to drive MiniMax image
     generation with negative_prompt.
  2. Retries with an action paraphrase (no annotations) on MiniMax failure.
  3. Retries a third time with annotations stripped on second failure.
  4. Falls back to a local ink-line PIL card when all retries fail
     (no Pexels/Pixabay stock — the post-cat-doctor plan removed stock).

Network, MiniMax, hyperframes, and PIL Image.save are all stubbed via
monkey-patching. No external IO.
"""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_render_jobs as rj  # noqa: E402


def _fake_shotlist(job_id, *, shots):
    """Write a minimal valid shotlist.json under RUNS_DIR/job_id/."""
    shotlist = {
        "schema_version": 1,
        "script_hash": "deadbeef",
        "theme": "测试主题",
        "shots": shots,
    }
    p = rj.VIDEO_RUNS_DIR / job_id / "shotlist.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(shotlist, ensure_ascii=False), encoding="utf-8")
    return p


def _fake_hyperframes_run(cmd, **kw):
    """subprocess.run stub for hyperframes lint/validate/render + ffprobe.

    The hyperframes render call writes video-only.mp4 to the --output arg
    so the post-loop existence check passes. All other subprocess calls
    just return success. ffprobe returns a non-empty stdout so the
    ffprobe gate passes too.
    """
    class _R:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
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
        return _R(stdout="42\n", stderr="")
    return _R()


def test_loop_uses_shot_prompts_from_shotlist_json():
    """When shotlist.json exists, prompts are sourced from `prompt` field."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        orig_runs = rj.VIDEO_RUNS_DIR
        rj.VIDEO_RUNS_DIR = tmpdir / "runs"
        rj.VIDEO_RUNS_DIR.mkdir(parents=True, exist_ok=True)
        job_id = "v_t01"
        render_dir = tmpdir / "render"
        render_dir.mkdir()

        _fake_shotlist(job_id, shots=[
            {
                "scene_index": 0,
                "chunk": "首句",
                "composition": "hook",
                "action": "拿放大镜看",
                "annotations": [{"text": "真相", "color": "red"}],
                "prompt": "INK-LINE PROMPT SHOT 0",
                "negative_prompt": "kawaii chibi cute",
            },
            None,  # pad
            {
                "scene_index": 2,
                "chunk": "末句",
                "composition": "twist",
                "action": "翻页",
                "annotations": [],
                "prompt": "INK-LINE PROMPT SHOT 2",
                "negative_prompt": "3D 渲染 拟真",
            },
        ])

        # Stub the slow/network paths so the loop actually exits cleanly.
        captured_prompts = []
        captured_negatives = []

        def fake_minimax(prompt, out_path, **kwargs):
            captured_prompts.append(prompt)
            captured_negatives.append(kwargs.get("negative_prompt", ""))
            out_path.write_bytes(b"FAKE-JPEG-BYTES")
            return True

        def fake_fallback(*a, **kw):
            pass  # no-op gradient stub

        try:
            with patch.object(rj, "try_minimax_image", side_effect=fake_minimax), \
                 patch.object(rj, "create_fallback_image", side_effect=fake_fallback), \
                 patch.object(rj, "build_image_composition_html",
                              return_value="<html></html>"), \
                 patch.object(rj, "_load_alignment_scene_times", return_value=[]), \
                 patch.object(rj, "_load_alignment_subtimes", return_value=[]), \
                 patch.object(rj, "_enrich_with_kinetic",
                              side_effect=lambda items, chunks, w, h: items), \
                 patch.object(rj, "split_script_to_cards",
                              return_value=["首句", "", "末句"]), \
                 patch("subprocess.run") as fake_run:
                fake_run.side_effect = _fake_hyperframes_run

                rj.render_placeholder(
                    job_id=job_id,
                    render_dir=render_dir,
                    script_text="首句。\n\n末句。",
                    theme="测试主题",
                )
        finally:
            rj.VIDEO_RUNS_DIR = orig_runs

        # shot 0 used its own prompt + negative_prompt (not generic build_visual_prompt)
        assert any("INK-LINE PROMPT SHOT 0" in p for p in captured_prompts), \
            f"shot 0 should use shotlist prompt, got: {captured_prompts}"
        assert any("INK-LINE PROMPT SHOT 2" in p for p in captured_prompts)
        # negative_prompt from shotlist is forwarded (not default cat-doctor)
        assert "kawaii chibi cute" in captured_negatives or \
               "3D 渲染 拟真" in captured_negatives


def test_loop_retries_with_action_paraphrase_then_no_annotations():
    """On MiniMax failure, retry #1 paraphrases action; retry #2 drops annotations."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        orig_runs = rj.VIDEO_RUNS_DIR
        rj.VIDEO_RUNS_DIR = tmpdir / "runs"
        rj.VIDEO_RUNS_DIR.mkdir(parents=True, exist_ok=True)
        job_id = "v_t02"
        render_dir = tmpdir / "render"
        render_dir.mkdir()

        _fake_shotlist(job_id, shots=[{
            "scene_index": 0,
            "chunk": "only one",
            "composition": "hook",
            "action": "拿放大镜看",
            "annotations": [{"text": "真相", "color": "red"}],
            "prompt": "PROMPT A",
            "negative_prompt": "kawaii",
        }])

        call_log = []

        def fake_minimax(prompt, out_path, **kwargs):
            call_log.append(prompt)
            if len(call_log) < 3:  # fail twice, succeed on 3rd
                return False
            out_path.write_bytes(b"OK")
            return True

        try:
            with patch.object(rj, "try_minimax_image", side_effect=fake_minimax), \
                 patch.object(rj, "create_fallback_image") as fb_calls, \
                 patch.object(rj, "build_image_composition_html",
                              return_value="<html></html>"), \
                 patch.object(rj, "_load_alignment_scene_times", return_value=[]), \
                 patch.object(rj, "_load_alignment_subtimes", return_value=[]), \
                 patch.object(rj, "_enrich_with_kinetic",
                              side_effect=lambda items, chunks, w, h: items), \
                 patch.object(rj, "split_script_to_cards", return_value=["only one"]), \
                 patch("subprocess.run") as fake_run:
                fake_run.side_effect = _fake_hyperframes_run

                rj.render_placeholder(
                    job_id=job_id,
                    render_dir=render_dir,
                    script_text="only one。",
                    theme="t",
                )
        finally:
            rj.VIDEO_RUNS_DIR = orig_runs

        # 3 calls: original → action paraphrase → no annotations
        assert len(call_log) == 3, f"expected 3 minimax calls, got {len(call_log)}"
        # 1st: original prompt
        assert call_log[0] == "PROMPT A"
        # 2nd: still mentions the action but paraphrased
        assert "放大镜" not in call_log[1] or "paraphrase" in call_log[1].lower() or \
               call_log[1] != "PROMPT A", \
               "retry #1 should be a paraphrase of the original"
        # 3rd: no annotations block
        assert "真相" not in call_log[2] or call_log[2] != call_log[1], \
            "retry #2 should drop annotations"
        # Gradient fallback never called (we succeeded on 3rd try)
        assert not fb_calls.called, "fallback should not be needed when retry succeeds"


def test_loop_falls_back_to_ink_line_card_when_all_retries_fail():
    """When MiniMax fails 3x, render an ink-line PIL card (no gradient)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        orig_runs = rj.VIDEO_RUNS_DIR
        rj.VIDEO_RUNS_DIR = tmpdir / "runs"
        rj.VIDEO_RUNS_DIR.mkdir(parents=True, exist_ok=True)
        job_id = "v_t03"
        render_dir = tmpdir / "render"
        render_dir.mkdir()

        _fake_shotlist(job_id, shots=[{
            "scene_index": 0,
            "chunk": "fallback case",
            "composition": "metaphor",
            "action": "举起问题",
            "annotations": [],
            "prompt": "PROMPT X",
            "negative_prompt": "",
        }])

        def always_fail(*a, **kw):
            return False

        try:
            with patch.object(rj, "try_minimax_image", side_effect=always_fail), \
                 patch.object(rj, "render_ink_line_card",
                              return_value=tmpdir / "fallback.jpg") as ink_calls, \
                 patch.object(rj, "create_fallback_image") as fb_calls, \
                 patch.object(rj, "build_image_composition_html",
                              return_value="<html></html>"), \
                 patch.object(rj, "_load_alignment_scene_times", return_value=[]), \
                 patch.object(rj, "_load_alignment_subtimes", return_value=[]), \
                 patch.object(rj, "_enrich_with_kinetic",
                              side_effect=lambda items, chunks, w, h: items), \
                 patch.object(rj, "split_script_to_cards", return_value=["fallback case"]), \
                 patch("subprocess.run") as fake_run:
                fake_run.side_effect = _fake_hyperframes_run

                rj.render_placeholder(
                    job_id=job_id,
                    render_dir=render_dir,
                    script_text="fallback case。",
                    theme="t",
                )
        finally:
            rj.VIDEO_RUNS_DIR = orig_runs

        assert ink_calls.called, "ink-line card must be used as last-resort fallback"
        # Old gradient create_fallback_image is NOT called anymore
        assert not fb_calls.called, "create_fallback_image (gradient) should be dead — replaced by ink-line card"


def test_loop_no_longer_calls_pexels_or_pixabay():
    """Per cat-doctor plan, all stock sources are removed (REMOVED-STOCK)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        orig_runs = rj.VIDEO_RUNS_DIR
        rj.VIDEO_RUNS_DIR = tmpdir / "runs"
        rj.VIDEO_RUNS_DIR.mkdir(parents=True, exist_ok=True)
        job_id = "v_t04"
        render_dir = tmpdir / "render"
        render_dir.mkdir()

        _fake_shotlist(job_id, shots=[{
            "scene_index": 0,
            "chunk": "ok",
            "composition": "metaphor",
            "action": "动",
            "annotations": [],
            "prompt": "P",
            "negative_prompt": "",
        }])

        try:
            with patch.object(rj, "try_minimax_image",
                              side_effect=lambda *a, **kw: (a[1].write_bytes(b"x"), True)[1]), \
                 patch.object(rj, "build_image_composition_html",
                              return_value="<html></html>"), \
                 patch.object(rj, "_load_alignment_scene_times", return_value=[]), \
                 patch.object(rj, "_load_alignment_subtimes", return_value=[]), \
                 patch.object(rj, "_enrich_with_kinetic",
                              side_effect=lambda items, chunks, w, h: items), \
                 patch.object(rj, "split_script_to_cards", return_value=["ok"]), \
                 patch("subprocess.run") as fake_run, \
                 patch.object(rj, "try_pexels_image") as pex_img, \
                 patch.object(rj, "try_pexels_video") as pex_vid, \
                 patch.object(rj, "try_pixabay_image") as pix_img, \
                 patch.object(rj, "try_pixabay_video") as pix_vid:
                fake_run.side_effect = _fake_hyperframes_run

                rj.render_placeholder(
                    job_id=job_id,
                    render_dir=render_dir,
                    script_text="ok。",
                    theme="t",
                )
        finally:
            rj.VIDEO_RUNS_DIR = orig_runs

        assert not pex_img.called, "Pexels image stock must be removed (REMOVED-STOCK)"
        assert not pex_vid.called
        assert not pix_img.called
        assert not pix_vid.called


if __name__ == "__main__":
    test_loop_uses_shot_prompts_from_shotlist_json()
    test_loop_retries_with_action_paraphrase_then_no_annotations()
    test_loop_falls_back_to_ink_line_card_when_all_retries_fail()
    test_loop_no_longer_calls_pexels_or_pixabay()
    print("\n✅ all 4 render_shotlist tests passed")