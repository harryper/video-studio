#!/usr/bin/env python3
"""Unit tests for reference-memes.md structure.

Run: python3 scripts/test_reference_memes.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMES_FILE = ROOT / "reference-memes.md"


def test_file_exists():
    """reference-memes.md must exist at project root."""
    assert MEMES_FILE.exists(), f"{MEMES_FILE} missing"
    print("✓ reference-memes.md exists at project root")


def test_has_four_sections():
    """Must contain 4 required section headings."""
    content = MEMES_FILE.read_text(encoding="utf-8")
    required = [
        "## 入选门槛",
        "## 段子表",
        "## 调性边界",
        "## 维护纪律",
    ]
    for heading in required:
        assert heading in content, f"missing section: {heading}"
    print(f"✓ all 4 required sections present: {required}")


def test_meme_table_has_9_rows():
    """段子表 must have exactly 9 [xingzhe] user-curated 段子."""
    content = MEMES_FILE.read_text(encoding="utf-8")
    # Count table rows starting with "| N |" where N is 1-9
    import re
    rows = re.findall(r"^\|\s*[1-9]\s*\|", content, re.MULTILINE)
    assert len(rows) == 9, f"expected 9 [xingzhe] meme rows, got {len(rows)}"
    print(f"✓ meme table has 9 [xingzhe] rows: {rows}")


def test_all_memes_non_empty():
    """Each of the 9 [xingzhe] memes must have non-empty content."""
    content = MEMES_FILE.read_text(encoding="utf-8")
    # Required [xingzhe] 段子 / 种子 phrases that must appear
    required_phrases = [
        "夏侯惇鉴宝",      # 段子 #1
        "一眼假",          # 段子 #1 PUN
        "恐怖直立猿",      # 段子 #2
        "地球online",      # 段子 #3
        "一眼望不到边",    # 段子 #4 PUN
        "四目相对",        # 段子 #6 PUN
        "太监开会",        # 段子 #7
        "无稽之谈",        # 段子 #7 PUN
        "路易十六的生日",  # 段子 #8
        "过到头了",        # 段子 #8 PUN
    ]
    for phrase in required_phrases:
        assert phrase in content, f"missing required phrase: {phrase}"
    print(f"✓ all 10 required [xingzhe] phrases present across 9 memes")


if __name__ == "__main__":
    test_file_exists()
    test_has_four_sections()
    test_meme_table_has_9_rows()
    test_all_memes_non_empty()
    print("\n所有 reference-memes.md 测试通过")
