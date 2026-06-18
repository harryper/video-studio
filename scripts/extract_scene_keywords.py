#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Extract per-scene visual specs for image/video matching.

The script daemon writes a Chinese narrative (e.g. "如果全世界人类都不吃脂肪
会怎样？"); the render daemon needs a *semantic* description of what each
scene should look like, so that Pexels image search and MiniMax image
generation both end up with visuals that actually match the script.

Schema (one dict per chunk):
    {
      "subject":       str — concrete, photogenic subject ("ticking clock
                       hand second-precision" — NOT "hook theory")
      "shot":          str — camera angle / framing ("extreme close-up",
                       "wide establishing shot")
      "mood":          str — 1-3 adjectives ("urgent, focused")
      "color_palette": str — primary + accent ("dark teal + neon red")
      "avoid":         str — things to keep OUT of the visual ("people,
                       faces, text, brand logos")
    }

The LLM is asked for ALL chunks in ONE call (batched), with 2-3 worked
examples that anchor it to "concrete, photogenic, English" output. Falls
back to empty list (caller regex-heuristic) on any error.

Caches results in runs/<job_id>/keywords.json (per-script-hash). Cache
file is tagged with `schema_version: 2` so future format changes can
gracefully invalidate old caches.
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

SCHEMA_VERSION = 2

# Field names we ask the LLM for. Kept in one place so the parser and
# prompt can't drift apart.
SPEC_FIELDS = ("subject", "shot", "mood", "color_palette", "avoid")


SYSTEM_PROMPT = (
    "You extract structured visual specs for a short-form video.\n"
    "Given a theme and N numbered Chinese script chunks (empty/blank chunks "
    "have already been filtered out), return ONLY a JSON array of length N. "
    "Each element is an object with exactly these fields:\n"
    f"  - subject:       concrete, photogenic English subject (1-5 words).\n"
    f"                    e.g. 'ticking clock hand' NOT 'time' or 'hook theory'.\n"
    f"                    If the chunk is abstract, name the visual metaphor:\n"
    f"                    'rhythm' → 'metronome ticking'.\n"
    f"  - shot:          camera framing in English (one of: 'extreme close-up',\n"
    f"                    'close-up', 'medium shot', 'wide shot', 'overhead',\n"
    f"                    'low angle', 'high angle', 'tracking shot', etc.).\n"
    f"  - mood:          1-3 comma-separated English adjectives\n"
    f"                    ('urgent, focused', 'calm, contemplative').\n"
    f"  - color_palette: 1-2 English color names ('dark teal + neon red',\n"
    f"                    'warm gold + black').\n"
    f"  - avoid:         what should NOT appear. Pick the visual noise most likely\n"
    f"                    to distract from `subject`. Always consider:\n"
    f"                      - human parts: hands, fingers, limbs, skin (unless the\n"
    f"                        subject explicitly IS a hand, e.g. 'thumb swipe')\n"
    f"                      - text/labels: text, captions, brand logos, watermarks\n"
    f"                      - generic crowd: people, faces, bodies (when subject\n"
    f"                        is an object)\n"
    f"                    Then add chunk-specific noise ('crowd, daylight' for\n"
    f"                    an indoor concept; 'water, blur' for a dry subject).\n"
    f"Output: JSON only, no prose, no markdown fences. Keys must be exactly\n"
    f"{', '.join(SPEC_FIELDS)} (snake_case).\n"
)


def build_spec_prompt(theme: str, chunks: list[str]) -> str:
    # Pre-strip empty chunks: we only ask the LLM for specs of NON-empty
    # chunks (we'll realign to the original positions downstream). Empty
    # chunks (pad scenes) don't need a spec — the renderer uses a gradient
    # for those. Asking for N_chunks specs wastes tokens and frequently
    # pushes the model past its generation limit, producing truncated
    # JSON that's hard to parse reliably.
    non_empty_chunks = [c for c in chunks if c.strip()]
    parts = [
        f"主题: {theme}",
        "",
        f"为下列每条非空脚本片段输出 1 个 JSON 对象（{len(non_empty_chunks)} 个对象）"
        "，整体形成一个 JSON 数组。空/纯空白片段无需处理，已自动剔除。",
        "JSON 对象字段: subject / shot / mood / color_palette / avoid（英文）。",
        "",
        "示例:",
        "  '你体内的脂肪是这样堆积的' → "
        '{"subject": "yellow adipose tissue cluster", "shot": "close-up", '
        '"mood": "clinical, focused", "color_palette": "warm yellow + soft white", '
        '"avoid": "people, faces, hands, skin, text, brand logos, watermarks"}',
        "  '三十岁的人骨头会像六十岁' → "
        '{"subject": "osteoporosis bone x-ray", "shot": "close-up", '
        '"mood": "stark, concerning", "color_palette": "muted gray + dark blue", '
        '"avoid": "people, faces, hands, skin, text, brand logos"}',
        "  '前 0.5 秒钩住你' → "
        '{"subject": "stopwatch second hand ticking", "shot": "extreme close-up", '
        '"mood": "urgent, suspenseful", "color_palette": "black + neon red", '
        '"avoid": "people, faces, human hands, skin, text, brand logos, watermarks"}',
        "",
        "脚本片段（按出现顺序）:",
    ]
    for i, c in enumerate(non_empty_chunks, 1):
        c_one_line = re.sub(r"\s+", " ", c).strip()[:80]
        parts.append(f"  [{i}] {c_one_line}")
    parts.append("")
    parts.append(
        "只输出 JSON 数组，例: [{\"subject\": \"...\", ...}, {\"subject\": \"...\", ...}]"
    )
    return "\n".join(parts)


# ── Parsing ───────────────────────────────────────────────────────────

_VALID_SHOTS = {
    "extreme close-up", "close-up", "medium shot", "wide shot",
    "overhead", "low angle", "high angle", "tracking shot",
    "over-the-shoulder", "portrait framing", "object still life",
    "wide establishing shot", "cinematic vista",
}


def _coerce_spec(item: object) -> dict:
    """Coerce a parsed JSON object into a valid spec dict. Missing fields
    become empty strings (callers handle missing data gracefully)."""
    if not isinstance(item, dict):
        return {f: "" for f in SPEC_FIELDS}
    out = {}
    for f in SPEC_FIELDS:
        v = item.get(f, "")
        if not isinstance(v, str):
            v = str(v) if v is not None else ""
        out[f] = v.strip()
    return out


def _parse_spec_array(text: str) -> list[dict] | None:
    """Extract a JSON array of objects from LLM output, tolerating stray prose.

    Returns None if the text is empty or no array-of-objects is found.
    Handles:
      1) Plain JSON: [{"subject":...}, ...]
      2) Wrapped JSON envelope (openclaw --json): {"text": "[...]", ...}
      3) Mixed prose + JSON: "blah blah [{...}] blah"
      4) Truncated JSON array (LLM hit token limit): "[{...}, {..." — we
         close the open brackets and parse.
    """
    if not text:
        return None

    def _try_parse(s: str) -> list[dict] | None:
        try:
            v = json.loads(s)
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(v, list) and all(isinstance(x, dict) for x in v):
            return [_coerce_spec(x) for x in v]
        return None

    # 1) direct parse
    out = _try_parse(text)
    if out is not None:
        return out

    # 2) parse whole text as JSON envelope and walk it
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        d = None
    if d is not None:
        def _walk(obj, depth=0):
            if depth > 8:
                return None
            if isinstance(obj, list) and obj and isinstance(obj[0], dict):
                # Only treat as a spec array if the dicts actually LOOK
                # like specs (have at least one spec field). Otherwise
                # this is a structural envelope (e.g. openclaw result
                # payloads) and we should keep walking.
                if any(isinstance(x, dict) and x.get("subject") for x in obj):
                    return [_coerce_spec(x) for x in obj]
                for x in obj:
                    r = _walk(x, depth + 1)
                    if r is not None:
                        return r
                return None
            if isinstance(obj, list):
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
                # Try normal parse first; if that fails, try recovery
                # (LLM truncated the JSON array inside this string).
                out = _try_parse(obj)
                if out is not None:
                    return out
                recovered = _recover_truncated_array(obj)
                if recovered is not None:
                    out = _try_parse(recovered)
                    if out is not None:
                        return out
            return None
        out = _walk(d)
        if out is not None:
            return out

    # 3) find outermost [...] block via bracket-matching (regex with .*?
    # stops at the first `}` which can be wrong when the array itself is
    # nested inside an envelope like [{"text": "[{...}]"}]).
    # Only accept it if the captured block actually LOOKS like a spec
    # array (each dict has at least one spec field). Otherwise we caught
    # an outer envelope array (e.g. openclaw `payloads`) — fall through
    # to recovery below.
    block = _find_outermost_array(text)
    if block is not None:
        out = _try_parse(block)
        if out is not None and any(
            isinstance(x, dict) and any(k in SPEC_FIELDS for k in x)
            for x in out
        ):
            return out

    # 4) Recover from a truncated JSON array (LLM hit output token limit
    # and dropped the closing `]`). The text typically ends mid-spec like
    # `[{"subject": "x", ...}, {"subject": "y", "sh...`. We find the
    # open `[`, walk forward, and close any open braces/brackets we find.
    recovered = _recover_truncated_array(text)
    if recovered is not None:
        out = _try_parse(recovered)
        if out is not None and any(
            isinstance(x, dict) and any(k in SPEC_FIELDS for k in x)
            for x in out
        ):
            return out
    return None


def _recover_truncated_array(text: str) -> str | None:
    """If `text` starts a JSON array but never closes it (LLM truncation),
    return the text with the necessary `}` and `]` appended to make it
    valid JSON. Returns None if no `[` is found.

    We only recover if at least one complete dict element was parsed
    (otherwise there's nothing to recover). Strategy:
      1. Find the first `[` (skipping prose).
      2. Walk character-by-character tracking open braces / strings.
      3. At end, append any missing `}` then `]` to close.
      4. If we're mid-spec (depth_obj > 0 and we're inside an object
         that's not yet closed), truncate back to the last `,` so we
         don't emit a half-built field. Also handle mid-string
         truncation by trimming back to the previous key-value boundary.
    """
    start = text.find("[")
    if start == -1:
        return None
    depth_obj = 0   # { ... }
    depth_arr = 0   # [ ... ]
    in_str = False
    escape = False
    last_complete_spec_end = -1   # index right after a closing `}`
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_str:
            escape = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth_obj += 1
        elif c == "}":
            depth_obj -= 1
            if depth_obj == 0:
                last_complete_spec_end = i + 1
        elif c == "[":
            depth_arr += 1
        elif c == "]":
            depth_arr -= 1

    if depth_arr <= 0 and depth_obj <= 0:
        return None  # already balanced; shouldn't reach here

    if last_complete_spec_end == -1:
        return None  # nothing complete to recover

    # Truncate text to the last complete spec, then append `]` to close array.
    return text[:last_complete_spec_end] + "]"


def _find_outermost_array(text: str) -> str | None:
    """Return the substring of the first balanced [...] array in text, or None.

    Bracket-counting avoids the regex .*? problem of stopping at the first
    inner `}`. Returns the substring (including outer brackets) so the
    caller can json.loads it.
    """
    start = text.find("[")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            c = text[i]
            if escape:
                escape = False
                continue
            if c == "\\" and in_str:
                escape = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("[", start + 1)
    return None


def _call_llm(theme: str, chunks: list[str], session_key: str) -> list[dict] | None:
    prompt = build_spec_prompt(theme, chunks)
    # openclaw agent has no --system flag — prepend system prompt to user message
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

    out = (result.stdout or "") + "\n" + (result.stderr or "")
    # DEBUG: trace what the LLM actually returned
    print(
        f"[extract_scene_keywords] LLM raw stdout ({len(result.stdout or '')} chars): "
        f"{(result.stdout or '')[:200]!r}",
        file=sys.stderr,
    )
    parsed = _parse_spec_array(out)
    if parsed is None:
        print(
            f"[extract_scene_keywords] parser returned None. Combined tail (300 chars): "
            f"{out[-300:]!r}",
            file=sys.stderr,
        )
    return parsed


# ── Cache ─────────────────────────────────────────────────────────────

def _script_hash(chunks: list[str], theme: str) -> str:
    h = hashlib.sha256()
    h.update(theme.encode("utf-8"))
    for c in chunks:
        h.update(b"\0")
        h.update(c.encode("utf-8"))
    return h.hexdigest()[:16]


def _read_cache(cache_path: Path, script_h: str) -> list[dict] | None:
    """Read cache, return list[dict] or None. Old schema (v1, list[list[str]])
    is treated as a cache miss so the new code regenerates it."""
    if not cache_path.exists():
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if cached.get("schema_version") != SCHEMA_VERSION:
        return None
    if cached.get("script_hash") != script_h:
        return None
    visual_specs = cached.get("visual_specs")
    if not isinstance(visual_specs, list):
        return None
    out = []
    for item in visual_specs:
        if isinstance(item, dict):
            out.append(_coerce_spec(item))
        else:
            out.append({f: "" for f in SPEC_FIELDS})
    return out


def extract_visual_specs(
    job_id: str, theme: str, chunks: list[str], *, force_refresh: bool = False
) -> list[dict]:
    """Return a list of length len(chunks); each element is a spec dict.

    Returns a list of empty-field dicts on failure (caller should fall back
    to the regex heuristic). Caches result in
    runs/<job_id>/keywords.json (same path as v1; v2 adds schema_version).
    """
    if not chunks:
        return []
    run_dir = RUNS_DIR / job_id
    cache_path = run_dir / "keywords.json"
    script_h = _script_hash(chunks, theme)

    if not force_refresh:
        cached = _read_cache(cache_path, script_h)
        if cached is not None and len(cached) == len(chunks):
            return cached

    # New session-key namespace so we don't pollute the v1 kw session and
    # so a v2 answer can't be confused with a stale v1 one if the cache
    # file gets corrupted.
    session_key = f"agent:main:video-studio-vspec-{job_id}"
    result = _call_llm(theme, chunks, session_key)

    # Re-align LLM output with the original chunks list. The LLM's
    # behavior is inconsistent: it sometimes drops trailing empty (pad)
    # chunks and returns N_real specs, sometimes pads its own entries
    # for them and returns N_chunks specs. We accept either length and
    # map positionally:
    #   - len(result) == n_real  → zip with non_empty_indices
    #   - len(result) == len(chunks) → 1:1
    # Anything else is treated as a parse failure.
    non_empty_indices = [i for i, c in enumerate(chunks) if c.strip()]
    n_real = len(non_empty_indices)

    def _fits(r):
        return r is not None and len(r) in (n_real, len(chunks))

    # The LLM intermittently returns a wrong-length list (we've seen 1-spec
    # responses when the right answer is 4). Manual _call_llm tests show
    # the model itself works fine — it's a transient alignment issue
    # (probably partial completion / mid-stream truncation). One retry
    # almost always clears it; further retries are wasteful.
    if not _fits(result):
        print(
            f"[extract_scene_keywords] LLM returned {len(result) if result else 0} specs "
            f"(expected {n_real} non-empty or {len(chunks)} total); retrying once",
            file=sys.stderr,
        )
        result_retry = _call_llm(theme, chunks, session_key + "-retry")
        if _fits(result_retry):
            print(
                f"[extract_scene_keywords] retry OK: got {len(result_retry)} specs",
                file=sys.stderr,
            )
            result = result_retry
        else:
            print(
                f"[extract_scene_keywords] retry also failed "
                f"(got {len(result_retry) if result_retry else 0}); using empty specs",
                file=sys.stderr,
            )

    if result is None:
        normalized = [{f: "" for f in SPEC_FIELDS} for _ in chunks]
    elif len(result) == n_real:
        normalized = [{f: "" for f in SPEC_FIELDS} for _ in chunks]
        for idx, spec in zip(non_empty_indices, result):
            normalized[idx] = _coerce_spec(spec)
    elif len(result) == len(chunks):
        normalized = [_coerce_spec(item) for item in result]
    else:
        print(
            f"[extract_scene_keywords] LLM returned {len(result)} specs "
            f"(expected {n_real} non-empty or {len(chunks)} total); using empty specs",
            file=sys.stderr,
        )
        normalized = [{f: "" for f in SPEC_FIELDS} for _ in chunks]

    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "script_hash": script_h,
                    "theme": theme,
                    "visual_specs": normalized,
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"[extract_scene_keywords] cache write failed: {e}", file=sys.stderr)
    return normalized


# Backwards-compatible alias. The render daemon imports `extract_keywords`;
# the function returns list[dict] now (was list[list[str]]). All v2 callers
# have been updated to use the new dict shape.
def extract_keywords(*args, **kwargs):
    return extract_visual_specs(*args, **kwargs)


# ── CLI for debugging ─────────────────────────────────────────────────
def _main() -> int:
    if len(sys.argv) < 3:
        print(
            "usage: extract_scene_keywords.py <job_id> <script_file> [--theme <t>]",
            file=sys.stderr,
        )
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
    result = extract_visual_specs(job_id, theme, chunks)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
