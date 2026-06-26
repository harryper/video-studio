#!/usr/bin/env python3
"""Unit tests for the targeted length-repair flow.

Run: python3 scripts/test_script_repair.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_script_jobs as psj  # noqa: E402


def _job(script, duration_sec=40, writer_attempt=1):
    """40s video → bounds 300-380 (target 216). writer_attempt=1 means the
    initial write already happened; repair is the 2nd+ pass."""
    return {
        "id": "v_test0001",
        "theme": "测试主题",
        "script": script,
        "writer_attempt": writer_attempt,
        "render": {"duration_sec": duration_sec},
    }


def test_prompt_short_direction():
    """Below the floor → prompt nudges expand, not trim."""
    job = _job(script="短" * 277)  # 277 < 300 floor
    mn, mx = psj.script_length_bounds(40)
    prompt = psj.build_repair_prompt(job, job["script"], mn, mx)
    assert "少 23 字" in prompt, f"should report 23-char shortfall, got: {prompt[:120]}"
    assert "扩写" in prompt, "short script should nudge expand"
    assert "删减" not in prompt, "short script must not nudge trim"
    assert f"{mn}-{mx}" in prompt, "bounds must appear in prompt"
    print(f"✓ short script (277<300) → expand nudge, gap reported")


def test_prompt_long_direction():
    """Above the cap → prompt nudges trim, not expand."""
    job = _job(script="长" * 500)  # 500 > 380 cap
    mn, mx = psj.script_length_bounds(40)
    prompt = psj.build_repair_prompt(job, job["script"], mn, mx)
    assert "多 120 字" in prompt, f"should report 120-char overshoot, got: {prompt[:120]}"
    assert "删减" in prompt, "long script should nudge trim"
    assert "扩写" not in prompt, "long script must not nudge expand"
    print(f"✓ long script (500>380) → trim nudge, overshoot reported")


def test_prompt_includes_current_script():
    """The existing script must be embedded so the agent can edit in-place."""
    job = _job(script="一二三四五")
    mn, mx = psj.script_length_bounds(40)
    prompt = psj.build_repair_prompt(job, job["script"], mn, mx)
    assert "一二三四五" in prompt, "current script must be in prompt body"
    print("✓ current script embedded in repair prompt")


def test_repair_cap_bails_without_agent():
    """At the attempt cap, repair must return False WITHOUT spawning the
    agent subprocess — no point burning tokens on an over-budget script."""
    job = _job(script="短" * 277, writer_attempt=psj.MAX_WRITER_ATTEMPTS)
    called = {"run": False}

    def _bomb(*a, **k):
        called["run"] = True
        raise AssertionError("subprocess.run must not be called at cap")

    orig = psj.subprocess.run
    psj.subprocess.run = _bomb
    try:
        ok = psj.repair_script_length(job, 300, 380)
    finally:
        psj.subprocess.run = orig
    assert ok is False, "repair at cap must return False"
    assert not called["run"], "subprocess.run must not be called at cap"
    print("✓ at MAX_WRITER_ATTEMPTS cap → bail without agent call")


def test_repair_no_script_returns_false():
    """Nothing on disk to repair from → False, no agent call."""
    job = _job(script="", writer_attempt=1)
    job["id"] = "v_nonexistent0001"  # no script.txt on disk either
    orig = psj.subprocess.run
    psj.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no agent"))
    try:
        ok = psj.repair_script_length(job, 300, 380)
    finally:
        psj.subprocess.run = orig
    assert ok is False, "empty script + no script.txt must return False"
    print("✓ empty script + missing script.txt → False (no repair source)")


def main():
    tests = [
        test_prompt_short_direction,
        test_prompt_long_direction,
        test_prompt_includes_current_script,
        test_repair_cap_bails_without_agent,
        test_repair_no_script_returns_false,
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
