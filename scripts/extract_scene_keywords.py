#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Extract visual search keywords from script chunks for Pexels image matching.

The script daemon writes a Chinese narrative (e.g. "如果全世界人类都不吃脂肪
会怎样？"); the render daemon needs short English Pexels search terms per
scene. The naive heuristic of "first 4 CJK chars + theme" often matches badly
("脂肪敌人" → drink photos because Pexels' Chinese index is sparse).

This module batches ALL chunks into ONE LLM call to extract 1-3 focused
English visual keywords per chunk. Falls back to an empty list on any error;
the caller should then fall back to the regex heuristic.

Caches results in runs/<job_id>/keywords.json (per-script-hash) so reruns of
the same script don't re-call the LLM.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_DIR = SKILL_DIR.parent
NODE = Path("/usr/bin/node")
OPENCLAW = Path("/usr/lib/node_modules/openclaw/openclaw.mjs")
RUNS_DIR = SKILL_DIR / "runs"

SYSTEM_PROMPT = (
    "You extract visual search keywords for a short-form video.\n"
    "Given a theme and N numbered Chinese script chunks, return ONLY a JSON "
    "array of length N, where each element is an array of 1-3 lowercase "
    "English Pexels search terms (specific, visual, no abstract concepts).\n"
    "Output: JSON only, no prose, no markdown fences.\n"
)


def build_keyword_prompt(theme: str, chunks: list[str]) -> str:
    parts = [
        f"主题: {theme}",
        "",
        "为下列每条脚本片段输出 1-3 个英文 Pexels 搜索关键词（具体、可视、避免抽象词）。",
        "每条输出一个 JSON 子数组，整体形成一个 JSON 数组。",
        "",
        "示例:",
        "  '大脑六成是脂肪' → ['human brain anatomy', 'DHA omega-3 molecule']",
        "  '脂肪是荷尔蒙原料' → ['cholesterol molecule', 'endocrine glands']",
        "  '三十岁的人骨头会像六十岁' → ['osteoporosis x-ray', 'elderly bone density']",
        "",
        "脚本片段:",
    ]
    for i, c in enumerate(chunks, 1):
        c_one_line = re.sub(r"\s+", " ", c).strip()[:80]
        parts.append(f"  [{i}] {c_one_line}")
    parts.append("")
    parts.append("只输出 JSON 数组，例: [[\"k1\", \"k2\"], [\"k3\"], ...]")
    return "\n".join(parts)


def _parse_json_array(text: str) -> list[list[str]] | None:
    """Extract a JSON array of arrays from LLM output, tolerating stray prose.

    Handles:
    1) Plain JSON: [["k1"], ["k2"]]
    2) Wrapped JSON envelope (openclaw --json): {"text": "[[...]]", ...}
    3) Mixed prose + JSON: "blah blah [["k1"]] blah"
    """
    if not text:
        return None

    def _try_parse(s):
        try:
            v = json.loads(s)
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(v, list) and all(isinstance(x, list) for x in v):
            return [[str(item) for item in x] for x in v]
        return None

    # 1) direct parse
    out = _try_parse(text)
    if out is not None:
        return out

    # 2) try parsing whole text as JSON envelope (openclaw --json 输出)
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        d = None
    if d is not None:
        # 递归找第一个形如 [[...]] 的字段
        def _walk(obj, depth=0):
            if depth > 8:
                return None
            if isinstance(obj, list):
                if obj and isinstance(obj[0], list):
                    return [[str(s) for s in x] for x in obj]
                for x in obj:
                    r = _walk(x, depth + 1)
                    if r is not None:
                        return r
            elif isinstance(obj, dict):
                for v in obj.values():
                    r = _walk(v, depth + 1)
                    if r is not None:
                        return r
            elif isinstance(obj, str):
                return _try_parse(obj)
            return None
        out = _walk(d)
        if out is not None:
            return out

    # 3) 抓第一段 [[...]]（re.DOTALL 跨行）
    m = re.search(r"\[\s*\[.*?\]\s*\]", text, re.DOTALL)
    if m:
        out = _try_parse(m.group(0))
        if out is not None:
            return out
    return None


def _call_llm(theme: str, chunks: list[str], session_key: str) -> list[list[str]] | None:
    prompt = build_keyword_prompt(theme, chunks)
    # openclaw agent 没有 --system 选项 — 把系统提示拼到 message 开头
    full_message = f"[系统指令]\n{SYSTEM_PROMPT}\n\n[用户]\n{prompt}"
    cmd = [
        str(NODE), str(OPENCLAW), "agent",
        "--agent", "main",
        "--session-key", session_key,
        "--message", full_message,
        "--thinking", "off",
        "--json",
        "--timeout", "120",
    ]
    try:
        result = subprocess.run(
            cmd, cwd=str(WORKSPACE_DIR), text=True,
            capture_output=True, timeout=150,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"[extract_scene_keywords] LLM call failed: {e}", file=sys.stderr)
        return None

    # openclaw --json may emit a JSON envelope; the model text is usually in
    # stdout. Try to find the assistant text.
    out = (result.stdout or "") + "\n" + (result.stderr or "")
    parsed = _parse_json_array(out)
    if parsed is None:
        # Last resort: grep for any nested array in the output
        m = re.search(r"(\[\s*\"[^\"]+\"[^]]*\])", out)
        if m:
            try:
                v = json.loads(m.group(0))
                if isinstance(v, list):
                    return [v]
            except json.JSONDecodeError:
                pass
    return parsed


def _script_hash(chunks: list[str], theme: str) -> str:
    h = hashlib.sha256()
    h.update(theme.encode("utf-8"))
    for c in chunks:
        h.update(b"\0")
        h.update(c.encode("utf-8"))
    return h.hexdigest()[:16]


def extract_keywords(
    job_id: str, theme: str, chunks: list[str], *, force_refresh: bool = False
) -> list[list[str]]:
    """Return a list of length len(chunks); each element is 1-3 English keywords.

    Returns empty inner list `[]` on failure (caller should fall back to
    regex heuristic). Caches result in runs/<job_id>/keywords.json.
    """
    if not chunks:
        return []
    run_dir = RUNS_DIR / job_id
    cache_path = run_dir / "keywords.json"
    script_h = _script_hash(chunks, theme)
    if not force_refresh and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("script_hash") == script_h and cached.get("keywords"):
                return cached["keywords"]
        except (OSError, json.JSONDecodeError):
            pass

    # Try batched LLM call
    session_key = f"agent:main:video-studio-kw-{job_id}"
    result = _call_llm(theme, chunks, session_key)
    if result is None or len(result) != len(chunks):
        print(f"[extract_scene_keywords] LLM returned no result, using empty", file=sys.stderr)
        result = [[] for _ in chunks]

    # Pad to 3 elements each (caller uses [0] mainly)
    normalized: list[list[str]] = []
    for kws in result:
        clean = [str(k).strip().lower() for k in (kws or []) if str(k).strip()]
        normalized.append(clean[:3] if clean else [])

    # Persist cache
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {"script_hash": script_h, "theme": theme, "keywords": normalized},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"[extract_scene_keywords] cache write failed: {e}", file=sys.stderr)
    return normalized


# ── CLI for debugging ─────────────────────────────────────────────────
def _main() -> int:
    if len(sys.argv) < 3:
        print("usage: extract_scene_keywords.py <job_id> <script_file> [--theme <t>]", file=sys.stderr)
        return 2
    job_id = sys.argv[1]
    script_path = Path(sys.argv[2])
    theme = "未指定"
    if "--theme" in sys.argv:
        i = sys.argv.index("--theme")
        if i + 1 < len(sys.argv):
            theme = sys.argv[i + 1]
    script = script_path.read_text(encoding="utf-8")
    # Chunk by sentence ending
    chunks = re.split(r"(?<=[。！？!?\.])\s+", script)
    chunks = [c.strip() for c in chunks if c.strip()]
    print(f"=== {len(chunks)} chunks for job {job_id} (theme={theme}) ===", file=sys.stderr)
    result = extract_keywords(job_id, theme, chunks)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
