#!/usr/bin/env python3
"""Unit tests for build_shotlist + shotlist schema (cat-doctor 6 类构图).

Run: python3 scripts/test_shotlist_schema.py

No LLM call, no network — the LLM is mocked at the call_llm boundary.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_director_jobs as dj  # noqa: E402
import extract_scene_keywords as ek  # noqa: E402


def _stub_shot(scene_index: int, chunk: str) -> dict:
    return {
        "scene_index": scene_index,
        "chunk": chunk,
        "composition": "hook" if scene_index == 0 else "process",
        "action": f"歪头看 chunk {scene_index}",
        "annotations": [
            {"text": f"注 {scene_index}", "color": "red"},
            {"text": f"label {scene_index}", "color": "blue"},
        ],
    }


def _stub_call_llm(theme, chunks, session_key):  # noqa: ARG001
    return [_stub_shot(i, c) for i, c in enumerate(chunks) if c.strip()]


def test_build_shotlist_returns_dict_with_schema_version():
    chunks = ["第一句内容", "第二句内容"]
    shotlist = dj.build_shotlist("v_test01", "测试主题", chunks, _llm=_stub_call_llm)
    assert shotlist["schema_version"] == ek.SHOTLIST_SCHEMA_VERSION
    assert shotlist["theme"] == "测试主题"
    assert "script_hash" in shotlist and len(shotlist["script_hash"]) == 16
    assert "shots" in shotlist and isinstance(shotlist["shots"], list)


def test_shots_length_equals_chunks_with_pad_nulls():
    chunks = ["A", "", "B", "C"]
    shotlist = dj.build_shotlist("v_test02", "t", chunks, _llm=_stub_call_llm)
    assert len(shotlist["shots"]) == len(chunks)
    assert shotlist["shots"][1] is None
    assert shotlist["shots"][0] is not None
    assert shotlist["shots"][2] is not None
    assert shotlist["shots"][3] is not None


def test_shots_have_required_fields_and_prompt():
    chunks = ["讲讲西瓜", ""]
    shotlist = dj.build_shotlist("v_test03", "水果", chunks, _llm=_stub_call_llm)
    shot = shotlist["shots"][0]
    assert shot is not None
    for key in ("scene_index", "chunk", "composition", "action", "annotations",
                "prompt", "negative_prompt"):
        assert key in shot, f"shot 缺字段 {key}"
    assert isinstance(shot["annotations"], list)
    assert shot["prompt"]
    assert shot["negative_prompt"]


def test_composition_must_be_in_allowed_enum():
    chunks = ["x"]
    bad_shot = {
        "scene_index": 0, "chunk": "x",
        "composition": "INVALID", "action": "y", "annotations": [],
    }

    def _bad_llm(theme, chunks, session_key):  # noqa: ARG001
        return [bad_shot]

    shotlist = dj.build_shotlist("v_test04", "t", chunks, _llm=_bad_llm)
    assert shotlist["shots"][0]["composition"] in ek.SHOTLIST_COMPOSITIONS


def test_annotation_colors_must_be_in_allowed_set():
    chunks = ["x"]
    bad_shot = {
        "scene_index": 0, "chunk": "x",
        "composition": "hook", "action": "y",
        "annotations": [{"text": "?", "color": "purple"}],
    }

    def _bad_llm(theme, chunks, session_key):  # noqa: ARG001
        return [bad_shot]

    shotlist = dj.build_shotlist("v_test05", "t", chunks, _llm=_bad_llm)
    shot = shotlist["shots"][0]
    for ann in shot["annotations"]:
        assert ann["color"] in ek.SHOTLIST_ANNOTATION_COLORS


def test_llm_failure_produces_minimal_shots_not_none():
    """director 降级: LLM 挂掉 → 产 action=chunk 摘要、无批注的最简 shot, 不阻断流水线。"""

    def _broken_llm(theme, chunks, session_key):  # noqa: ARG001
        return None

    chunks = ["A 句", "B 句", "C 句"]
    shotlist = dj.build_shotlist("v_test06", "t", chunks, _llm=_broken_llm)
    assert shotlist is not None
    assert len(shotlist["shots"]) == len(chunks)
    for shot in shotlist["shots"]:
        assert shot is not None
        assert shot["action"]
        assert shot["composition"] in ek.SHOTLIST_COMPOSITIONS


def test_cache_hit_skips_llm():
    with tempfile.TemporaryDirectory() as tmpdir:
        runs_dir = Path(tmpdir) / "runs"
        runs_dir.mkdir()
        job_id = "v_cachetest"

        def _first_llm(theme, chunks, session_key):  # noqa: ARG001
            return [_stub_shot(i, c) for i, c in enumerate(chunks) if c.strip()]

        original_runs = dj.RUNS_DIR
        dj.RUNS_DIR = runs_dir
        try:
            sl1 = dj.build_shotlist(job_id, "t", ["hello"], _llm=_first_llm)
            script_hash = ek._script_hash(["hello"], "t")
            cached_file = runs_dir / job_id / "shotlist.json"
            cached_file.parent.mkdir(parents=True, exist_ok=True)
            cached_file.write_text(json.dumps({
                "schema_version": ek.SHOTLIST_SCHEMA_VERSION,
                "script_hash": script_hash,
                "theme": "t",
                "shots": sl1["shots"],
            }, ensure_ascii=False), encoding="utf-8")

            call_count = {"n": 0}

            def _second_llm(theme, chunks, session_key):  # noqa: ARG001
                call_count["n"] += 1
                return None

            sl2 = dj.build_shotlist(job_id, "t", ["hello"], _llm=_second_llm)
            assert call_count["n"] == 0, "缓存命中时不该调 LLM"
            assert sl2["shots"] == sl1["shots"]
        finally:
            dj.RUNS_DIR = original_runs


def test_force_refresh_skips_cache():
    with tempfile.TemporaryDirectory() as tmpdir:
        runs_dir = Path(tmpdir) / "runs"
        runs_dir.mkdir()
        job_id = "v_forceref"
        shotlist_path = runs_dir / job_id / "shotlist.json"
        shotlist_path.parent.mkdir(parents=True, exist_ok=True)
        shotlist_path.write_text(json.dumps({
            "schema_version": ek.SHOTLIST_SCHEMA_VERSION,
            "script_hash": "STALE",
            "theme": "t",
            "shots": [],
        }, ensure_ascii=False), encoding="utf-8")

        original_runs = dj.RUNS_DIR
        dj.RUNS_DIR = runs_dir
        try:
            call_count = {"n": 0}

            def _llm(theme, chunks, session_key):  # noqa: ARG001
                call_count["n"] += 1
                return [_stub_shot(i, c) for i, c in enumerate(chunks) if c.strip()]

            dj.build_shotlist(job_id, "t", ["hello"], force_refresh=True, _llm=_llm)
            assert call_count["n"] == 1, "force_refresh=True 必须绕过缓存"
        finally:
            dj.RUNS_DIR = original_runs


if __name__ == "__main__":
    test_build_shotlist_returns_dict_with_schema_version()
    test_shots_length_equals_chunks_with_pad_nulls()
    test_shots_have_required_fields_and_prompt()
    test_composition_must_be_in_allowed_enum()
    test_annotation_colors_must_be_in_allowed_set()
    test_llm_failure_produces_minimal_shots_not_none()
    test_cache_hit_skips_llm()
    test_force_refresh_skips_cache()
    print("\n✅ all 8 shotlist_schema tests passed")