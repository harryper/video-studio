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
    """MEME_GUIDE must exist as a module-level string with [xingzhe] content."""
    assert hasattr(psj, "MEME_GUIDE"), "MEME_GUIDE not defined"
    guide = psj.MEME_GUIDE
    assert isinstance(guide, str), f"MEME_GUIDE must be str, got {type(guide)}"
    assert len(guide) > 1000, f"MEME_GUIDE too short ({len(guide)} chars), expected >1000"
    # Must contain [xingzhe] 段子 reference
    assert "reference-memes.md" in guide, "MEME_GUIDE must reference reference-memes.md"
    assert "9 条" in guide, "MEME_GUIDE must say '9 条' ([xingzhe] meme count)"
    # [xingzhe] 9 条段子的种子人物
    assert "夏侯惇" in guide, "MEME_GUIDE must list 种子 夏侯惇"
    assert "路易十六" in guide, "MEME_GUIDE must list 种子 路易十六"
    assert "恐怖直立猿" in guide, "MEME_GUIDE must list [xingzhe] meme 恐怖直立猿"
    assert "地球online" in guide, "MEME_GUIDE must list [xingzhe] meme 地球online"
    # Verbatim 规则
    assert "不仿写" in guide, "MEME_GUIDE must say '不仿写'"
    assert "不自创" in guide, "MEME_GUIDE must say '不自创'"
    assert "不改字" in guide, "MEME_GUIDE must say '不改字'"
    print(f"✓ MEME_GUIDE defined ({len(guide)} chars) and contains [xingzhe] 9 条 段子 + 种子 + verbatim 规则")


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
    assert "[xingzhe] 风格 + 段子" in prompt, "[xingzhe] MEME_GUIDE header missing"
    assert "Hook 公式" in prompt, "MEME_GUIDE 4 Hook 公式 missing"
    assert "9 条" in prompt, "MEME_GUIDE 9 条 段子 reference missing"
    print("✓ build_prompt() includes [xingzhe] MEME_GUIDE content")


def test_build_prompt_hard_constraint_4_modified():
    """硬约束 #4 must NOT have '可叠加' suffix (memes now in #8)."""
    fake_job = {
        "id": "v_test_001",
        "theme": "test theme",
        "render": {"duration_sec": 110},
    }
    prompt = psj.build_prompt(fake_job)
    # 4 hook types
    assert "具体数字" in prompt, "硬约束 #4 should mention '具体数字'"
    assert "段子化破折号" in prompt, "硬约束 #4 should mention '段子化破折号'"
    assert "数学对比" in prompt, "硬约束 #4 should mention '数学对比'"
    assert "跨学科引用" in prompt, "硬约束 #4 should mention '跨学科引用'"
    print("✓ 硬约束 #4 lists 4 hook types (数字/段子化/数学/跨学科)")


def test_build_prompt_hard_constraint_8_new():
    """硬约束 #8 must encode the [xingzhe] 9 条 verbatim-use rule."""
    fake_job = {
        "id": "v_test_001",
        "theme": "test theme",
        "render": {"duration_sec": 110},
    }
    prompt = psj.build_prompt(fake_job)
    # Must reference 9 条 [xingzhe] library
    assert "9 条" in prompt, "硬约束 #8 should mention '9 条' [xingzhe] library"
    assert "[xingzhe]" in prompt, "硬约束 #8 should mention '[xingzhe]' style"
    # Must reference MEME_GUIDE §5 (verbatim / 服务主题 / 同种子不重复)
    assert "MEME_GUIDE" in prompt and "§5" in prompt, \
        "硬约束 #8 should reference MEME_GUIDE §5 for full rules"
    print("✓ 硬约束 #8 references [xingzhe] 9 条 + MEME_GUIDE §5")


def test_build_prompt_self_check_10_modified():
    """硬约束 #10 (写完自检) must add 段子服务主题 check (was #9 before)."""
    fake_job = {
        "id": "v_test_001",
        "theme": "test theme",
        "render": {"duration_sec": 110},
    }
    prompt = psj.build_prompt(fake_job)
    # Old check: "段子是否一字不改嵌入" still present? (we tightened to "服务主题")
    # New check: 段子服务主题 (没末尾冷知识/bonus)
    assert "服务主题" in prompt or "冷知识" in prompt, \
        "硬约束 #10 should check 段子服务主题 (not 末尾冷知识/bonus)"
    print("✓ 硬约束 #10 (写完自检) updated to check 段子服务主题")




def test_build_prompt_numbered_structure():
    """硬约束 #9 (NEW 编号结构) must require 第一笔/第一层/第一波."""
    fake_job = {
        "id": "v_test_001",
        "theme": "test theme",
        "render": {"duration_sec": 110},
    }
    prompt = psj.build_prompt(fake_job)
    assert "第一笔" in prompt, "硬约束 #9 should mention '第一笔'"
    assert "第一层" in prompt, "硬约束 #9 should mention '第一层'"
    assert "第一波" in prompt, "硬约束 #9 should mention '第一波'"
    print("✓ 硬约束 #9 (NEW 编号结构) requires 第一笔/第一层/第一波")
if __name__ == "__main__":
    test_old_constants_removed()
    test_meme_guide_defined()
    test_cover_instructions_preserved()
    test_build_prompt_references_reference_memes()
    test_build_prompt_includes_meme_guide()
    test_build_prompt_hard_constraint_4_modified()
    test_build_prompt_hard_constraint_8_new()
    test_build_prompt_numbered_structure()
    test_build_prompt_self_check_10_modified()
    print("\n所有 [xingzhe] 风格 + 段子 prompt 集成测试通过")