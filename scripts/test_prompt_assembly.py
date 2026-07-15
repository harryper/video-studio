#!/usr/bin/env python3
"""Unit tests for prompt_assemble (cat-doctor 5-段结构).

Run: python3 scripts/test_prompt_assembly.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_director_jobs as dj  # noqa: E402
import extract_scene_keywords as ek  # noqa: E402


STYLE_PREFIX = (
    "A minimal ink-line illustration, pure white background, no paper texture, "
    "no shadow, no gradient. Thin black hand-drawn lines, slightly wobbly, "
    "deliberately imperfect."
)
CHARACTER_BLOCK = (
    "A simple small anthropomorphic cat character (round head, two small triangle "
    "ears, two small dot eyes, single small round monocle on a thin gold chain, "
    "small bowtie collar) drawn in the style of a quirky worker. Cat is small in "
    "frame occupying about 30 to 40 percent of canvas, lots of negative white space."
)


def test_style_prefix_present_in_prompt():
    shot = {
        "scene_index": 0,
        "chunk": "西瓜比想象中甜?",
        "composition": "hook",
        "action": "歪头看着一个巨大的西瓜",
        "annotations": [{"text": "甜??", "color": "red"}, {"text": "4-7% 蔗糖", "color": "blue"}],
    }
    prompt, _neg = dj.prompt_assemble(shot, STYLE_PREFIX, CHARACTER_BLOCK)
    assert STYLE_PREFIX in prompt, "风格前缀必须原样出现在 prompt 中"


def test_character_block_present_in_prompt():
    shot = {
        "scene_index": 1,
        "chunk": "x",
        "composition": "process",
        "action": "拿扳手拧一颗螺栓",
        "annotations": [],
    }
    prompt, _neg = dj.prompt_assemble(shot, STYLE_PREFIX, CHARACTER_BLOCK)
    assert CHARACTER_BLOCK in prompt, "角色模板必须原样出现在 prompt 中"


def test_annotations_listed_with_color_in_prompt():
    shot = {
        "scene_index": 2,
        "chunk": "x",
        "composition": "data-compare",
        "action": "指着三根高低不同的柱状图",
        "annotations": [
            {"text": "6倍差距", "color": "red"},
            {"text": "一亩地", "color": "blue"},
            {"text": "为什么?", "color": "orange"},
        ],
    }
    prompt, _neg = dj.prompt_assemble(shot, STYLE_PREFIX, CHARACTER_BLOCK)
    assert "red text 「6倍差距」" in prompt
    assert "blue text 「一亩地」" in prompt
    assert "orange text 「为什么?」" in prompt


def test_action_present_in_prompt():
    shot = {
        "scene_index": 3,
        "chunk": "x",
        "composition": "process",
        "action": "在工厂里扳手拧螺栓",
        "annotations": [],
    }
    prompt, _neg = dj.prompt_assemble(shot, STYLE_PREFIX, CHARACTER_BLOCK)
    assert "在工厂里扳手拧螺栓" in prompt


def test_negative_prompt_contains_anti_keywords():
    shot = {
        "scene_index": 4,
        "chunk": "x",
        "composition": "rank",
        "action": "审视排行榜",
        "annotations": [],
    }
    _prompt, neg = dj.prompt_assemble(shot, STYLE_PREFIX, CHARACTER_BLOCK)
    for kw in ["kawaii", "chibi", "3D 渲染", "拟真照片", "萌系"]:
        assert kw in neg, f"negative_prompt 必须包含反例词 {kw!r}"


def test_shot_negative_prompt_overrides_default():
    shot = {
        "scene_index": 5,
        "chunk": "x",
        "composition": "metaphor",
        "action": "压一个想法成砖",
        "annotations": [],
        "negative_prompt": "custom-anti-keyword, 拟真照片",
    }
    _prompt, neg = dj.prompt_assemble(shot, STYLE_PREFIX, CHARACTER_BLOCK)
    assert "custom-anti-keyword" in neg
    assert "拟真照片" in neg


def test_16_9_and_pure_white_in_prompt():
    shot = {
        "scene_index": 6,
        "chunk": "x",
        "composition": "hook",
        "action": "歪头",
        "annotations": [],
    }
    prompt, _neg = dj.prompt_assemble(shot, STYLE_PREFIX, CHARACTER_BLOCK)
    assert "16:9" in prompt
    assert "pure white background" in prompt


def test_no_annotations_still_produces_valid_prompt():
    shot = {
        "scene_index": 7,
        "chunk": "x",
        "composition": "process",
        "action": "拿工具",
        "annotations": [],
    }
    prompt, neg = dj.prompt_assemble(shot, STYLE_PREFIX, CHARACTER_BLOCK)
    assert isinstance(prompt, str) and prompt
    assert isinstance(neg, str) and neg


def test_annotations_verbatim_quoted_no_translate():
    """批注必须以「」原样引用 + 明确禁止翻译成英文。

    v_1aed9b49/v_affb0166 实测: prompt 写 'red text 光撞硅', MiniMax 画成
    'Light strikes Silicon' — 英文叙述里的中文被当语义翻译了。修复要求
    每条批注用「」括起, 且 prompt 带 verbatim/do not translate 指令。"""
    shot = {
        "scene_index": 0,
        "chunk": "x",
        "composition": "process",
        "action": "看着硅片",
        "annotations": [
            {"text": "光撞硅", "color": "red"},
            {"text": "1m²≈200W", "color": "red"},
        ],
    }
    prompt, neg = dj.prompt_assemble(shot, STYLE_PREFIX, CHARACTER_BLOCK)
    assert "「光撞硅」" in prompt, f"annotation must be 「」-quoted verbatim: {prompt}"
    assert "「1m²≈200W」" in prompt
    assert "do not translate" in prompt.lower(), \
        f"prompt must forbid translating annotations: {prompt}"
    assert "verbatim" in prompt.lower() or "exactly as given" in prompt.lower()


def test_negative_prompt_blocks_english_text():
    """默认 negative_prompt 必须抑制英文文字渲染。"""
    shot = {
        "scene_index": 1,
        "chunk": "x",
        "composition": "hook",
        "action": "歪头",
        "annotations": [{"text": "真or假", "color": "blue"}],
    }
    _prompt, neg = dj.prompt_assemble(shot, STYLE_PREFIX, CHARACTER_BLOCK)
    assert "English text" in neg or "english text" in neg, \
        f"negative_prompt must suppress English text: {neg}"


if __name__ == "__main__":
    test_style_prefix_present_in_prompt()
    test_character_block_present_in_prompt()
    test_annotations_listed_with_color_in_prompt()
    test_action_present_in_prompt()
    test_negative_prompt_contains_anti_keywords()
    test_shot_negative_prompt_overrides_default()
    test_16_9_and_pure_white_in_prompt()
    test_no_annotations_still_produces_valid_prompt()
    test_annotations_verbatim_quoted_no_translate()
    test_negative_prompt_blocks_english_text()
    print("\n✅ all 10 prompt_assembly tests passed")