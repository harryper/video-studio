#!/usr/bin/env python3
"""Unit tests for extract_scene_visual_specs (v2 schema).

Run: python3 scripts/test_visual_specs.py

Covers the spec parser, the v2 cache (with v1 → v2 invalidation), and
build_visual_prompt's spec-driven path. No LLM call, no network — the
LLM is mocked at the `_call_llm` boundary.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_scene_keywords as ek  # noqa: E402
import process_video_render_jobs as pv  # noqa: E402


# ── Parser tests ───────────────────────────────────────────────────────

def test_parse_full_spec():
    raw = json.dumps([{
        "subject": "stopwatch ticking hand",
        "shot": "extreme close-up",
        "mood": "urgent, suspenseful",
        "color_palette": "black + neon red",
        "avoid": "people, faces, text, brand logos",
    }])
    out = ek._parse_spec_array(raw)
    assert out is not None
    assert len(out) == 1
    assert out[0]["subject"] == "stopwatch ticking hand"
    assert out[0]["shot"] == "extreme close-up"


def test_parse_partial_spec_filled_with_empty():
    """LLM sometimes returns only `subject` — the rest should be empty
    strings, not absent, so callers can safely `.get()` without None."""
    raw = json.dumps([{"subject": "clock"}])
    out = ek._parse_spec_array(raw)
    assert out is not None
    spec = out[0]
    assert spec["subject"] == "clock"
    assert spec["shot"] == ""
    assert spec["mood"] == ""
    assert spec["color_palette"] == ""
    assert spec["avoid"] == ""


def test_parse_envelope():
    """openclaw --json output is wrapped: {"text": "[...]"}"""
    raw = json.dumps({"text": json.dumps([{"subject": "x", "shot": "y"}])})
    out = ek._parse_spec_array(raw)
    assert out is not None
    assert out[0]["subject"] == "x"


def test_parse_prose_around_json():
    raw = 'Sure! Here is the spec: [{"subject": "x", "shot": "y"}] Hope that helps.'
    out = ek._parse_spec_array(raw)
    assert out is not None
    assert out[0]["subject"] == "x"


def test_parse_empty_returns_none():
    assert ek._parse_spec_array("") is None
    assert ek._parse_spec_array("not json at all") is None


def test_parse_envelope_with_text_field():
    """The openclaw --json envelope wraps the model text in
    {"text": "[{...}]"}. A naive regex that stops at the first `}` would
    pick up the outer envelope object and try to coerce it as a spec
    (giving all-empty fields). Bracket-matching must find the inner
    [...] array instead."""
    raw = json.dumps({
        "runId": "abc",
        "result": {
            "payloads": [
                {"text": '[{"subject": "clock", "shot": "close-up", '
                          '"mood": "", "color_palette": "", "avoid": ""}]'}
            ]
        }
    })
    out = ek._parse_spec_array(raw)
    assert out is not None
    assert len(out) == 1
    assert out[0]["subject"] == "clock"
    assert out[0]["shot"] == "close-up"


def test_parse_non_dict_items_get_coerced():
    """If a list element is not a dict, coerce to empty spec rather than
    crashing — defensive against LLM weirdness."""
    raw = json.dumps([{"subject": "x"}, "bad", 42])
    out = ek._parse_spec_array(raw)
    # "bad" is a string not a dict, so _coerce_spec returns empty fields
    # but the overall structure is still a list of dicts (empty ones are
    # still dicts). The parser accepts it; the caller can filter empties.
    assert out is not None
    assert len(out) == 3
    assert out[0]["subject"] == "x"
    assert out[1] == {f: "" for f in ek.SPEC_FIELDS}
    assert out[2] == {f: "" for f in ek.SPEC_FIELDS}


# ── Cache tests ────────────────────────────────────────────────────────

def test_cache_round_trip():
    """Write a v2 cache, read it back, get the same content. v1 cache
    (no schema_version, list[list[str]]) is treated as a cache miss."""
    job_id = "test_vspec_cache"
    run_dir = Path(tempfile.mkdtemp(prefix="vspec_"))
    try:
        # Bind this job_id to our temp run_dir so the module can find it
        ek.RUNS_DIR.mkdir(exist_ok=True)
        link = ek.RUNS_DIR / job_id
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(run_dir)
        try:
            chunks = ["前 0.5 秒钩住你", "中间每 1.5 秒一个新刺激"]
            theme = "抖音三秒钩子"
            specs = [
                {"subject": "stopwatch", "shot": "close-up", "mood": "urgent",
                 "color_palette": "black + red", "avoid": "people"},
                {"subject": "rhythm pattern", "shot": "wide", "mood": "pulsing",
                 "color_palette": "neon", "avoid": "text"},
            ]
            cache_path = run_dir / "keywords.json"
            cache_path.write_text(json.dumps({
                "schema_version": ek.SCHEMA_VERSION,
                "script_hash": ek._script_hash(chunks, theme),
                "theme": theme,
                "visual_specs": specs,
            }, ensure_ascii=False), encoding="utf-8")

            # Read back
            script_h = ek._script_hash(chunks, theme)
            out = ek._read_cache(cache_path, script_h)
            assert out is not None
            assert out == specs
        finally:
            if link.exists() or link.is_symlink():
                link.unlink()
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def test_v1_cache_invalidated():
    """Old v1 cache (no schema_version, list[list[str]]) should NOT be
    served back as v2 — it's a cache miss so the LLM is re-called."""
    job_id = "test_v1_cache"
    run_dir = Path(tempfile.mkdtemp(prefix="vspec_v1_"))
    try:
        ek.RUNS_DIR.mkdir(exist_ok=True)
        link = ek.RUNS_DIR / job_id
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(run_dir)
        try:
            chunks = ["前 0.5 秒钩住你"]
            theme = "抖音三秒钩子"
            cache_path = run_dir / "keywords.json"
            cache_path.write_text(json.dumps({
                "script_hash": ek._script_hash(chunks, theme),
                "theme": theme,
                "keywords": [["stopwatch", "close-up"]],  # v1 shape
            }, ensure_ascii=False), encoding="utf-8")

            script_h = ek._script_hash(chunks, theme)
            out = ek._read_cache(cache_path, script_h)
            assert out is None, "v1 cache should be invalidated"
        finally:
            if link.exists() or link.is_symlink():
                link.unlink()
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


# ── build_visual_prompt tests ──────────────────────────────────────────

def test_build_visual_prompt_with_spec():
    spec = {
        "subject": "stopwatch ticking hand",
        "shot": "extreme close-up",
        "mood": "urgent, suspenseful",
        "color_palette": "black + neon red",
        "avoid": "people, faces, text",
    }
    prompt = pv.build_visual_prompt("前 0.5 秒钩住你", "抖音三秒钩子", 0, 15, spec=spec)
    # spec subject must appear, Chinese chunk text should NOT (it's
    # replaced by the structured English subject)
    assert "stopwatch ticking hand" in prompt
    assert "extreme close-up" in prompt
    assert "urgent, suspenseful" in prompt
    assert "black + neon red" in prompt
    assert "avoid: people, faces, text" in prompt
    assert "前 0.5 秒钩住你" not in prompt


def test_build_visual_prompt_without_spec_falls_back():
    """No spec → legacy path uses Chinese chunk + rotating shot."""
    prompt = pv.build_visual_prompt("前 0.5 秒钩住你", "抖音三秒钩子", 0, 15)
    assert "前 0.5 秒钩住你" in prompt
    # scene_index=0 → first shot in the rotation
    assert "wide establishing shot" in prompt


def test_build_visual_prompt_partial_spec():
    """spec with only `subject` set should still produce a useful prompt
    (other fields empty, joined with comma + space)."""
    spec = {"subject": "clock", "shot": "", "mood": "",
            "color_palette": "", "avoid": ""}
    prompt = pv.build_visual_prompt("x", "y", 0, 1, spec=spec)
    assert "clock" in prompt
    # Empty fields collapse to ", ," sequences — just verify no crash.
    assert len(prompt) > 0


# ── Realignment tests (trailing-pad scenario) ───────────────────────

def test_real_chunks_aligned_when_pad_trailing():
    """LLM naturally drops empty trailing chunks and returns N_real specs.
    The realignment must map them back to the original chunk positions:
    real chunks get specs, pad chunks stay empty."""
    job_id = "test_realign_pad"
    run_dir = Path(tempfile.mkdtemp(prefix="vspec_realign_"))
    try:
        ek.RUNS_DIR.mkdir(exist_ok=True)
        link = ek.RUNS_DIR / job_id
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(run_dir)
        try:
            # 4 real + 3 empty (mimics the v_5dec0abc case of 10 real + 5 pad)
            chunks = [
                "前 0.5 秒钩住你",
                "中间每 1.5 秒一个新刺激",
                "评论区告诉我你刷到第几秒会划走",
                "三秒定生死",
                "", "", "",
            ]
            theme = "抖音三秒钩子"
            # Stub _call_llm to return 4 specs (one per real chunk)
            captured = {}

            def fake_call(theme_arg, chunks_arg, session_key):
                captured["n_chunks"] = len(chunks_arg)
                captured["session"] = session_key
                return [
                    {"subject": "stopwatch", "shot": "extreme close-up",
                     "mood": "urgent", "color_palette": "black + red",
                     "avoid": "text"},
                    {"subject": "metronome", "shot": "close-up",
                     "mood": "pulsing", "color_palette": "black + white",
                     "avoid": "faces"},
                    {"subject": "comment bubble", "shot": "extreme close-up",
                     "mood": "intimate", "color_palette": "soft + cyan",
                     "avoid": "logos"},
                    {"subject": "stopwatch 2", "shot": "close-up",
                     "mood": "tense", "color_palette": "black",
                     "avoid": "text"},
                ]
            orig = ek._call_llm
            ek._call_llm = fake_call
            try:
                specs = ek.extract_visual_specs(job_id, theme, chunks)
            finally:
                ek._call_llm = orig

            # 7 chunks total → 7 specs (3 pads filled with empty fields)
            assert len(specs) == 7, f"expected 7 specs, got {len(specs)}"
            # First 4 (real chunks) have subjects
            assert specs[0]["subject"] == "stopwatch"
            assert specs[1]["subject"] == "metronome"
            assert specs[2]["subject"] == "comment bubble"
            assert specs[3]["subject"] == "stopwatch 2"
            # Last 3 (pads) are empty
            for i in (4, 5, 6):
                assert specs[i] == {f: "" for f in ek.SPEC_FIELDS}, \
                    f"pad at {i} should be empty, got {specs[i]}"
            # Cache written
            cache_path = run_dir / "keywords.json"
            assert cache_path.exists()
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            assert cached["schema_version"] == ek.SCHEMA_VERSION
            assert len(cached["visual_specs"]) == 7
        finally:
            if link.exists() or link.is_symlink():
                link.unlink()
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def test_wrong_count_falls_back_to_all_empty():
    """If LLM returns the wrong number of specs (e.g. one missing for a
    real chunk), we should NOT silently misalign — fall back to all-empty
    so the caller uses the regex heuristic instead of bogus visuals."""
    job_id = "test_wrong_count"
    run_dir = Path(tempfile.mkdtemp(prefix="vspec_wrong_"))
    try:
        ek.RUNS_DIR.mkdir(exist_ok=True)
        link = ek.RUNS_DIR / job_id
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(run_dir)
        try:
            chunks = ["a", "b", "c", "d", "e"]  # 5 real chunks
            theme = "test"

            def fake_call(theme_arg, chunks_arg, session_key):
                return [{"subject": "x"}]  # only 1, should mismatch

            orig = ek._call_llm
            ek._call_llm = fake_call
            try:
                specs = ek.extract_visual_specs(job_id, theme, chunks)
            finally:
                ek._call_llm = orig

            assert len(specs) == 5
            for s in specs:
                assert s == {f: "" for f in ek.SPEC_FIELDS}
        finally:
            if link.exists() or link.is_symlink():
                link.unlink()
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def test_llm_returns_full_length():
    """LLM sometimes pads its own entries for empty chunks and returns
    len(chunks) specs. We should accept that and 1:1 map, not fall back
    to all-empty (which would be a regression on the working L2 path)."""
    job_id = "test_llm_full"
    run_dir = Path(tempfile.mkdtemp(prefix="vspec_full_"))
    try:
        ek.RUNS_DIR.mkdir(exist_ok=True)
        link = ek.RUNS_DIR / job_id
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(run_dir)
        try:
            chunks = ["前 0.5 秒钩住你", "中间刺激", "", ""]
            theme = "test"

            def fake_call(theme_arg, chunks_arg, session_key):
                # LLM returns 4 specs (matches chunks length) — sometimes
                # it pads its own for blank chunks
                return [
                    {"subject": "stopwatch", "shot": "close-up",
                     "mood": "urgent", "color_palette": "black", "avoid": "text"},
                    {"subject": "metronome", "shot": "close-up",
                     "mood": "pulsing", "color_palette": "black", "avoid": "faces"},
                    {"subject": "blank fill", "shot": "",
                     "mood": "", "color_palette": "", "avoid": ""},
                    {"subject": "blank fill", "shot": "",
                     "mood": "", "color_palette": "", "avoid": ""},
                ]

            orig = ek._call_llm
            ek._call_llm = fake_call
            try:
                specs = ek.extract_visual_specs(job_id, theme, chunks)
            finally:
                ek._call_llm = orig

            assert len(specs) == 4
            # Positional 1:1 — no realignment needed
            assert specs[0]["subject"] == "stopwatch"
            assert specs[1]["subject"] == "metronome"
            assert specs[2]["subject"] == "blank fill"
            assert specs[3]["subject"] == "blank fill"
        finally:
            if link.exists() or link.is_symlink():
                link.unlink()
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_parse_full_spec,
        test_parse_partial_spec_filled_with_empty,
        test_parse_envelope,
        test_parse_prose_around_json,
        test_parse_empty_returns_none,
        test_parse_non_dict_items_get_coerced,
        test_parse_envelope_with_text_field,
        test_cache_round_trip,
        test_v1_cache_invalidated,
        test_build_visual_prompt_with_spec,
        test_build_visual_prompt_without_spec_falls_back,
        test_build_visual_prompt_partial_spec,
        test_real_chunks_aligned_when_pad_trailing,
        test_wrong_count_falls_back_to_all_empty,
        test_llm_returns_full_length,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
