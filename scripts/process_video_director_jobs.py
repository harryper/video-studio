#!/usr/bin/env python3
"""Host-side director for video-studio (mode='video').

Stage 2 of the 4-stage pipeline (script → director → render → narrate).
Reads jobs in status='ready_script', translates each script chunk into a
cat-doctor shot (composition / action / annotations / full prompt), writes
runs/{job_id}/shotlist.json, sets status='ready_shotlist', touches the
render trigger.

Created in Task 4 (prompt_assemble only). The full daemon loop, LLM
call, cache, and cascade are added in Tasks 5-6.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
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


def prompt_assemble(shot: dict, style_prefix: str = STYLE_PREFIX,
                    character_block: str = CHARACTER_BLOCK) -> tuple[str, str]:
    """Assemble the final MiniMax prompt + negative_prompt for one shot.

    Implements the cat-doctor 5-段 prompt structure (reference/cat-doctor/prompt-template.md):
      [1 风格前缀] + [2 主体描述] + [3 角色描述] + [4 动作与场景] + [5 批注与结尾]

    Returns (prompt, negative_prompt). negative_prompt defaults to
    ek.SHOTLIST_DEFAULT_NEGATIVE_PROMPT unless shot provides its own.
    """
    annotations = shot.get("annotations") or []
    ann_lines = []
    for ann in annotations:
        color = ann.get("color", "blue")
        text = ann.get("text", "")
        ann_lines.append(f"{color} text {text}")
    if ann_lines:
        annotations_block = (
            "A few small handwritten Chinese annotations in red, orange and blue "
            "floating around: " + ", ".join(ann_lines) + "."
        )
    else:
        annotations_block = (
            "A few small handwritten Chinese annotations may float around in red, "
            "orange or blue. 16:9 horizontal, pure white background."
        )

    action = (shot.get("action") or "").strip()
    if action:
        scene_block = f"The cat is {action}."
    else:
        scene_block = "The cat is doing the core action of this scene."

    composition = shot.get("composition", "")
    subject_block = _subject_block(composition, shot)

    prompt = (
        f"{style_prefix}\n\n"
        f"{subject_block}\n\n"
        f"{character_block} {scene_block}\n\n"
        f"{annotations_block}\n\n"
        "Clean, witty, slightly absurd but not cute, not childish, not adorable. "
        "16:9 horizontal, pure white background."
    )

    negative_prompt = (
        shot.get("negative_prompt")
        or ek.SHOTLIST_DEFAULT_NEGATIVE_PROMPT
    )
    return prompt, negative_prompt


def _subject_block(composition: str, shot: dict) -> str:
    """Pick the 主体描述 skeleton by composition (cat-doctor §2)."""
    action = (shot.get("action") or "").strip()
    if composition == "hook":
        return "A surprising or counter-intuitive object next to the cat, looking puzzled."
    if composition == "data-compare":
        return "Three or more simple outlined bar shapes of different heights."
    if composition == "process":
        return f"A simple scene where {action or 'something is happening step by step'}."
    if composition == "rank":
        return "A simple hand-drawn ranking list with bars of decreasing height."
    if composition == "twist":
        return "The frame split into two halves: left side vs right side."
    if composition == "metaphor":
        return f"A physical metaphor scene: {action or 'an abstract idea rendered as a concrete object'}."
    return "A simple hand-drawn scene."