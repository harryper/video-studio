#!/usr/bin/env python3
"""Unit tests for MEME_GUIDE prompt integration.

Run: python3 scripts/test_meme_guide.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_script_jobs as psj  # noqa: E402


def test_old_constants_removed():
    """3 old string constants must be deleted."""
    assert not hasattr(psj, "GOOD_EXAMPLES"), "GOOD_EXAMPLES still exists, should be removed"
    assert not hasattr(psj, "HOOK_TEMPLATES"), "HOOK_TEMPLATES still exists, should be removed"
    assert not hasattr(psj, "ANTI_PATTERNS"), "ANTI_PATTERNS still exists, should be removed"
    print("✓ 3 old string constants removed (GOOD_EXAMPLES / HOOK_TEMPLATES / ANTI_PATTERNS)")


def test_meme_guide_defined():
    """MEME_GUIDE must exist as a module-level string."""
    assert hasattr(psj, "MEME_GUIDE"), "MEME_GUIDE not defined"
    guide = psj.MEME_GUIDE
    assert isinstance(guide, str), f"MEME_GUIDE must be str, got {type(guide)}"
    assert len(guide) > 500, f"MEME_GUIDE too short ({len(guide)} chars), expected >500"
    # Must contain key content
    assert "reference-memes.md" in guide, "MEME_GUIDE must reference reference-memes.md"
    assert "不仿写" in guide, "MEME_GUIDE must say '不仿写'"
    assert "不自创" in guide, "MEME_GUIDE must say '不自创'"
    assert "不改字" in guide, "MEME_GUIDE must say '不改字'"
    assert "路易十六" in guide, "MEME_GUIDE must list 种子 路易十六"
    assert "魏忠贤" in guide, "MEME_GUIDE must list 种子 魏忠贤"
    assert "夏侯惇" in guide, "MEME_GUIDE must list 种子 夏侯惇"
    print(f"✓ MEME_GUIDE defined ({len(guide)} chars) and contains 7 required substrings")


def test_cover_instructions_preserved():
    """COVER_INSTRUCTIONS must be preserved (out of scope of this refactor)."""
    assert hasattr(psj, "COVER_INSTRUCTIONS"), "COVER_INSTRUCTIONS must be preserved"
    assert "main_highlight" in psj.COVER_INSTRUCTIONS, "COVER_INSTRUCTIONS content changed unexpectedly"
    print("✓ COVER_INSTRUCTIONS preserved unchanged")


def test_build_prompt_references_reference_memes():
    """build_prompt() output must reference reference-memes.md."""
    fake_job = {
        "id": "v_test_001",
        "theme": "test theme",
        "render": {"duration_sec": 110},
    }
    prompt = psj.build_prompt(fake_job)
    assert "reference-memes.md" in prompt, "build_prompt must reference reference-memes.md"
    print("✓ build_prompt() references reference-memes.md")


def test_build_prompt_includes_meme_guide():
    """build_prompt() output must include MEME_GUIDE content (verbatim)."""
    fake_job = {
        "id": "v_test_001",
        "theme": "test theme",
        "render": {"duration_sec": 110},
    }
    prompt = psj.build_prompt(fake_job)
    # MEME_GUIDE should be embedded in the prompt
    assert "MEME_GUIDE" not in prompt or "网络热梗 + 古人 PUN 段子" in prompt, \
        "build_prompt should include MEME_GUIDE content"
    assert "网络热梗 + 古人 PUN 段子" in prompt, "MEME_GUIDE header missing"
    assert "挑合适的直接用" in prompt, "MEME_GUIDE usage rule missing"
    print("✓ build_prompt() includes MEME_GUIDE content")


def test_build_prompt_hard_constraint_4_modified():
    """硬约束 #4 must add '可叠加网络热梗或古人 PUN 段子'."""
    fake_job = {
        "id": "v_test_001",
        "theme": "test theme",
        "render": {"duration_sec": 110},
    }
    prompt = psj.build_prompt(fake_job)
    assert "可叠加网络热梗或古人 PUN 段子" in prompt, \
        "硬约束 #4 should mention '可叠加网络热梗或古人 PUN 段子'"
    print("✓ 硬约束 #4 modified to allow hot meme overlay")


def test_build_prompt_hard_constraint_8_new():
    """硬约束 #8 (NEW) must encode the verbatim-use rule."""
    fake_job = {
        "id": "v_test_001",
        "theme": "test theme",
        "render": {"duration_sec": 110},
    }
    prompt = psj.build_prompt(fake_job)
    # Look for the new #8 硬约束 - check for the exact wording
    expected_substring = "不改字不仿写不自创"
    assert expected_substring in prompt, \
        f"硬约束 #8 should contain '{expected_substring}'"
    # Also verify it says "不强求密度"
    assert "不强求密度" in prompt, "硬约束 #8 should mention '不强求密度'"
    # And 同一人物最多 1 次
    assert "同一人物最多 1 次" in prompt or "同一人物最多" in prompt, \
        "硬约束 #8 should mention '同一人物最多 1 次'"
    print("✓ 硬约束 #8 (NEW) present with verbatim-use rule")


def test_build_prompt_self_check_9_modified():
    """硬约束 #9 (写完自检, was #8) must add '段子是否一字不改' check."""
    fake_job = {
        "id": "v_test_001",
        "theme": "test theme",
        "render": {"duration_sec": 110},
    }
    prompt = psj.build_prompt(fake_job)
    assert "段子是否一字不改嵌入" in prompt, \
        "硬约束 #9 (写完自检) must add '段子是否一字不改嵌入' check"
    print("✓ 硬约束 #9 (写完自检) modified to add 段子 quality check")


if __name__ == "__main__":
    test_old_constants_removed()
    test_meme_guide_defined()
    test_cover_instructions_preserved()
    test_build_prompt_references_reference_memes()
    test_build_prompt_includes_meme_guide()
    test_build_prompt_hard_constraint_4_modified()
    test_build_prompt_hard_constraint_8_new()
    test_build_prompt_self_check_9_modified()
    print("\n所有 MEME_GUIDE prompt 集成测试通过")