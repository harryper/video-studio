#!/usr/bin/env python3
"""Offline tests for the keywords daemon's engine-decoupled code path.

Replaces the old subprocess-based LLM call with a direct llm_client call.
Validates:
  - extract_visual_specs writes a v2 keywords.json cache with schema_version
  - on cache hit (same hash), the LLM is NOT called again
  - on LLM exception, returns empty specs (fall back to regex heuristic)
  - on wrong-length LLM output, retries once then falls back to empty
  - on force_refresh, ignores cache

Run: python3 scripts/test_keywords_engine_decouple.py
"""
import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_scene_keywords as esk  # noqa: E402


@contextmanager
def _temp_runs():
    tmp = tempfile.TemporaryDirectory()
    orig = esk.RUNS_DIR
    esk.RUNS_DIR = Path(tmp.name)
    try:
        yield
    finally:
        esk.RUNS_DIR = orig
        tmp.cleanup()


def _llm_response(specs):
    return json.dumps(specs, ensure_ascii=False)


def test_writes_v2_cache_with_specs():
    """Happy path: LLM returns N specs → keywords.json with schema_version=2."""
    with _temp_runs():
        job_id = "v_kw_happy"
        theme = "糖在二战"
        chunks = ["糖在二战被列为战略物资。", "可口可乐的配方里有它。"]
        llm_text = _llm_response([
            {"subject": "sugar ration card WWII", "shot": "close-up",
             "mood": "urgent, focused", "color_palette": "sepia + black",
             "avoid": "people, faces, text, brand logos"},
            {"subject": "coca-cola original formula bottle", "shot": "extreme close-up",
             "mood": "mysterious", "color_palette": "amber + cream",
             "avoid": "people, faces, hands"},
        ])
        with patch.object(esk.llm_client, "complete", return_value=llm_text) as m:
            specs = esk.extract_visual_specs(job_id, theme, chunks)
        assert m.called, "LLM should have been called"
        assert len(specs) == len(chunks)
        # Every spec must have the right keys
        for s in specs:
            assert set(s.keys()) == set(esk.SPEC_FIELDS)
        # Cache file written with schema_version
        cache = json.loads((esk.RUNS_DIR / job_id / "keywords.json").read_text(encoding="utf-8"))
        assert cache["schema_version"] == 2
        assert cache["script_hash"] == esk._script_hash(chunks, theme)
        assert len(cache["visual_specs"]) == len(chunks)
    print("✓ extract_visual_specs writes v2 cache with normalized specs")


def test_cache_hit_skips_llm():
    """Second call with same inputs reads cache, no LLM call."""
    with _temp_runs():
        job_id = "v_kw_cache"
        chunks = ["场景一。", "场景二。", "场景三。"]
        llm_text = _llm_response([
            {"subject": f"subject {i}", "shot": "close-up",
             "mood": "calm", "color_palette": "blue",
             "avoid": "people"} for i in range(len(chunks))
        ])
        with patch.object(esk.llm_client, "complete", return_value=llm_text) as m:
            first = esk.extract_visual_specs(job_id, "t", chunks)
            assert m.call_count == 1
            # Second call should be a cache hit
            second = esk.extract_visual_specs(job_id, "t", chunks)
            assert m.call_count == 1, "cache hit should NOT call the LLM again"
            assert first == second
    print("✓ cache hit skips LLM")


def test_force_refresh_bypasses_cache():
    """force_refresh=True re-calls the LLM even on a cache hit."""
    with _temp_runs():
        job_id = "v_kw_force"
        chunks = ["场景。"]
        llm_text = _llm_response([{"subject": "x", "shot": "close-up",
                                    "mood": "calm", "color_palette": "blue",
                                    "avoid": "people"}])
        with patch.object(esk.llm_client, "complete", return_value=llm_text) as m:
            esk.extract_visual_specs(job_id, "t", chunks)
            assert m.call_count == 1
            esk.extract_visual_specs(job_id, "t", chunks, force_refresh=True)
            assert m.call_count == 2, "force_refresh should re-call LLM"
    print("✓ force_refresh bypasses cache")


def test_llm_exception_returns_empty_specs():
    """If llm_client.complete raises, return a list of empty dicts (fallback path)."""
    with _temp_runs():
        job_id = "v_kw_err"
        chunks = ["场景一。", "场景二。"]
        with patch.object(esk.llm_client, "complete", side_effect=RuntimeError("timeout")):
            specs = esk.extract_visual_specs(job_id, "t", chunks)
        assert len(specs) == len(chunks)
        for s in specs:
            assert s == {f: "" for f in esk.SPEC_FIELDS}
    print("✓ LLM exception → empty specs (caller regex fallback)")


def test_wrong_length_triggers_retry():
    """If LLM returns a wrong-length list, retry once; if still wrong, empty."""
    with _temp_runs():
        job_id = "v_kw_retry"
        chunks = ["场景一。", "场景二。", "场景三。"]
        # First call returns 1 spec (wrong), second call returns 3 (right)
        responses = iter([
            _llm_response([{"subject": "x", "shot": "close-up", "mood": "m",
                            "color_palette": "c", "avoid": "a"}]),
            _llm_response([
                {"subject": f"sub {i}", "shot": "close-up", "mood": "m",
                 "color_palette": "c", "avoid": "a"} for i in range(len(chunks))
            ]),
        ])
        with patch.object(esk.llm_client, "complete", side_effect=lambda **kw: next(responses)):
            specs = esk.extract_visual_specs(job_id, "t", chunks)
        assert len(specs) == len(chunks)
        assert all(s["subject"] for s in specs)
    print("✓ wrong-length LLM output triggers one retry")


def test_wrong_length_after_retry_returns_empty():
    """Two consecutive wrong-length responses → all empty (no infinite retries)."""
    with _temp_runs():
        job_id = "v_kw_drain"
        chunks = ["a。", "b。", "c。"]
        bad = _llm_response([{"subject": "x", "shot": "s", "mood": "m",
                              "color_palette": "c", "avoid": "a"}])
        with patch.object(esk.llm_client, "complete", return_value=bad) as m:
            specs = esk.extract_visual_specs(job_id, "t", chunks)
        assert m.call_count == 2, f"expected exactly 2 calls, got {m.call_count}"
        assert all(s == {f: "" for f in esk.SPEC_FIELDS} for s in specs)
    print("✓ retry exhausted → all empty specs (caller fallback)")


def test_no_subprocess_import():
    """Belt-and-suspenders: the daemon must not import subprocess or shell
    out to any agent binary. (Comments and docstrings may still reference
    openclaw as historical context for the JSON-walk strategy.)"""
    src = Path(esk.__file__).read_text(encoding="utf-8")
    assert "import subprocess" not in src, "subprocess import leaked"
    # openclaw may only appear inside string literals (docstrings), comments,
    # or the SYSTEM_PROMPT template — never as an executable identifier.
    import ast
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "openclaw":
            raise AssertionError(f"openclaw used as identifier at line {node.lineno}")
        if isinstance(node, ast.Attribute) and node.attr == "openclaw":
            raise AssertionError(f"openclaw used as attribute at line {node.lineno}")
    print("✓ no subprocess import; openclaw never used as an identifier")


def main():
    tests = [
        test_writes_v2_cache_with_specs,
        test_cache_hit_skips_llm,
        test_force_refresh_bypasses_cache,
        test_llm_exception_returns_empty_specs,
        test_wrong_length_triggers_retry,
        test_wrong_length_after_retry_returns_empty,
        test_no_subprocess_import,
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
