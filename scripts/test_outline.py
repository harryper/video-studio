#!/usr/bin/env python3
"""Unit tests for outline phase (two-phase script writing).

Run: python3 scripts/test_outline.py
"""
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_script_jobs as psj  # noqa: E402


@contextlib.contextmanager
def monkeypatch_attr(obj, name, value):
    """Standalone monkeypatch — restore the original attribute on exit.
    Used because these tests run without pytest."""
    had = hasattr(obj, name)
    orig = getattr(obj, name, None)
    setattr(obj, name, value)
    try:
        yield
    finally:
        if had:
            setattr(obj, name, orig)
        else:
            delattr(obj, name)


def test_build_outline_prompt_contains_theme():
    """主题必须出现在提纲 prompt 里。"""
    job = {"theme": "西瓜为什么不能制糖"}
    prompt = psj.build_outline_prompt(job)
    assert "西瓜为什么不能制糖" in prompt, "theme missing from outline prompt"


def test_build_outline_prompt_contains_schema_keywords():
    """提纲 prompt 必须引导 LLM 输出 facts / angle / hook 三字段。"""
    job = {"theme": "test"}
    prompt = psj.build_outline_prompt(job)
    for kw in ["facts", "angle", "hook"]:
        assert kw in prompt, f"outline prompt missing keyword: {kw}"


def test_build_outline_prompt_demands_json_only():
    """提纲 prompt 必须禁止 markdown fence / 额外说明。"""
    job = {"theme": "test"}
    prompt = psj.build_outline_prompt(job)
    assert "json" in prompt.lower(), "outline prompt missing JSON keyword"
    assert "markdown fence" in prompt or "markdown" in prompt, \
        "outline prompt should warn against markdown fence"


def test_generate_outline_parses_valid_json():
    """合法 JSON → 返回 (dict, None)。"""
    import json as _json
    valid = _json.dumps({
        "facts": ["西瓜含糖 6-8%", "甘蔗含糖 17-20%"],
        "angle": "经济账角度",
        "hook": "西瓜其实挺甜",
    })
    with monkeypatch_attr(psj.llm_client, "complete", lambda **kwargs: valid):
        outline, err = psj.generate_outline({"theme": "西瓜", "id": "v_x"})
    assert err is None, f"unexpected err: {err}"
    assert outline["facts"] == ["西瓜含糖 6-8%", "甘蔗含糖 17-20%"]
    assert outline["angle"] == "经济账角度"
    assert outline["hook"] == "西瓜其实挺甜"


def test_generate_outline_returns_error_on_bad_json():
    """非法 JSON → 返回 (None, err_msg)。"""
    with monkeypatch_attr(psj.llm_client, "complete", lambda **kwargs: "not json at all"):
        outline, err = psj.generate_outline({"theme": "x", "id": "v_x"})
    assert outline is None, "expected None outline on bad JSON"
    assert err, "expected non-empty err msg"


def test_generate_outline_returns_error_on_missing_fields():
    """JSON 缺字段（facts/angle/hook）→ 返回 (None, err_msg)。"""
    import json as _json
    incomplete = _json.dumps({"facts": ["a"]})  # 缺 angle/hook
    with monkeypatch_attr(psj.llm_client, "complete", lambda **kwargs: incomplete):
        outline, err = psj.generate_outline({"theme": "x", "id": "v_x"})
    assert outline is None, "expected None on missing fields"
    assert err, "expected err msg"


def test_generate_outline_recovers_json_in_prose():
    """LLM 在 prose 里夹 JSON 块也能解出来（复用 _iter_json_objects）。"""
    import json as _json
    obj = {"facts": ["a", "b"], "angle": "A", "hook": "H"}
    noisy = "好的, 这是提纲:\n" + _json.dumps(obj) + "\n请确认."
    with monkeypatch_attr(psj.llm_client, "complete", lambda **kwargs: noisy):
        outline, err = psj.generate_outline({"theme": "x", "id": "v_x"})
    assert err is None, f"unexpected err: {err}"
    assert outline["hook"] == "H"


def test_build_prompt_includes_outline_block_when_present():
    """job 含 outline → build_prompt 输出包含 facts 文字。"""
    job = {
        "theme": "x",
        "render": {"duration_sec": 60},
        "outline": {
            "facts": ["西瓜含糖 6-8%", "甘蔗含糖 17-20%"],
            "angle": "经济账角度",
            "hook": "西瓜其实挺甜",
        },
    }
    prompt = psj.build_prompt(job)
    assert "西瓜含糖 6-8%" in prompt, "outline fact missing from build_prompt"
    assert "经济账角度" in prompt, "outline angle missing from build_prompt"
    assert "已确认的创作提纲" in prompt, "outline block header missing"


def test_build_prompt_omits_outline_block_when_absent():
    """job 无 outline → build_prompt 不含「已确认的创作提纲」字样（兼容旧 job / 旧路径）。"""
    job = {"theme": "x", "render": {"duration_sec": 60}}
    prompt = psj.build_prompt(job)
    assert "已确认的创作提纲" not in prompt, \
        "outline block header should not appear when no outline"


def test_pending_jobs_includes_pending_script():
    """pending_jobs 应该同时拣 pending 和 pending_script 状态。"""
    import tempfile
    import json as _json
    from pathlib import Path as _P
    with tempfile.TemporaryDirectory() as tmp:
        orig = psj.JOBS_DIR
        psj.JOBS_DIR = _P(tmp) / "jobs" / "video"
        psj.JOBS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            for st in ("pending", "pending_script", "ready_script", "writing"):
                job = {"id": f"v_{st}", "mode": "video", "status": st, "created_at": st}
                (psj.JOBS_DIR / f"v_{st}.json").write_text(
                    _json.dumps(job, ensure_ascii=False), encoding="utf-8"
                )
            picked = psj.pending_jobs()
            ids = sorted(j["id"] for j in picked)
            assert ids == ["v_pending", "v_pending_script"], \
                f"pending_jobs picked wrong jobs: {ids}"
        finally:
            psj.JOBS_DIR = orig


def main():
    tests = [
        test_build_outline_prompt_contains_theme,
        test_build_outline_prompt_contains_schema_keywords,
        test_build_outline_prompt_demands_json_only,
        test_generate_outline_parses_valid_json,
        test_generate_outline_returns_error_on_bad_json,
        test_generate_outline_returns_error_on_missing_fields,
        test_generate_outline_recovers_json_in_prose,
        test_build_prompt_includes_outline_block_when_present,
        test_build_prompt_omits_outline_block_when_absent,
        test_pending_jobs_includes_pending_script,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  ✓ {t.__name__}")
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
