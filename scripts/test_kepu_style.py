#!/usr/bin/env python3
"""Unit tests for kepu-style prompt + lint_script.

Run: python3 scripts/test_kepu_style.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_script_jobs as psj  # noqa: E402


def test_lint_script_clean():
    """A clean script returns []."""
    text = "海水是咸的，因为河把盐冲进了海里，就这么简单。"
    assert psj.lint_script(text) == [], f"clean script linted: {psj.lint_script(text)}"


def test_lint_script_ai_word():
    """AI 黑名单词触发 'ai_word' 命中。"""
    text = "此外，海水含有大量盐分，扮演着重要角色。"
    hits = psj.lint_script(text)
    assert "ai_word" in hits, f"expected 'ai_word' in {hits}"


def test_lint_script_long_clause():
    """单 clause > 20 字触发 'long_clause'。"""
    text = "海水之所以是咸的是因为河流长期不断地冲刷地表岩石并将其中溶解的矿物质带入海洋中"
    hits = psj.lint_script(text)
    assert "long_clause" in hits, f"expected 'long_clause' in {hits}"


def test_lint_script_banned_pattern():
    """三段式排比/否定式排比触发 'banned_pattern'。"""
    text = "它是大海的眼泪，是地球的血液，也是时间的印记。"
    hits = psj.lint_script(text)
    assert "banned_pattern" in hits, f"expected 'banned_pattern' in {hits}"


def test_lint_script_banned_ending():
    """强行升华结尾触发 'banned_ending'。"""
    text = "海水的咸味让我们明白，这告诉我们大自然的力量值得深思。"
    hits = psj.lint_script(text)
    assert "banned_ending" in hits, f"expected 'banned_ending' in {hits}"


def test_lint_script_empty_and_none():
    """鲁棒性：空串和 None 返回空列表，不抛异常。"""
    assert psj.lint_script("") == []
    assert psj.lint_script(None) == []


def test_build_prompt_has_narrative_skeleton():
    """build_prompt 必须包含 NARRATIVE_SKELETON 的 5 阶段标记。"""
    job = {"theme": "测试", "render": {"duration_sec": 60}}
    prompt = psj.build_prompt(job)
    for marker in ["钩子", "认知缺口", "逐层揭秘", "反转", "落点"]:
        assert marker in prompt, f"prompt missing skeleton marker: {marker}"


def test_build_prompt_has_voice_guide():
    """build_prompt 必须包含 VOICE_GUIDE 关键词 (有观点/具体优先于抽象)."""
    job = {"theme": "测试", "render": {"duration_sec": 60}}
    prompt = psj.build_prompt(job)
    assert "有观点" in prompt, "missing voice: 有观点"
    assert "具体优先于抽象" in prompt, "missing voice: 具体优先于抽象"


def test_build_prompt_has_anti_ai_rules():
    """build_prompt 必须包含 ANTI_AI_RULES 黑名单词样本 + 禁止句式类别。"""
    job = {"theme": "测试", "render": {"duration_sec": 60}}
    prompt = psj.build_prompt(job)
    for word in ["此外", "至关重要", "扮演着重要角色"]:
        assert word in prompt, f"missing AI word in blacklist: {word}"
    assert "三段式排比" in prompt, "missing banned-pattern category"


def test_build_prompt_has_clause_rules():
    """build_prompt 必须包含 CLAUSE_RULES 的 v9 断句硬数字 (20 字 / 12 字)."""
    job = {"theme": "测试", "render": {"duration_sec": 60}}
    prompt = psj.build_prompt(job)
    assert "20 字" in prompt or "20字" in prompt, "missing max-clause 20-char marker"
    assert "2-12" in prompt or "2-12 字" in prompt, "missing ideal-clause 2-12 marker"


def test_build_prompt_preserves_cover_and_format():
    """COVER_INSTRUCTIONS 和 JSON 输出格式必须保留 (不改 cover)."""
    job = {"theme": "测试", "render": {"duration_sec": 60}}
    prompt = psj.build_prompt(job)
    assert "main_highlight" in prompt, "cover marker 'main_highlight' missing"
    assert '"script":' in prompt, "JSON output format marker '\"script\":' missing"
    assert '"cover":' in prompt, "JSON output format marker '\"cover\":' missing"


def test_gold_script_passes_lint():
    """手写 gold 稿 (海水为什么是咸的) 必须 lint_script 返回 []."""
    gold = (
        "海水是咸的, 但你可能没想到, 罪魁祸首根本不是海.\n"
        "一公斤海水里, 溶了 35 克盐, 全球海洋加起来有 50 万吨.\n"
        "你以为盐一直就在海里, 其实不是. 海本来是淡水.\n"
        "盐是河冲进来的. 雨水落到山上, 一点点溶解矿物质, "
        "然后汇成河, 一路冲进海.\n"
        "海里的水蒸发, 盐留下, 越攒越多, 一攒就是几十亿年.\n"
        "所以下次喝到咸水, 别骂海, 骂山."
    )
    hits = psj.lint_script(gold)
    assert hits == [], f"gold script lint hits (should be []): {hits}"


def test_gold_script_lacks_banned_opening():
    """gold 稿首句不允许是冷启动套话 ('今天我们'/'你有没有想过').
    这条不属于 lint_script 自动规则, 但作为风格锚点保留断言."""
    gold = "海水是咸的, 但你可能没想到, 罪魁祸首根本不是海."
    banned_openings = ["今天我们", "你有没有想过", "让我们一起", "接下来我要"]
    for op in banned_openings:
        assert not gold.startswith(op), f"gold opens with banned phrase: {op}"


def main():
    tests = [
        test_lint_script_clean,
        test_lint_script_ai_word,
        test_lint_script_long_clause,
        test_lint_script_banned_pattern,
        test_lint_script_banned_ending,
        test_lint_script_empty_and_none,
        test_build_prompt_has_narrative_skeleton,
        test_build_prompt_has_voice_guide,
        test_build_prompt_has_anti_ai_rules,
        test_build_prompt_has_clause_rules,
        test_build_prompt_preserves_cover_and_format,
        test_gold_script_passes_lint,
        test_gold_script_lacks_banned_opening,
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