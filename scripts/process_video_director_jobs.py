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

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_scene_keywords as ek  # noqa: E402
from _paths import SKILL_DIR, JOBS_DIR, RUNS_DIR  # noqa: E402
import llm_client  # noqa: E402


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


# ── LLM call + shotlist build ────────────────────────────────────────

CAT_DOCTOR_SYSTEM = (
    "你是 video-studio 的科普画面导演。"
    "风格: 纯白底 #FFFFFF + 极简黑墨线 1-2px (轻微手绘抖动) + 留白 50-70% + "
    "红/橙/蓝三色手写中文批注 (≤5 条)。"
    "IP: 喵博士 = 单片眼镜 (金链) + 蝴蝶领结 + 简笔小猫, 头身比 1:1, "
    "怪诞工人感, 不卖萌。"
    "构图: 一图一动作, 6 类 (hook/data-compare/process/rank/twist/metaphor)。"
    "金句 quote 不进配图。"
    "反例: 精致扁平插画, 商业插画, PPT 流程图, 萌系 Q 版, 拟真照片, 3D 渲染, "
    "kawaii, chibi, cute, adorable, blush。"
)


def build_shot_prompt(theme: str, chunks: list[str]) -> str:
    """Batch prompt: ask LLM to translate each non-empty chunk into one shot."""
    non_empty = [c for c in chunks if c and c.strip()]
    bullets = "\n".join(f"{i}. {c}" for i, c in enumerate(non_empty))
    return (
        f"主题：{theme}\n\n"
        f"共有 {len(non_empty)} 个非空场景（脚本已按句子切分, 按顺序编号）：\n"
        f"{bullets}\n\n"
        f"为每个场景输出一个 JSON object, 所有 object 包在一个 JSON array 里输出。"
        f"每个 object 字段：\n"
        f"  scene_index: int (对应上面编号, 从 0 开始)\n"
        f"  composition: enum ∈ hook | data-compare | process | rank | twist | metaphor\n"
        f"  action: str (喵博士正在做的核心动作, 一图一动作, 用物理隐喻翻译抽象概念)\n"
        f"  annotations: list of {{text, color}}; color ∈ red|orange|blue; 3-5 条\n"
        f"    red=结论/警告/数字冲击, orange=疑问/引导, blue=标签/中性\n"
        f"  negative_prompt: str (可选, 留空则用默认反例词)\n\n"
        f"严格要求：\n"
        f"- composition 必须从 6 选 1, 不要 'quote' (金句不进配图)\n"
        f"- annotations 每条 text 1-6 字, 总数 3-5\n"
        f"- action 必须含一个具体的物理动作 (拿工具/指向/歪头/抱胸), 不要抽象动词\n"
        f"- 只输出 JSON array, 没有任何额外文字\n"
    )


def call_llm(theme: str, chunks: list[str], session_key: str) -> list[dict] | None:
    """Single batch LLM call via llm_client (post-decouple: no subprocess).

    Returns list[shot-dict] or None on failure. `session_key` is retained in
    the signature for test-mock compatibility (see test_shotlist_schema.py).
    """
    user_prompt = build_shot_prompt(theme, chunks)
    try:
        text = llm_client.complete(
            system=CAT_DOCTOR_SYSTEM,
            user=user_prompt,
            max_tokens=4096,
            timeout=180.0,
        )
    except Exception as e:
        print(f"[director] LLM call failed: {e}", file=sys.stderr)
        return None
    return _parse_shot_array(text)


def _parse_shot_array(text: str) -> list[dict] | None:
    """Parse LLM output as a list of shot dicts.

    Reuses ek._recover_truncated_array / _find_outermost_array for resilience.
    Falls back to None if recovery fails.
    """
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            return [_coerce_shot(x) for x in obj if isinstance(x, dict)]
    except (ValueError, TypeError):
        pass

    block = ek._find_outermost_array(text)
    if block:
        try:
            obj = json.loads(block)
            if isinstance(obj, list):
                return [_coerce_shot(x) for x in obj if isinstance(x, dict)]
        except (ValueError, TypeError):
            pass

    recovered = ek._recover_truncated_array(text)
    if recovered:
        try:
            obj = json.loads(recovered)
            if isinstance(obj, list):
                return [_coerce_shot(x) for x in obj if isinstance(x, dict)]
        except (ValueError, TypeError):
            return None
    return None


def _coerce_shot(item: dict) -> dict:
    """Validate + normalize a single shot dict from the LLM."""
    composition = item.get("composition", "")
    if composition not in ek.SHOTLIST_COMPOSITIONS:
        composition = "metaphor"
    annotations = []
    for ann in (item.get("annotations") or []):
        if not isinstance(ann, dict):
            continue
        color = ann.get("color", "blue")
        if color not in ek.SHOTLIST_ANNOTATION_COLORS:
            color = "blue"
        text = str(ann.get("text", "")).strip()[:12]
        if text:
            annotations.append({"text": text, "color": color})
    return {
        "scene_index": int(item.get("scene_index", 0)),
        "chunk": str(item.get("chunk", "")),
        "composition": composition,
        "action": str(item.get("action", "")).strip()[:120],
        "annotations": annotations,
        "negative_prompt": str(item.get("negative_prompt", "") or "").strip(),
    }


def build_shotlist(job_id: str, theme: str, chunks: list[str], *,
                   force_refresh: bool = False,
                   _llm=call_llm) -> dict:
    """Main entry: read/write cache, call LLM, assemble full shotlist."""
    script_hash = ek._script_hash(chunks, theme)
    cache_path = RUNS_DIR / job_id / "shotlist.json"
    if not force_refresh:
        cached = _read_shotlist_cache(cache_path, script_hash)
        if cached is not None:
            return cached

    session_key = f"agent:main:video-studio-director-{job_id}"
    raw_shots = _llm(theme, chunks, session_key)

    non_empty_indices = [i for i, c in enumerate(chunks) if c and c.strip()]
    if raw_shots is None:
        shots_aligned = [_minimal_shot(i, chunks[i]) for i in non_empty_indices]
    else:
        by_index = {int(s.get("scene_index", -1)): s for s in raw_shots}
        shots_aligned = []
        for i in non_empty_indices:
            shot = by_index.get(i)
            if shot is None:
                shots_aligned.append(_minimal_shot(i, chunks[i]))
            else:
                shot["chunk"] = chunks[i]
                shot["scene_index"] = i
                shots_aligned.append(_coerce_shot(shot))

    final_shots: list = [None] * len(chunks)
    for i, idx in enumerate(non_empty_indices):
        final_shots[idx] = shots_aligned[i]

    for shot in final_shots:
        if shot is None:
            continue
        prompt, neg = prompt_assemble(shot)
        shot["prompt"] = prompt
        shot["negative_prompt"] = neg

    shotlist = {
        "schema_version": ek.SHOTLIST_SCHEMA_VERSION,
        "script_hash": script_hash,
        "theme": theme,
        "shots": final_shots,
    }

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(shotlist, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return shotlist


def _minimal_shot(scene_index: int, chunk: str) -> dict:
    """Director 降级 shot (LLM 挂掉时): action=chunk 摘要, 无批注, metaphor 构图."""
    return {
        "scene_index": scene_index,
        "chunk": chunk,
        "composition": "metaphor",
        "action": f"用简笔物理隐喻表达: {chunk[:60]}",
        "annotations": [],
        "negative_prompt": "",
    }


def _read_shotlist_cache(cache_path: Path, script_h: str) -> dict | None:
    if not cache_path.exists():
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if cached.get("schema_version") != ek.SHOTLIST_SCHEMA_VERSION:
        return None
    if cached.get("script_hash") != script_h:
        return None
    return cached