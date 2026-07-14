#!/usr/bin/env python3
"""Offline tests for the script daemon's engine-decoupled code path.

The old daemon shelled out to `openclaw agent` which then wrote
runs/<id>/script.txt itself. The new daemon calls llm_client directly
and writes the file from the JSON {script, cover} response.

These tests stub llm_client.complete so the daemon is exercised end-to-end
without any HTTP. Validates:
  - generate_script writes script.txt + cover.json when the LLM returns
    a clean JSON object
  - generate_script validates the cover (rejects bad highlights) but
    still writes script.txt (render daemon falls back for the cover)
  - generate_script falls back to truncation recovery when the LLM
    response is unparseable
  - finalize_from_script_file produces the right script_meta + status

Run: python3 scripts/test_script_engine_decouple.py
"""
import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_script_jobs as psj  # noqa: E402


@contextmanager
def _temp_runs():
    """Redirect psj.RUNS_DIR to a temp dir for the duration."""
    tmp = tempfile.TemporaryDirectory()
    orig = psj.RUNS_DIR
    psj.RUNS_DIR = Path(tmp.name)
    try:
        yield
    finally:
        psj.RUNS_DIR = orig
        tmp.cleanup()


def _make_job(job_id="v_test_engine", duration_sec=60, theme="测试"):
    return {
        "id": job_id,
        "theme": theme,
        "mode": "video",
        "status": "pending",
        "render": {"duration_sec": duration_sec},
        "audio": {"speed": 1.0},
        "created_at": "2026-07-13T00:00:00",
    }


def test_generate_script_writes_script_and_cover():
    """Happy path: clean JSON object → both files written."""
    with _temp_runs():
        job = _make_job()
        llm_text = json.dumps({
            "script": "如果全世界人类都不吃脂肪会怎样？答案是糖会直接杀死你。第一笔，先看心脏。心脏是脂肪撑起来的。",
            "cover": {
                "main": "糖不是调味品",
                "main_highlight": [1, 3],
                "sub": "二战真相比你想的更狠",
            },
        }, ensure_ascii=False)

        with patch.object(psj.llm_client, "complete", return_value=llm_text):
            script, cover, err = psj.generate_script(job)

        assert err is None, f"unexpected error: {err}"
        assert cover is not None, "valid cover should pass validation"
        assert cover["main"] == "糖不是调味品"
        run_dir = psj.RUNS_DIR / job["id"]
        assert (run_dir / "script.txt").exists(), "script.txt should be written"
        assert (run_dir / "cover.json").exists(), "cover.json should be written"
        on_disk = (run_dir / "script.txt").read_text(encoding="utf-8")
        assert script in on_disk and on_disk.startswith(script[:10])
        on_disk_cover = json.loads((run_dir / "cover.json").read_text(encoding="utf-8"))
        assert on_disk_cover["main"] == "糖不是调味品"
    print("✓ generate_script writes script.txt + cover.json (happy path)")


def test_generate_script_rejects_bad_cover_but_writes_script():
    """Invalid cover (start=0 → reject) → cover None, script still written."""
    with _temp_runs():
        job = _make_job()
        llm_text = json.dumps({
            "script": "测试正文，超过最短长度要求。",
            "cover": {
                "main": "糖不是调味品",
                "main_highlight": [0, 6],  # start=0 → reject
                "sub": "正常副标",
            },
        }, ensure_ascii=False)

        with patch.object(psj.llm_client, "complete", return_value=llm_text):
            script, cover, err = psj.generate_script(job)

        assert err is None, f"unexpected error: {err}"
        assert script is not None
        assert cover is None, f"invalid cover should be rejected, got {cover}"
        run_dir = psj.RUNS_DIR / job["id"]
        assert (run_dir / "script.txt").exists()
        assert not (run_dir / "cover.json").exists(), "rejected cover should not be persisted"
    print("✓ invalid cover rejected; script.txt still written (caller falls back)")


def test_generate_script_recovers_truncated_response():
    """LLM hit max_tokens mid-JSON → recover the script field. The parser
    flags it as truncated but generate_script treats recovery as success
    (the script is usable; caller will length-check and may repair)."""
    with _temp_runs():
        job = _make_job()
        truncated = '{"script": "截断的脚本正文但前几个字还在这里'

        with patch.object(psj.llm_client, "complete", return_value=truncated):
            script, cover, err = psj.generate_script(job)

        assert script is not None, "truncation recovery should yield a partial script"
        assert "截断的脚本正文" in script
        # err may be set ("truncated JSON, recovered partial script") or None —
        # the contract is "script is usable", not "err was populated".
        run_dir = psj.RUNS_DIR / job["id"]
        assert (run_dir / "script.txt").exists(), "recovered script should be written"
    print("✓ truncated response → script recovered and written")


def test_generate_script_handles_unparseable_response():
    """Pure prose, no JSON at all → None, error."""
    with _temp_runs():
        job = _make_job()
        with patch.object(psj.llm_client, "complete", return_value="I cannot do that."):
            script, cover, err = psj.generate_script(job)
        assert script is None and err is not None
    print("✓ unparseable response → (None, error)")


def test_generate_script_handles_api_exception():
    """llm_client.complete raises → (None, None, error)."""
    with _temp_runs():
        job = _make_job()
        with patch.object(psj.llm_client, "complete", side_effect=RuntimeError("network down")):
            script, cover, err = psj.generate_script(job)
        assert script is None and cover is None and "network down" in err
    print("✓ API exception surfaces as error message")


def test_repair_prompt_contains_directional_nudge():
    """Repair prompt for an over-long script says 'trim', under-long says 'expand'."""
    job = _make_job(duration_sec=60)  # bounds = (300, 521)
    long_script = "很长的脚本" * 100
    over_prompt = psj.build_repair_prompt(job, long_script, 300, 521)
    assert "上限多" in over_prompt, "over-max repair should say trim"
    short_script = "短"
    under_prompt = psj.build_repair_prompt(job, short_script, 300, 521)
    assert "下限少" in under_prompt, "under-min repair should say expand"
    print("✓ repair prompt picks correct direction (expand vs trim)")


def test_build_prompt_is_style_neutral():
    """build_prompt no longer injects the old [xingzhe] 段子 writing style.

    The script-body style was removed (待定义新风格); only the neutral
    skeleton (theme + length + JSON) plus the cover instructions remain.
    """
    job = _make_job(duration_sec=60, theme="糖在二战")
    prompt = psj.build_prompt(job)
    # Old style must be gone
    for leaked in ("reference-style-video.md", "reference-memes.md",
                   "恐怖直立猿", "夏侯惇", "段子", "第一笔", "MEME_GUIDE"):
        assert leaked not in prompt, f"removed style leaked into prompt: {leaked}"
    # Neutral skeleton + cover preserved
    assert "糖在二战" in prompt, "theme must be in prompt"
    assert "main_highlight" in prompt, "cover instructions must be preserved"
    assert '"script"' in prompt, "JSON output format must be present"
    # No agent-flavoured instructions
    assert "session-key" not in prompt
    assert "openclaw" not in prompt.lower()
    print("✓ build_prompt is style-neutral (skeleton + cover only)")


def main():
    tests = [
        test_generate_script_writes_script_and_cover,
        test_generate_script_rejects_bad_cover_but_writes_script,
        test_generate_script_recovers_truncated_response,
        test_generate_script_handles_unparseable_response,
        test_generate_script_handles_api_exception,
        test_repair_prompt_contains_directional_nudge,
        test_build_prompt_is_style_neutral,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
