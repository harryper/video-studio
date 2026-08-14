#!/usr/bin/env python3
"""Unit + integration tests for the editorial engine.

Run: python3 scripts/test_editorial_engine.py
"""
import contextlib
import json as _json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_script_jobs as psj  # noqa: E402


@contextlib.contextmanager
def monkeypatch_attr(obj, name, value):
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


# ── Task 1: validate_editorial_request ─────────────────────────

def test_validate_accepts_empty_data():
    """空 body → 全默认。"""
    req, err = psj.validate_editorial_request({})
    assert err is None, f"unexpected err: {err}"
    assert req == {
        "audience": "普通用户",
        "tone": "auto",
        "angle": "auto",
        "goal": "balanced",
        "fact_strictness": "standard",
    }


def test_validate_accepts_known_enums():
    """合法 enum → 原样回填。"""
    data = {"audience": "对历史感兴趣的普通用户", "tone": "冷峻", "goal": "completion",
            "fact_strictness": "high", "angle": "英国战时配给本"}
    req, err = psj.validate_editorial_request(data)
    assert err is None
    assert req["audience"] == "对历史感兴趣的普通用户"
    assert req["tone"] == "冷峻"
    assert req["goal"] == "completion"
    assert req["fact_strictness"] == "high"
    assert req["angle"] == "英国战时配给本"


def test_validate_rejects_bad_tone():
    """非法 tone → (None, err_msg)。"""
    req, err = psj.validate_editorial_request({"tone": "嘻哈"})
    assert req is None and err, "expected reject for bad tone"


def test_validate_rejects_bad_goal():
    """非法 goal → 拒绝。"""
    req, err = psj.validate_editorial_request({"goal": "viral"})
    assert req is None and err


def test_validate_rejects_overlong_audience():
    """audience 超过 80 字 → 拒绝。"""
    req, err = psj.validate_editorial_request({"audience": "x" * 81})
    assert req is None and err


def test_validate_rejects_non_string_audience():
    """非字符串 audience → 拒绝。"""
    req, err = psj.validate_editorial_request({"audience": 123})
    assert req is None and err


# ── Task 2: story_brief ─────────────────────────────────────────

VALID_BRIEF = {
    "content_lane": "historical_power",
    "candidate_angles": [
        {"angle": "英国战时配给本", "core_thesis": "糖是近代国家最易运输和分配的人体燃料",
         "why_it_spreads": "从日常茶杯反转到战争物流"},
        {"angle": "工业工人的茶杯", "core_thesis": "糖帮助工业社会以低成本补充劳动力热量",
         "why_it_spreads": "把工业革命从煤炭叙事翻转为人体能源叙事"},
        {"angle": "甘蔗田与帝国贸易", "core_thesis": "糖的供应链塑造了殖民时代的权力关系",
         "why_it_spreads": "日常消费品背后有巨大的地理与人力代价"},
    ],
    "chosen_angle": "英国战时配给本",
    "core_thesis": "糖是近代国家最易运输和分配的人体燃料",
    "audience_misconception": "糖只是调味品",
    "opening_scene": "英国人翻开配给本, 糖和黄油、培根同批被限制",
    "evidence_chain": [
        "英国依赖进口, 战时海运受威胁",
        "糖耐储存、易运输、易按人头配给",
        "糖能快速补充体力, 因此被纳入战争后勤",
    ],
    "twist": "被保卫的不是甜食, 而是可立刻转成体力的热量",
    "visual_anchors": ["配给本", "港口麻袋", "工人的茶杯"],
    "risk_claims": [
        {"claim": "糖是最早被配给的食品", "risk": "high",
         "instruction": "禁止如此表述; 需说明糖与培根、黄油同批进入配给"},
    ],
}


def test_parse_story_brief_accepts_valid():
    """合法 brief → 解析成功, 字段全部原样回填。"""
    brief, err = psj.parse_story_brief(_json.dumps(VALID_BRIEF))
    assert err is None, f"unexpected err: {err}"
    assert brief["content_lane"] == "historical_power"
    assert len(brief["candidate_angles"]) == 3
    assert brief["chosen_angle"] == "英国战时配给本"


def test_parse_story_brief_rejects_too_few_candidates():
    """< 3 个候选角度 → 拒绝。"""
    bad = dict(VALID_BRIEF)
    bad["candidate_angles"] = VALID_BRIEF["candidate_angles"][:2]
    brief, err = psj.parse_story_brief(_json.dumps(bad))
    assert brief is None and err


def test_parse_story_brief_rejects_duplicate_angles():
    """三个 angle 文本重复 → 拒绝。"""
    bad = dict(VALID_BRIEF)
    bad["candidate_angles"] = [
        {"angle": "重复角度", "core_thesis": "a", "why_it_spreads": "b"},
        {"angle": "重复角度", "core_thesis": "c", "why_it_spreads": "d"},
        {"angle": "重复角度", "core_thesis": "e", "why_it_spreads": "f"},
    ]
    brief, err = psj.parse_story_brief(_json.dumps(bad))
    assert brief is None and err


def test_parse_story_brief_rejects_bad_lane():
    """非法 content_lane → 拒绝。"""
    bad = dict(VALID_BRIEF)
    bad["content_lane"] = "conspiracy"
    brief, err = psj.parse_story_brief(_json.dumps(bad))
    assert brief is None and err


def test_parse_story_brief_rejects_short_evidence():
    """evidence_chain 任一条 < 8 字 (纯名词列表) → 拒绝。"""
    bad = dict(VALID_BRIEF)
    bad["evidence_chain"] = ["abc", "糖, 盐, 油, 茶", "w"]  # 全 < 8 字
    brief, err = psj.parse_story_brief(_json.dumps(bad))
    assert brief is None and err


def test_generate_story_brief_preserves_user_angle():
    """用户 angle != auto → brief.chosen_angle 必须等于用户输入。"""
    user_request = {"angle": "我的固定切入角度"}
    with monkeypatch_attr(psj.llm_client, "complete",
                          lambda **kw: _json.dumps(VALID_BRIEF)):
        brief, err = psj.generate_story_brief(
            {"id": "v_x", "theme": "糖"}, user_request,
        )
    assert err is None
    assert brief["chosen_angle"] == "我的固定切入角度"


def test_generate_story_brief_returns_err_on_bad_json():
    """LLM 返回非 JSON → (None, err_msg)。"""
    with monkeypatch_attr(psj.llm_client, "complete",
                          lambda **kw: "not json"):
        brief, err = psj.generate_story_brief({"id": "v_x", "theme": "糖"}, {})
    assert brief is None and err


# ── Task 3: quality_report ──────────────────────────────────────

VALID_QUALITY = {
    "score": 84,
    "dimensions": {
        "factual_safety": 18,
        "distinctive_angle": 18,
        "opening_hook": 16,
        "causal_clarity": 17,
        "spoken_rhythm": 8,
        "ending_payoff": 7,
    },
    "must_fix": [
        {"category": "factual_safety", "instruction": "将「最早配给」改为「与培根、黄油同批进入配给」"},
    ],
    "strengths": ["开场具有明确物件和冲突"],
    "verdict": "repair",
}


def test_parse_quality_accepts_valid():
    """合法 → 解析成功; 维度总分与 score 匹配 (容差 ±1)。"""
    rep, err = psj.parse_quality_report(_json.dumps(VALID_QUALITY))
    assert err is None, f"unexpected err: {err}"
    assert rep["score"] == 84
    assert rep["verdict"] == "repair"
    assert len(rep["must_fix"]) == 1


def test_parse_quality_rejects_dimension_mismatch():
    """维度总分 ≠ score (超出 ±1) → 拒绝。"""
    bad = {
        "score": 84,
        "dimensions": {"factual_safety": 18, "distinctive_angle": 18,
                       "opening_hook": 5, "causal_clarity": 17,
                       "spoken_rhythm": 8, "ending_payoff": 7},  # sum=73
        "must_fix": [], "strengths": [], "verdict": "pass",
    }
    rep, err = psj.parse_quality_report(_json.dumps(bad))
    assert rep is None and err


def test_parse_quality_rejects_bad_verdict():
    """非法 verdict → 拒绝。"""
    bad = dict(VALID_QUALITY)
    bad["verdict"] = "unknown"
    rep, err = psj.parse_quality_report(_json.dumps(bad))
    assert rep is None and err


def test_parse_quality_rejects_empty_must_fix_fields():
    """must_fix 项缺 category / instruction → 拒绝。"""
    bad = dict(VALID_QUALITY)
    bad["must_fix"] = [{"category": "", "instruction": "x"}]
    rep, err = psj.parse_quality_report(_json.dumps(bad))
    assert rep is None and err


def test_generate_quality_report_passes_through_brief_and_script():
    """生成 prompt 包含 brief core_thesis + script 全文。"""
    captured = {}
    def fake_complete(**kw):
        captured["user"] = kw.get("user", "")
        return _json.dumps(VALID_QUALITY)
    with monkeypatch_attr(psj.llm_client, "complete", fake_complete):
        rep, err = psj.generate_quality_report(
            {"id": "v_x", "theme": "糖"},
            VALID_BRIEF,
            "糖是战略物资。配给本上写着...",
        )
    assert err is None
    assert "糖是近代国家最易运输和分配的人体燃料" in captured["user"]
    assert "配给本上写着" in captured["user"]


# ── Task 4: repair ──────────────────────────────────────────────

SAMPLE_REPAIR_SCRIPT = "糖不是调味品, 是战略物资. 配给本上有糖与培根、黄油同批被限制. " * 5  # 长到 110+ 字
SAMPLE_REPAIR_COVER = {"main": "糖不是调味品", "main_highlight": [1, 3],
                       "sub": "二战真相比你想的更狠"}


def test_build_repair_prompt_includes_must_fix():
    """每条 must_fix.instruction 必须出现在 repair prompt. """
    report = {
        "score": 60, "verdict": "repair",
        "dimensions": {"factual_safety": 8, "distinctive_angle": 12,
                       "opening_hook": 10, "causal_clarity": 10,
                       "spoken_rhythm": 8, "ending_payoff": 12},
        "must_fix": [
            {"category": "factual_safety", "instruction": "将「最早配给」改为「与培根同批进入配给」"},
            {"category": "opening_hook", "instruction": "前 3 句必须出现配给本"},
        ],
        "strengths": [],
    }
    prompt = psj.build_editorial_repair_prompt(
        {"theme": "糖", "render": {"duration_sec": 60}},
        VALID_BRIEF, report, "糖是战略物资. 最早配给的...", 300, 1200, "已超出下限",
    )
    assert "将「最早配给」改为「与培根同批进入配给」" in prompt, \
        "must_fix[0].instruction missing"
    assert "前 3 句必须出现配给本" in prompt, \
        "must_fix[1].instruction missing"
    assert "糖是近代国家最易运输和分配的人体燃料" in prompt, \
        "core_thesis missing"


def test_build_repair_prompt_includes_length_gap():
    """length_gap_str 必须出现, 让 LLM 知道是扩写还是删减."""
    prompt = psj.build_editorial_repair_prompt(
        {"theme": "x", "render": {"duration_sec": 60}},
        VALID_BRIEF, {"score": 60, "verdict": "repair",
                      "dimensions": {}, "must_fix": [], "strengths": []},
        "短脚本", 300, 1200, "当前 50 字, 比下限少 250 字. 需要扩写",
    )
    assert "当前 50 字" in prompt
    assert "扩写" in prompt


def test_generate_repair_pass_returns_script_and_cover():
    """LLM 返回合法 JSON → (script, cover, None)。"""
    repair_response = _json.dumps({
        "script": SAMPLE_REPAIR_SCRIPT,
        "cover": SAMPLE_REPAIR_COVER,
    })
    with monkeypatch_attr(psj.llm_client, "complete",
                          lambda **kw: repair_response):
        script, cover, err = psj.generate_repair_pass(
            {"id": "v_x", "theme": "糖", "render": {"duration_sec": 60}},
            VALID_BRIEF,
            {"score": 60, "verdict": "repair",
             "dimensions": {"factual_safety": 8, "distinctive_angle": 12,
                            "opening_hook": 10, "causal_clarity": 10,
                            "spoken_rhythm": 8, "ending_payoff": 12},
             "must_fix": [], "strengths": []},
            "旧脚本", 300, 1200, "已超出",
        )
    assert err is None
    assert "糖不是调味品" in script
    assert cover == SAMPLE_REPAIR_COVER


# ── Minimal test runner (后续 task 会扩展这里) ─────────────────────

def _run_all_tests():
    """收集并运行本文件中所有 test_* 函数, 按字典序返回结果。"""
    import inspect
    here = Path(__file__).resolve()
    tests = []
    for name, fn in inspect.getmembers(sys.modules[__name__], inspect.isfunction):
        if name.startswith("test_") and fn.__module__ == __name__:
            tests.append((name, fn))
    tests.sort()
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
            failed.append(name)
    print()
    print(f"Total: {len(tests)} | Passed: {len(tests) - len(failed)} | Failed: {len(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(_run_all_tests())