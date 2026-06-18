#!/usr/bin/env python3
"""Smoke test: _spec_skip_pexels correctly detects Pexels-blind avoid keywords.

Note: skip_pexels 用来挡 Pexels 图片(避免 hands-on-phone 等带人图);
Pexels 视频路径仍然保留(stopwatch/抽象动效不像 Pexels 图片那样被
"人手"主导,值得保留)。这条测试只覆盖 helper 函数本身。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_render_jobs as rv  # noqa: E402


def test_skip_when_avoid_has_hands():
    spec = {"avoid": "people, faces, human hands, skin, text, brand logos, watermarks"}
    assert rv._spec_skip_pexels(spec) is True, "v_5dec0abc case must skip Pexels"
    print("✓ hands/face/text/watermark → skip Pexels")


def test_no_skip_when_avoid_is_empty():
    spec = {"avoid": ""}
    assert rv._spec_skip_pexels(spec) is False
    print("✓ empty avoid → use Pexels")


def test_no_skip_when_avoid_is_composition_only():
    spec = {"avoid": "busy background, warm tones, low contrast"}
    assert rv._spec_skip_pexels(spec) is False
    print("✓ composition-only avoid → use Pexels (can be expressed in query)")


def test_no_skip_when_spec_is_empty():
    assert rv._spec_skip_pexels({}) is False
    assert rv._spec_skip_pexels(None) is False
    print("✓ empty/None spec → use Pexels")


def test_skip_on_individual_pexels_blind_keyword():
    # Each of these alone is enough to skip — Pexels is "blind" to all of them
    for kw in ["people", "face", "hand", "hands", "text", "watermark", "logo", "human", "skin"]:
        spec = {"avoid": kw}
        assert rv._spec_skip_pexels(spec) is True, f"keyword {kw!r} should skip Pexels"
    print("✓ 9 individual Pexels-blind keywords each skip Pexels")


def test_no_false_positive_on_substrings():
    # "handy" → no match (no "hand" word). "logos-free" → match ("logos" is
    # a real Pexels-blind keyword + "-" is a word boundary). "non-human" →
    # match (intentional, means avoid humans).
    assert rv._spec_skip_pexels({"avoid": "handy tools, no handy items"}) is False
    assert rv._spec_skip_pexels({"avoid": "logos-free design"}) is True
    assert rv._spec_skip_pexels({"avoid": "non-human scene"}) is True
    print("✓ word-boundary: handy → no match, logos-free → match, non-human → match")


def test_case_insensitive():
    spec = {"avoid": "PEOPLE, Faces, HAND"}
    assert rv._spec_skip_pexels(spec) is True
    print("✓ case-insensitive matching")


if __name__ == "__main__":
    test_skip_when_avoid_has_hands()
    test_no_skip_when_avoid_is_empty()
    test_no_skip_when_avoid_is_composition_only()
    test_no_skip_when_spec_is_empty()
    test_skip_on_individual_pexels_blind_keyword()
    test_no_false_positive_on_substrings()
    test_case_insensitive()
    print("\n✅ all 7 skip_pexels tests passed")