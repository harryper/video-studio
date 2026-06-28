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


def test_meme_table_has_5_rows():
    """段子表 must have exactly 5 user-approved 段子."""
    content = MEMES_FILE.read_text(encoding="utf-8")
    # Count table rows starting with "| N |" where N is 1-5
    import re
    rows = re.findall(r"^\|\s*[1-5]\s*\|", content, re.MULTILINE)
    assert len(rows) == 5, f"expected 5 meme rows, got {len(rows)}"
    print(f"✓ meme table has 5 rows: {rows}")


def test_all_memes_non_empty():
    """Each of the 5 memes must have non-empty content."""
    content = MEMES_FILE.read_text(encoding="utf-8")
    # Required seed phrases that must appear
    required_phrases = [
        "魏忠贤",          # 段子 #1
        "无后为大",        # 段子 #1 PUN
        "夏侯惇",          # 段子 #2
        "一眼就相中",      # 段子 #2 PUN
        "路易十六",        # 段子 #3/#4/#5
        "替我出头",        # 段子 #3 PUN
        "摸不着头脑",      # 段子 #4 PUN
        "禁止调头",        # 段子 #5 PUN
    ]
    for phrase in required_phrases:
        assert phrase in content, f"missing required phrase: {phrase}"
    print(f"✓ all 8 required phrases present across 5 memes")


if __name__ == "__main__":
    test_file_exists()
    test_has_four_sections()
    test_meme_table_has_5_rows()
    test_all_memes_non_empty()
    print("\n所有 reference-memes.md 测试通过")
