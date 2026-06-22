#!/usr/bin/env python3
"""Host-side render writer for video-studio jobs (mode='video').

Mirrors process_pending_voice_jobs.py structure, but:
- Listens on .video-render-trigger
- Reads jobs/video/ for ready_script jobs
- Renders hyperframes HTML composition to mp4
- Uploads to R2
- On success: status -> rendered, touches .video-narrate-trigger

v1 uses the static placeholder template (templates/video_placeholder.html).
P2 will replace the placeholder with LLM-generated HTML.
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


def escape_html(s):
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&#39;"))


SKILL_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = SKILL_DIR.parents[1]
JOBS_DIR = SKILL_DIR / "jobs" / "video"
VIDEO_RUNS_DIR = Path("/root/.openclaw/workspace/skills/video-studio/runs")
PLACEHOLDER_HTML = SKILL_DIR / "templates" / "video_placeholder.html"
VIDEO_STYLE_HELPER = Path("/root/.openclaw/workspace/skills/video-studio/reference-style-video.md")
UPLOAD_SCRIPT = SKILL_DIR / "scripts" / "upload_to_oss.py"
IMAGE_GEN_SCRIPT = SKILL_DIR / "scripts" / "minimax_image_gen.py"
PEXELS_IMAGE_SCRIPT = SKILL_DIR / "scripts" / "pexels_image.py"
PEXELS_VIDEO_SCRIPT = SKILL_DIR / "scripts" / "pexels_video.py"
PIXABAY_IMAGE_SCRIPT = SKILL_DIR / "scripts" / "pixabay_image.py"
PIXABAY_VIDEO_SCRIPT = SKILL_DIR / "scripts" / "pixabay_video.py"

DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_DURATION_SEC = 150
DEFAULT_FPS = 15

LOCK_PATH = SKILL_DIR / ".video-render-writer.lock"
RENDER_TRIGGER = SKILL_DIR / ".video-render-trigger"
NARRATE_TRIGGER = SKILL_DIR / ".video-narrate-trigger"
LAST_RUN_MARKER = SKILL_DIR / ".video-render-writer.lastrun"
LOG_FILE = Path("/var/log/video-studio/video-render-watcher.log")

# 150s at 15fps is 2250 frames and can take 10-30 minutes on this VM
# depending on host load. Bumped to 60 min after 30 min timeouts during
# re-render with alignment (machine was contended with the openclaw
# memory-core embedder).
RENDER_TIMEOUT_SEC = 3600


def log(msg):
    line = f"[video-render-writer] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def job_path(job_id):
    return JOBS_DIR / f"{job_id}.json"


def load_job(path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_job(job):
    job["updated_at"] = now_iso()
    tmp = job_path(job["id"]).with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2)
    os.replace(tmp, job_path(job["id"]))


def pending_jobs():
    jobs = []
    if not JOBS_DIR.exists():
        return jobs
    for path in JOBS_DIR.glob("v_*.json"):
        try:
            job = load_job(path)
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("mode") != "video" or job.get("status") != "ready_script":
            continue
        # Defense in depth: preview_only jobs go straight to narrate for
        # black-bg ffmpeg, skipping the full image-fetch pipeline.
        if (job.get("render") or {}).get("preview_only"):
            continue
        jobs.append(job)
    return sorted(jobs, key=lambda j: j.get("updated_at", ""))


def safe_slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower())[:30].strip("-")


def upload_mp4(local_path, slug, short_id, kind):
    """Upload to R2 and return the pre-signed URL."""
    filename = f"video-{slug}-{short_id}-{kind}.mp4"
    object_key = f"{datetime.now().strftime('%Y-%m-%d')}/video-studio/{filename}"
    cmd = [
        "python3", str(UPLOAD_SCRIPT),
        "--file", str(local_path),
        "--key", object_key,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"upload failed: {(result.stderr or result.stdout)[:500]}")
    return result.stdout.strip()


def render_placeholder(
    job_id,
    render_dir,
    script_text="",
    theme="",
    width=DEFAULT_WIDTH,
    height=DEFAULT_HEIGHT,
    total_duration=DEFAULT_DURATION_SEC,
    fps=DEFAULT_FPS,
):
    """Generate images + HTML composition for 抖音-style video (P3 v1).

    Splits the script into scenes, then for each scene:
    1. Periodically try Pexels stock video
    2. Try Pexels stock photos
    3. Fall back to MiniMax image generation
    4. Final fallback: gradient placeholder

    Builds an HTML composition with:
    - Image background per scene
    - Ken Burns effect (slow zoom + pan)
    - Subtitle overlay at bottom
    - Total duration is driven by the job render config
    """
    render_dir.mkdir(parents=True, exist_ok=True)
    html_path = render_dir / "index.html"

    # 1. Generate scene images (Pexels primary, MiniMax fallback, gradient last)
    images_dir = render_dir / "images"
    images_dir.mkdir(exist_ok=True)
    videos_dir = render_dir / "videos"
    videos_dir.mkdir(exist_ok=True)
    # ~6s per scene now (was 10s) — matches the 抖音短片节奏 of the
    # reference video. 总时长除以 6 而不是 10，scene 数会更多、字幕更密。
    n_scenes = min(22, max(15, round(total_duration / 6)))
    # RC3 pipeline-order fix: cache chunks AND n_scenes so the post-narrate
    # re-render uses the same split as the pre-narrate render. The
    # re-render sees total_duration = voice_duration (could be ±10s off
    # from the pre-narrate estimate), and n_scenes = round(total/6)
    # would shift, breaking the scene_times alignment lookup. The first
    # render writes the cache, every subsequent render reads it.
    chunks_cache = render_dir / "chunks.json"
    if chunks_cache.exists():
        try:
            cached = json.loads(chunks_cache.read_text(encoding="utf-8"))
            if isinstance(cached, list) and all(isinstance(c, str) for c in cached):
                chunks = cached
                if len(chunks) != n_scenes:
                    log(f"  using cached n_chunks={len(chunks)} (overrides computed n_scenes={n_scenes})")
                    n_scenes = len(chunks)
            else:
                chunks = split_script_to_cards(script_text, n_cards=n_scenes)
                chunks_cache.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            chunks = split_script_to_cards(script_text, n_cards=n_scenes)
            chunks_cache.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
    else:
        chunks = split_script_to_cards(script_text, n_cards=n_scenes)
        chunks_cache.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
    log(f"  using {n_scenes} scenes for {len(script_text)} chars ({total_duration}s)")
    log(f"  generating {len(chunks)} scene images (Pexels → MiniMax → gradient)...")

    # 1a. LLM 关键词抽取（一次性批量调用，缓存）
    # 让"大脑六成是脂肪"配出大脑图而不是饮料杯
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from extract_scene_keywords import extract_keywords
        keywords_per_scene = extract_keywords(job_id, theme, chunks)
        if any(keywords_per_scene):
            log(f"  ✓ LLM extracted keywords for {sum(1 for k in keywords_per_scene if k)}/{len(chunks)} scenes")
    except Exception as e:
        log(f"  ⚠ keyword extraction failed: {e}; using regex fallback")
        keywords_per_scene = [[] for _ in chunks]

    media_items = []
    # Job seed for Pexels offset (per-scene variety, same job reruns are idempotent)
    try:
        job_seed = sum(ord(c) for c in job_id) % 7
    except Exception:
        job_seed = 0
    for i, chunk in enumerate(chunks):
        # Pad chunk (split_script_to_cards trailing ""): skip Pexels and
        # write a cheap gradient placeholder. build_image_composition_html
        # also skips pad scenes (their chunk is empty, so the HTML side
        # never reads these images) — the gradient just keeps media_items
        # positionally aligned with chunks.
        if not chunk or not chunk.strip():
            img_path = images_dir / f"scene_{i+1}.jpg"
            if not (img_path.exists() and img_path.stat().st_size > 5000):
                create_fallback_image(
                    img_path, scene_index=i, total=len(chunks),
                    width=width, height=height,
                )
            log(f"  scene {i+1}: pad (gradient placeholder)")
            media_items.append(("image", img_path))
            continue
        # 优先用 LLM visual spec；spec 缺失时回落到正则启发式
        spec = keywords_per_scene[i] if i < len(keywords_per_scene) else {}
        # 2026-06-18 修复:当 spec avoid 含 Pexels 瞎的关键词(hand/face/
        # text/watermark 等)时,跳过 Pexels 直走 MiniMax —— Pexels API
        # 不支持 negative keyword,query 怎么调都返回 hands-on-phone。
        # 同时跳过 Pexels 缓存(否则仍会用到旧的 Pexels 图)。
        skip_pexels = _spec_skip_pexels(spec)
        if spec and spec.get("subject"):
            # spec 驱动:subject 是英文具体可拍物体;shot 整段做 Pexels
            # 取景修饰(2026-06-18 修复:之前只取 shot.split()[0],丢掉了
            # "close-up" 这种关键 tag,导致"phone screen ... extreme"返
            # 回 hands-on-phone 而不是屏幕特写)。
            parts = [spec["subject"]]
            shot_full = " ".join((spec.get("shot") or "").split())
            if shot_full and shot_full.lower() not in spec["subject"].lower():
                parts.append(shot_full)
            base_query = " ".join(parts)[:80]
        else:
            base_query = extract_pexels_query(chunk, theme, i)
        offset = (i * 3 + job_seed) % 5
        # Use real motion footage for roughly one third of the scenes.
        # skip_pexels 不挡 Pexels 视频:Pexels 视频(stopwatch/抽象动效)
        # 不像 Pexels 图片那样被"人手"主导,值得保留。Pexels 视频失败时
        # 自然 fall through 到下面的 MiniMax 静态图路径。
        if i % 3 == 1:
            video_path = videos_dir / f"scene_{i+1}.mp4"
            if (
                (video_path.exists() and video_path.stat().st_size > 100_000)
                or try_pixabay_video(base_query, video_path, width, height, offset=offset)
                or try_pexels_video(base_query, video_path, width, height, offset=offset)
            ):
                log(f"  scene {i+1}: stock video (q={base_query!r}, offset={offset})")
                media_items.append(("video", video_path))
                continue
        img_path = images_dir / f"scene_{i+1}.jpg"
        # cache only honored when not skip_pexels — Pexels cache is "stale"
        # by new rules when spec says avoid hands/face/text.
        if not skip_pexels and img_path.exists() and img_path.stat().st_size > 5000:
            log(f"  scene {i+1}: cached")
            media_items.append(("image", img_path))
            continue
        # 1. Try Pixabay (主源) → Pexels (备)
        if not skip_pexels and (
            try_pixabay_image(base_query, img_path, width=width, height=height, offset=offset)
            or try_pexels_image(base_query, img_path, width=width, height=height, offset=offset)
        ):
            log(f"  scene {i+1}: stock (q={base_query!r}, offset={offset})")
            media_items.append(("image", img_path))
            continue
        # 1b. spec 缺失时，再试 chunk 原文本（中文 stock 兜底）
        if not (spec and spec.get("subject")) and not skip_pexels:
            fallback_q = extract_pexels_query(chunk, theme, i)
            if (
                try_pixabay_image(fallback_q, img_path, width=width, height=height, offset=offset)
                or try_pexels_image(fallback_q, img_path, width=width, height=height, offset=offset)
            ):
                log(f"  scene {i+1}: stock fallback (q={fallback_q!r})")
                media_items.append(("image", img_path))
                continue
        # 2. MiniMax (Pexels 跳过时,这里是主路径;否则是 Pexels miss 后的兜底)
        if skip_pexels:
            log(
                f"  scene {i+1}: spec avoid={spec.get('avoid', '')[:60]!r} "
                f"contains Pexels-blind keywords, going to MiniMax"
            )
        else:
            log(f"  scene {i+1}: pexels miss, trying MiniMax...")
        vp = build_visual_prompt(chunk, theme, scene_index=i, total=len(chunks), spec=spec)
        if try_minimax_image(
            vp["prompt"], img_path, width=width, height=height,
            negative_prompt=vp.get("negative_prompt", ""),
        ):
            log(f"  scene {i+1}: minimax")
            media_items.append(("image", img_path))
            continue
        # 3. Gradient fallback
        log(f"  scene {i+1}: both miss, using gradient")
        create_fallback_image(
            img_path, scene_index=i, total=len(chunks), width=width, height=height
        )
        media_items.append(("image", img_path))

    # 1c. 决定场景类型 — 30% kinetic（数字/短句用 hyperframes 原生渲染）
    # 这样 hyperframes 的 kinetic typography、动画数字、scene transitions
    # 才有发挥空间，而不是 100% 都是 Pexels 网图当幻灯片。
    media_items = _enrich_with_kinetic(media_items, chunks, width, height)

    # 2. Build HTML with images + Ken Burns + subtitles
    # RC3 fix: when alignment.json exists (TTS server provides per-char
    # timestamps via subtitle_enable=true), size each scene to the actual
    # TTS span of its text rather than equal-time. Without this, scene i's
    # caption appears 0.5-1s before/after TTS actually reads that text.
    scene_times = _load_alignment_scene_times(job_id, chunks, total_duration)
    # RC3+ fix: sub-captions within a scene also need to follow TTS pacing,
    # not just the scene boundary. Otherwise we get "TTS finished the
    # sentence but the next sub-caption hasn't appeared" or "next sub is
    # already showing while TTS is still mid-sentence".
    subtimes = (
        _load_alignment_subtimes(job_id, scene_times, chunks)
        if scene_times
        else None
    )

    html = build_image_composition_html(
        media_items,
        chunks,
        total_duration=total_duration,
        width=width,
        height=height,
        scene_times=scene_times,
        subtimes=subtimes,
    )
    html_path.write_text(html, encoding="utf-8")
    log(f"  generated HTML with {len(chunks)} scenes ({len(script_text)} chars)"
        + (f" [alignment-driven, {len(scene_times)} spans, "
           f"{sum(1 for s in subtimes or [] if s)}/{len(chunks)} scenes with sub-caption alignment]"
           if scene_times and subtimes else
           f" [alignment-driven, {len(scene_times)} spans]" if scene_times else " [equal-time]"))

    out_mp4 = render_dir / "video-only.mp4"

    for cmd in [
        ["npx", "--yes", "hyperframes@0.6.89", "lint"],
        ["npx", "--yes", "hyperframes@0.6.89", "validate"],
    ]:
        result = subprocess.run(
            cmd, cwd=render_dir, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed: {(result.stderr or result.stdout)[-500:]}")

    result = subprocess.run(
        [
            "npx", "--yes", "hyperframes@0.6.89", "render",
            "--fps", str(fps),
            "--workers", "1",
            "--low-memory-mode",
            "--output", str(out_mp4),
        ],
        cwd=render_dir, capture_output=True, text=True,
        timeout=RENDER_TIMEOUT_SEC,
    )
    if result.returncode != 0:
        raise RuntimeError(f"hyperframes render failed: {(result.stderr or result.stdout)[-1000:]}")
    if not out_mp4.exists():
        raise RuntimeError("render exit 0 but video-only.mp4 missing")
    # out_mp4.exists() can lie: hyperframes writes the file but the moov
    # atom may not be relocated yet, leaving an unplayable mp4. ffprobe
    # catches that — refuse to ship a video that no player can decode.
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-count_packets",
         "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_packets",
         "-of", "csv=p=0",
         str(out_mp4)],
        capture_output=True, text=True, timeout=30,
    )
    if probe.returncode != 0 or not (probe.stdout or "").strip():
        raise RuntimeError(
            f"hyperframes mp4 unreadable (ffprobe failed): "
            f"{(probe.stderr or '')[-500:]}"
        )
    return out_mp4


def try_pexels_video(query, out_path, width, height, timeout=120, offset=0):
    """Try to fetch a Pexels video clip. Returns True on success."""
    try:
        result = subprocess.run(
            [
                "python3", str(PEXELS_VIDEO_SCRIPT),
                "--query", query,
                "--out", str(out_path),
                "--w", str(width),
                "--h", str(height),
                "--offset", str(offset),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return (
            result.returncode == 0
            and out_path.exists()
            and out_path.stat().st_size > 100_000
        )
    except Exception:
        return False


def try_pexels_image(query, out_path, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT, timeout=30, offset=0):
    """Try to fetch a Pexels image. Returns True on success."""
    try:
        result = subprocess.run(
            ["python3", str(PEXELS_IMAGE_SCRIPT),
             "--query", query,
             "--out", str(out_path),
             "--w", str(width), "--h", str(height),
             "--per-page", "3", "--offset", str(offset)],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 5000:
            return True
        return False
    except Exception:
        return False


def try_pixabay_video(query, out_path, width, height, timeout=120, offset=0):
    """Try to fetch a Pixabay video clip. Returns True on success."""
    try:
        result = subprocess.run(
            [
                "python3", str(PIXABAY_VIDEO_SCRIPT),
                "--query", query,
                "--out", str(out_path),
                "--w", str(width),
                "--h", str(height),
                "--offset", str(offset),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return (
            result.returncode == 0
            and out_path.exists()
            and out_path.stat().st_size > 100_000
        )
    except Exception:
        return False


def try_pixabay_image(query, out_path, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT, timeout=30, offset=0):
    """Try to fetch a Pixabay image. Returns True on success."""
    try:
        result = subprocess.run(
            ["python3", str(PIXABAY_IMAGE_SCRIPT),
             "--query", query,
             "--out", str(out_path),
             "--w", str(width), "--h", str(height),
             "--per-page", "3", "--offset", str(offset)],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 5000:
            return True
        return False
    except Exception:
        return False


def try_minimax_image(
    prompt, out_path, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT,
    timeout=120, negative_prompt="",
):
    """Try to generate a MiniMax image. Returns True on success.

    `negative_prompt` is forwarded to MiniMax as a dedicated field when
    non-empty (image-01 honors it more reliably than `avoid:` baked into
    the positive prompt). Empty string omits the field entirely.
    """
    try:
        cmd = [
            "python3", str(IMAGE_GEN_SCRIPT),
            "--prompt", prompt,
            "--aspect", aspect_ratio_for_dimensions(width, height),
            "--n", "1",
            "--out", str(out_path),
        ]
        if negative_prompt:
            cmd += ["--negative-prompt", negative_prompt]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 5000
    except Exception:
        return False


# Pexels search API 不支持 negative keyword —— 它只能正向 tag-match,
# 没办法说"不要 hands/face/text/watermark"。当 L2 spec 的 avoid 字段
# 包含这些项时,无论 query 怎么调,Pexels 都会把含 hands/face 的库存图
# 排前面(因为 "phone screen" 这类 query 的库存图就是人手拿手机)。
# 此时应跳过 Pexels,直接走 MiniMax(支持 negative_prompt)。
#
# 这里列出的是 Pexels 真正"瞎"的关键词。构图/色调/景深类的(如
# "busy background", "warm light")Pexels 用 query 修饰能凑合避开,
# 不在此列。
PEXELS_BLIND_AVOID_KEYWORDS = (
    "people", "person", "human", "face", "facial", "head",
    "hand", "hands", "finger", "fingers", "skin", "palm",
    "text", "typography", "lettering", "watermark", "watermarks",
    "logo", "logos", "brand", "branding",
)


def _spec_skip_pexels(spec):
    """Return True if spec's avoid field has keywords Pexels can't filter.

    用 word-boundary 正则匹配,避免 "handy" 误匹配 "hand"、
    "non-human" 误匹配 "human" 等子串问题。
    """
    if not spec:
        return False
    avoid = (spec.get("avoid") or "").lower()
    if not avoid.strip():
        return False
    for kw in PEXELS_BLIND_AVOID_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", avoid):
            return True
    return False


def extract_pexels_query(chunk_text, theme, scene_index):
    """Extract a Pexels search query from a script chunk.

    Strategy: use theme as the primary anchor (consistent visual style),
    enrich with chunk keywords for variety, add per-scene hint.
    Pexels handles Chinese queries (老人 → 2500+ results).
    """
    import re as _re
    text = _re.sub(r"[，。！？、,.!?\s\d]+", " ", chunk_text).strip()
    cn_chars = [c for c in text if '\u4e00' <= c <= '\u9fff']
    # Per-scene variation hints (different angles/subjects to avoid all-same-photo)
    scene_hints = [
        "portrait", "close-up", "wide", "detail", "outdoor",
        "candid", "interior", "still life", "landscape", "urban",
    ]
    hint = scene_hints[scene_index % len(scene_hints)]

    if cn_chars:
        # Take 2-4 chinese chars as the core subject
        cn_part = "".join(cn_chars[:4])
        # Mix subject + theme + hint
        parts = [cn_part]
        if theme:
            theme_chars = [c for c in theme if '\u4e00' <= c <= '\u9fff']
            if theme_chars:
                theme_short = "".join(theme_chars[:3])
                if theme_short != cn_part:
                    parts.append(theme_short)
        parts.append(hint)
        return " ".join(parts)[:35]
    # No Chinese in chunk: use theme + hint
    if theme:
        return f"{theme} {hint}"[:35]
    return hint


def build_visual_prompt(chunk_text, theme, scene_index, total, spec=None):
    """Build a visual prompt for MiniMax image generation.

    When `spec` is provided (from extract_scene_visual_specs), it drives
    subject / shot / mood / color_palette / avoid. Otherwise we fall
    back to a chunk-text + theme + rotating-shot heuristic.

    MiniMax image-01 supports English and Chinese in the same prompt;
    structured English is more reliable for camera/mood vocabulary.
    """
    # Style: cinematic 抖音-quality, vertical
    style = (
        "cinematic photography, professional quality, "
        "warm natural lighting, shallow depth of field, "
        "35mm film grain, 9:16 vertical composition"
    )

    if spec and spec.get("subject"):
        # Spec-driven: build prompt from the 5-field structured spec.
        shot = spec.get("shot") or "medium shot"
        subject = spec["subject"]
        mood = spec.get("mood") or ""
        palette = spec.get("color_palette") or ""
        avoid = spec.get("avoid") or ""
        prompt = f"{shot}, {subject}, {mood}, {palette}, {style}"
        if avoid:
            prompt = f"{prompt}, avoid: {avoid}"
    else:
        # Fallback: rotating shot + chunk text (legacy v1 path).
        shot_variations = [
            "wide establishing shot",
            "medium shot with context",
            "close-up detail shot",
            "extreme close-up texture",
            "wide shot from a different angle",
            "over-the-shoulder perspective",
            "low angle dramatic shot",
            "portrait framing centered",
            "object still life composition",
            "wide cinematic vista",
        ]
        shot = shot_variations[scene_index % len(shot_variations)]
        subject = chunk_text.strip()[:60].rstrip("。，！？,.!? ")
        if not subject:
            subject = theme[:30] if theme else "atmospheric scene"
        prompt = f"{shot}, {subject}, {style}"

    # Cap prompt length (API limit ~2000 chars)
    if len(prompt) > 500:
        prompt = prompt[:500]
    # Split `avoid` out into a separate negative_prompt — image-01's
    # `negative_prompt` field is more reliable than `avoid: ...` baked
    # into the positive prompt (verified 2026-06: prompt-only avoid was
    # ignored ~30% of the time; negative_prompt never produced a face
    # in our test set of stock/clock/cross-section subjects).
    negative = ""
    if spec and spec.get("subject") and spec.get("avoid"):
        negative = spec["avoid"]
        # Strip the trailing `avoid: ...` clause we appended above so we
        # don't double-state it. We appended a single ", avoid: <avoid>"
        # at the end; reverse that surgically. Splitting on the marker is
        # more robust than regex (which broke on commas inside the avoid
        # list like "people, faces, text").
        marker = ", avoid: "
        idx = prompt.rfind(marker)
        if idx != -1:
            prompt = prompt[:idx].rstrip(" ,")
    return {"prompt": prompt, "negative_prompt": negative}


def aspect_ratio_for_dimensions(width, height):
    if width == height:
        return "1:1"
    return "16:9" if width > height else "9:16"


def create_fallback_image(
    out_path, scene_index, total, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT
):
    """Generate a gradient PNG as fallback when image gen fails."""
    try:
        from PIL import Image, ImageDraw
        palette = [
            (30, 58, 138), (124, 58, 237), (236, 72, 153),
            (245, 158, 11), (16, 185, 129), (14, 165, 233),
            (99, 102, 241), (220, 38, 38), (217, 119, 6),
            (5, 150, 105),
        ]
        c1, c2 = palette[scene_index % len(palette)], palette[(scene_index + 1) % len(palette)]
        img = Image.new("RGB", (width, height), c1)
        draw = ImageDraw.Draw(img)
        for y in range(height):
            t = y / height
            r = int(c1[0] * (1 - t) + c2[0] * t)
            g = int(c1[1] * (1 - t) + c2[1] * t)
            b = int(c1[2] * (1 - t) + c2[2] * t)
            draw.line([(0, y), (width, y)], fill=(r, g, b))
        img.save(out_path, "JPEG", quality=85)
    except ImportError:
        # PIL not available: just write empty
        out_path.write_bytes(b"")


def _load_alignment_scene_times(job_id, chunks, total_duration):
    """Map each split chunk to a (start, end) tuple from alignment.json.

    alignment.json is built by process_video_narrate_jobs.
    _build_alignment_from_tts_subs from the TTS server's per-word
    timestamps. We need to translate the *n_scenes* (which were produced
    by split_script_to_cards — equal-chunk-size slices) to those
    per-sentence timestamps so the rendered video's scenes match the
    TTS pacing.

    Strategy: each chunk contains some number of original sentences from
    script.txt. Find the earliest start and latest end among the alignment
    sentences whose text appears within that chunk. This gives a tight
    start/end pair per chunk.

    Returns a list of (start, end) tuples the same length as chunks, or []
    if no alignment is available (caller should fall back to equal-time).
    """
    run_dir = SKILL_DIR / "runs" / job_id
    aln_path = run_dir / "alignment.json"
    if not aln_path.exists():
        return []
    try:
        aln = json.loads(aln_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    sentences = aln.get("sentences") or []
    if not sentences:
        return []

    # For each sentence, build a strip()d version for substring match.
    sent_clean = [s["text"].strip() for s in sentences]
    # Per-sentence spans (in seconds), start/end in absolute voice time.
    sent_spans = [(s["start"], s["end"]) for s in sentences]

    scene_times: list[tuple[float, float]] = []
    cursor = 0  # walk through sentences, don't re-match
    # Normalize whitespace for both sides — TTS preserves punctuation
    # but the chunk may differ from the sentence text by 1-2 chars
    # (e.g. trailing punctuation, line breaks from split_script_to_cards).
    def _norm(s: str) -> str:
        return "".join(s.split())

    sent_norm = [_norm(s) for s in sent_clean]
    for chunk in chunks:
        if not chunk:
            # Pad scene (split_script_to_cards fills the tail with "" when
            # n_scenes > n_sentences). Mark as None so build_image_composition_html
            # can slot it at the previous cursor and emit a zero-length
            # clip — (0.0, 0.0) here would later become a `starts` entry
            # that resets the cumulative timeline to zero, turning all
            # intervening per_this values negative.
            scene_times.append(None)
            continue
        chunk_norm = _norm(chunk)
        # Find the first sentence (≥cursor) whose normalized text overlaps
        # the normalized chunk.
        first = None
        last = None
        for j in range(cursor, len(sent_norm)):
            stxt = sent_norm[j]
            if not stxt:
                continue
            # Substring match in either direction, or large overlap
            # (e.g. chunk contains first 30 chars of a 60-char sentence).
            if stxt in chunk_norm or chunk_norm in stxt:
                if first is None:
                    first = sent_spans[j][0]
                last = sent_spans[j][1]
                cursor = j + 1
            elif first is not None and (stxt[:20] not in chunk_norm and chunk_norm[:20] not in stxt):
                # Already matched some, this sentence clearly unrelated
                break
        if first is None:
            scene_times.append(None)
        else:
            scene_times.append((first, last))
    # If any non-empty chunk missed, drop back to equal-time for everything
    # (cleaner than mixing). Pad chunks (None from the empty-chunk branch
    # above) are allowed — they get a zero-length slot in the timeline.
    real_chunks_missed = [
        i for i, (c, t) in enumerate(zip(chunks, scene_times)) if c and t is None
    ]
    if real_chunks_missed:
        return []
    # Clamp: last real scene must end exactly at total_duration so the
    # video doesn't run short. (If the last chunk is a pad from
    # split_script_to_cards trailing empty chunks, scene_times[-1] is
    # None and we leave the cursor to handle it downstream.)
    if scene_times and scene_times[-1] is not None:
        scene_times[-1] = (scene_times[-1][0], float(total_duration))
    return scene_times


# Split-priority chars for `_split_sentence_into_subs`. Order = preference:
# (punctuation, particles, pronouns, common 2-3 char words).
# Include both full-width and ASCII punctuation. LLM writes English
# commas/periods (Pexels/MiniMax prompts default), so ASCII versions
# must be honored or `_split_sentence_into_subs` will silently skip
# real sentence boundaries and fall back to mid-phrase particle cuts
# (e.g. splitting "今天算的是,你这一辈子,被房贷偷走的购买力" at
# the "的" particle instead of either comma).
_SPLIT_PUNCT = "。！？，；：、,?!"
_SPLIT_PARTICLES = "的了着过啊吧呢嘛呀"
_SPLIT_PRONOUNS = "我你他她它们"
_SPLIT_COMMON_WORDS = (
    "自己", "百分之", "九十", "萤火虫", "鮟鱇鱼", "共生", "深海", "灯光",
    "太阳光", "灯笼鱼", "百分之九十", "海面", "天空", "大地", "生物",
)


def _split_sentence_into_subs(text: str, max_chars: int, hard_max: int) -> list[str]:
    """v9: split at every _SPLIT_PUNCT boundary (one sub per clause).

    The user wants strict per-punctuation splitting regardless of sub
    length: each `,` `、` `。` etc. boundary becomes its own sub-caption.
    Downstream `wrap_caption_lines(max_chars=28, max_lines=1)` enforces
    single-line display (16:9); 9:16 uses 16. Overflow shows "…" truncation.

    Single clauses that exceed max_chars (no PUNCT inside, e.g. dense
    "中间隔了2次封神、3次朝堂清洗、5次人间王朝更替" 27 chars between
    outer commas) fall through to `_split_long_clause` (the v7-v8.1
    length-based candidate scan + hard-cut) for that clause.

    Short sentences (≤ max_chars) stay whole — even if they have internal
    PUNCT, no need to fragment a 12-char "你劝年轻人早睡," into two subs.

    Single-clause sentences with no _SPLIT_PUNCT boundary (e.g. dense
    "7 年内的死亡率比继续参与工作的/组高出 2.3 倍" — `/` and decimal `.`
    are NOT in _SPLIT_PUNCT) also stay whole regardless of length. Downstream
    wrap_caption_lines handles multi-line display. Fragmenting a single
    semantically-complete clause at PARTICLE positions like 的/了 produces
    worse subtitles than letting it render as 2 lines.
    """
    if len(text) <= max_chars:
        return [text]

    # Single-clause (no PUNCT boundary) → keep whole. v9's principle is
    # "one sub per clause"; a clause without boundaries is one clause.
    if not any(ch in _SPLIT_PUNCT for ch in text):
        return [text]

    # 1. Greedy split at every PUNCT char. Each clause includes its
    # trailing PUNCT so wrap can strip it for display but it stays in
    # the raw sub for alignment.json char-index lookup.
    clauses: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _SPLIT_PUNCT:
            clauses.append(buf)
            buf = ""
    if buf:
        clauses.append(buf)

    # 2. Each clause is its own sub. When we hit a clause > max_chars
    # (e.g. "一片褪黑素30块催眠6小时——清醒1小时成本只有睡眠的1/3,"
    # 31 chars between outer commas), delegate the rest of the clause
    # stream to `_split_long_clause` (the v7-v8.1 length-based splitter).
    # `_split_long_clause` is PUNCT-aware (range_kind_hard accepts PUNCT
    # past hard_max), so it can cut at `1/3,` etc. inside the over-long
    # clause. We DON'T re-process the remaining clauses in this loop
    # — `_split_long_clause` already absorbed their text into its output.
    out: list[str] = []
    for j, clause in enumerate(clauses):
        stripped = clause.strip()
        if not stripped:
            continue
        if len(stripped) > max_chars:
            # Hand the rest of the clause stream to _split_long_clause.
            # It scans for internal PUNCT (range_kind_hard) and falls
            # back to PARTICLE/PRONOUN + mid-CJK hard-cut.
            remaining = "".join(clauses[j:])
            out.extend(_split_long_clause(remaining, max_chars, hard_max))
            return out
        out.append(clause)
    return out


def _split_long_clause(text: str, max_chars: int, hard_max: int) -> list[str]:
    """v7-v8.1 length-based split: used when a PUNCT-delimited clause is
    still over max_chars (e.g. dense "中间隔了2次封神、3次朝堂清洗、5次
    人间王朝更替" 27 chars with internal `、` PUNCT but no outer boundary
    below max_chars). Falls through to PARTICLE/PRONOUN candidate scan,
    then mid-CJK hard-cut with back-up to nearest PUNCT/space.
    """
    first_safe = max(4, len(text) // 3)
    candidates: list[tuple[int, int, int]] = []
    range_kind_soft = 0
    range_kind_hard = 1
    allow_hard = len(text) > 2 * hard_max
    for i in range(first_safe, len(text) - 3 if allow_hard else min(len(text), hard_max)):
        prev_ch = text[i - 1] if i > 0 else ""
        next_ch = text[i] if i < len(text) else ""
        in_soft = i < hard_max
        if prev_ch in _SPLIT_PUNCT:
            candidates.append((0, i, range_kind_soft if in_soft else range_kind_hard))
        elif prev_ch in _SPLIT_PARTICLES and in_soft:
            candidates.append((1, i, range_kind_soft))
        elif next_ch in _SPLIT_PRONOUNS and i + 1 < len(text) and in_soft:
            candidates.append((2, i, range_kind_soft))
    has_punct_soft = any(rk == range_kind_soft and p == 0 for p, _, rk in candidates)
    if not has_punct_soft:
        early_limit = max(4, first_safe - 4)
        for i in range(early_limit, first_safe):
            prev_ch = text[i - 1] if i > 0 else ""
            if prev_ch in _SPLIT_PUNCT:
                candidates.append((0, i, range_kind_soft))
    if candidates:
        best = None
        for prio, end, rk in candidates:
            tail_len = len(text) - end
            if tail_len < 5:
                continue
            head_len = end
            if head_len > 2 * hard_max:
                continue
            if head_len > max_chars:
                continue
            head_penalty = 0 if head_len <= max_chars else (head_len - max_chars) * 10
            balance_diff = abs(max(head_len, tail_len) - max_chars / 2)
            tail_penalty = 0 if tail_len <= max_chars else (tail_len - max_chars) * 5
            range_excess = max(0, end - hard_max) if rk == range_kind_hard else 0
            tail_char = text[end - 1] if end > 0 else ""
            head_orphan_penalty = 0 if (tail_char in _SPLIT_PUNCT or tail_char == " ") else 5
            key = (rk, prio, head_penalty, balance_diff, tail_penalty, range_excess, head_orphan_penalty)
            if best is None or key < best[0]:
                best = (key, end)
        if best is not None:
            tail = text[best[1]:]
            if len(tail) > max_chars:
                # Recurse with `_split_long_clause` (not `_split_sentence_
                # into_subs`) — v9's greedy PUNCT split would re-enter
                # the same long clause and risk infinite ping-pong.
                return [text[:best[1]], *_split_long_clause(tail, max_chars, hard_max)]
            return [text[:best[1]], text[best[1]:]]
    if len(text) <= hard_max:
        return [text]
    end = hard_max
    back_limit = max(2, first_safe - 4)
    while end > back_limit:
        prev_ch = text[end - 1] if end > 0 else ""
        next_ch = text[end] if end < len(text) else ""
        if prev_ch in _SPLIT_PUNCT or prev_ch == " " or next_ch in _SPLIT_PUNCT or next_ch == " ":
            break
        end -= 1
    for word in _SPLIT_COMMON_WORDS:
        if end >= len(word) and text[end - len(word):end] == word:
            end -= len(word)
            break
    return [text[:end], *(_split_long_clause(text[end:], max_chars, hard_max))]


# Sub-caption text cleanup: replace Chinese punctuation with a single
# space (so word boundaries remain readable) and collapse repeated
# whitespace. The time projection in `_load_alignment_subtimes` still
# walks the original sub_text (with punctuation) so slot_start/slot_end
# keep tracking TTS character positions.
# Strip visual-noise punctuation from subtitles.
# Note: '.' and '%' are INTENTIONALLY kept — `.` is part of decimal numbers
# (1.8 → tokenized as one), `%` is part of percentages (40% → one token).
# ec82ace commit fixed wrap so "1.8"/"40%" survive — do not regress that.
_PUNCT_TO_SPACE = str.maketrans({c: " " for c in "，。！？；：、　,?!;:;\"'()[]{}<>"})
_ELLIPSIS_TO_SPACE = str.maketrans({c: " " for c in "…⋯"})


def _strip_punctuation(text: str) -> str:
    """Replace Chinese punctuation and overflow ellipsis markers with spaces,
    then collapse runs of whitespace."""
    text = text.translate(_PUNCT_TO_SPACE).translate(_ELLIPSIS_TO_SPACE)
    return " ".join(text.split())


def _load_alignment_subtimes(job_id, scene_times, chunks, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT):
    """Per-scene sub-caption (text + timing) driven by alignment.sentences.

    Returns a list parallel to chunks; for each scene, a list of
    (lines, slot_start, slot_end) tuples in *absolute* video time. Each
    `lines` is a list[str] (the sub-caption's line list, e.g. ["海底发光的鱼"]),
    and slot_start/slot_end are the absolute start/end in seconds.

    The caller MUST use these lines for the subtitle text (not
    wrap_to_subcaptions(chunk)) so that the rendered n_subs matches the
    alignment-driven count exactly. If the caller used
    wrap_to_subcaptions(chunk), the sub count would differ (because
    `chunk` and the contained sentence list produce different splits
    when a sentence crosses the 20-char boundary), and the alignment-
    driven slot list would silently fail to apply.

    Empty list (len 0 for a scene) means "no alignment available for
    this scene; fall back to equal-time within the scene".

    Without this, sub-captions get equal-time within a scene (e.g. 60
    chars / 3 = 20 chars per slot, then time split 3 ways) — but TTS
    doesn't speak equal time per 20 chars (it pauses on punctuation,
    runs through numbers faster, etc.). The result: TTS finishes a
    sub-caption's text but the next one hasn't appeared yet, OR the
    next sub-caption is already up while TTS is still mid-sentence.
    By using per-sentence alignment spans, sub-captions track the real
    cadence of the voice.
    """
    if not scene_times:
        return None
    run_dir = SKILL_DIR / "runs" / job_id
    aln_path = run_dir / "alignment.json"
    if not aln_path.exists():
        return None
    try:
        aln = json.loads(aln_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    sentences = aln.get("sentences") or []
    if not sentences:
        return None

    sent_spans = [(s["start"], s["end"]) for s in sentences]
    # Sub-caption char budget.
    #   max_chars: single-line cap — wrap will not split text ≤ this len.
    #              Set generously (20/12) to keep short phrases whole.
    #   hard_max:  hard ceiling on every produced sub-chunk. v6 sets this
    #              EQUAL to max_chars so any sentence > max_chars must be
    #              split (semantic split if PUNCT/PARTICLE candidate exists
    #              in [first_safe, max_chars), else hard-cut at max_chars).
    #              Without this, sentences 21-30 chars with no good split
    #              in the soft range are returned whole, then wrap splits
    #              them at midpoint into 2 lines — the exact "2-line
    #              subtitle" bug the user keeps reporting.
    # The split is done by `_split_sentence_into_subs`, which prefers
    # semantic boundaries (punctuation > particles > pronouns > common
    # words) over hard char-cut positions.
    # Bumped max_chars 18→20 (v4) and hard_max 24→30 (v6) to match the
    # wrap_caption_lines cap: short sentences ≤ 20 chars (after
    # punctuation strip) stay whole, and longer sentences with multiple
    # commas can split at the EARLIEST comma rather than a weak fallback
    # (e.g. "你以为房贷只是利息高,其实真正贵的,是复利,是时间,..." was
    # splitting at the 3rd comma giving a 22-char head that wrapped to
    # 2 visual lines; with hard_max=20 we hard-cut at 20 so every sub-chunk
    # is guaranteed to display ≤ max_chars — never 2 visual lines).
    max_chars = 28 if width >= height else 16
    hard_max = max_chars
    MIN_SUB_DUR = 0.3  # floor: a sub-caption must stay on screen >=300ms
    SUB_GAP = 0.04     # visual gap between consecutive sub-captions

    # Char-level alignment (1:1 with script non-whitespace chars). Used
    # to look up actual TTS time for each sub-caption's first/last char.
    char_entries = aln.get("chars") or []
    script_path = run_dir / "script.txt"
    script_chars: list[str] = []
    if script_path.exists():
        script_chars = [c for c in script_path.read_text(encoding="utf-8").strip() if c.strip() and c != "\n"]
    if len(char_entries) != len(script_chars):
        # Lengths disagree (shouldn't happen, but guard against it) —
        # fall back to equal-time within sentence for the whole scene.
        char_entries = []

    out: list[list[tuple[list[str], float, float]]] = []
    for scene_span, chunk in zip(scene_times, chunks):
        if scene_span is None or not chunk:
            # Pad scene (split_script_to_cards trailing "" when
            # n_scenes > n_sentences) or empty chunk — no subs.
            out.append([])
            continue
        scene_start, scene_end = scene_span
        scene_dur = scene_end - scene_start
        if scene_dur <= 0:
            out.append([])
            continue
        # Include any sentence that overlaps with the scene, not just
        # ones entirely contained within it. The previous strict filter
        # (`a >= scene_start-0.05 and b <= scene_end+0.05`) dropped
        # sentences that crossed the scene boundary — e.g. a 6s sentence
        # starting at scene_end-2s would vanish from both scenes' subs
        # even though its middle 4s falls inside scene N and its tail 2s
        # falls inside scene N+1. For cross-boundary sentences the subs
        # are clipped to the scene by the `else` branch below (or by
        # clip_subs in the preview path), so the same sentence appearing
        # in both scenes' contained_idx is harmless — each scene gets
        # the portion that falls inside its time range.
        contained_idx = [
            j for j, (a, b) in enumerate(sent_spans)
            if a < scene_end + 0.05 and b > scene_start - 0.05
            and a < scene_end and b > scene_start
        ]
        if not contained_idx:
            out.append([])
            continue
        scene_subs: list[tuple[list[str], float, float]] = []
        if not char_entries:
            # No char-level data — fall back to per-sentence proportional
            # allocation using _split_sentence_into_subs (semantic split
            # at punctuation > particles > pronouns > common words).
            for j in contained_idx:
                sent_a, sent_b = sent_spans[j]
                sent_text_j = sentences[j]["text"].strip()
                sent_dur = max(sent_b - sent_a, 0.0)
                sent_len = len(sent_text_j) or 1
                sub_chunks = _split_sentence_into_subs(sent_text_j, max_chars, hard_max)
                cursor_in_sent = 0
                for sub_text in sub_chunks:
                    if not sub_text:
                        continue
                    char_start = cursor_in_sent
                    char_end = cursor_in_sent + len(sub_text) - 1
                    if sent_dur > 0:
                        slot_start = sent_a + (char_start / sent_len) * sent_dur
                        slot_end = sent_a + ((char_end + 1) / sent_len) * sent_dur
                    else:
                        slot_start, slot_end = sent_a, sent_b
                    display_text = _strip_punctuation(sub_text)
                    if not display_text:
                        cursor_in_sent += len(sub_text)
                        continue
                    lines = wrap_caption_lines(display_text, max_chars=max_chars, max_lines=1)
                    scene_subs.append((lines, slot_start, slot_end))
                    cursor_in_sent += len(sub_text)
        else:
            # Find the script-chars index where this scene's text begins.
            # Strategy: walk the chunk's first few chars forward from
            # scene_start to find the matching position in script_chars.
            chunk_first = chunk.strip()[0] if chunk and chunk.strip() else None
            cursor_char_idx = 0
            if chunk_first:
                for i, sc in enumerate(script_chars):
                    if sc == chunk_first:
                        # Check timestamp: this char should be at or after
                        # scene_start (within tolerance).
                        if char_entries[i]["start"] >= scene_start - 0.3:
                            cursor_char_idx = i
                            break
            for j in contained_idx:
                sent_text_j = sentences[j]["text"].strip()
                if not sent_text_j:
                    continue
                # Advance cursor_char_idx until script_chars[cursor_char_idx]
                # matches sent_text_j[0] (or past end)
                while cursor_char_idx < len(script_chars) and script_chars[cursor_char_idx] != sent_text_j[0]:
                    cursor_char_idx += 1
                if cursor_char_idx >= len(script_chars):
                    # Out of alignment data — use sentence span as flat
                    sent_a, sent_b = sent_spans[j]
                    stripped = _strip_punctuation(sent_text_j)
                    if stripped:
                        lines = wrap_caption_lines(stripped, max_chars=max_chars, max_lines=1)
                        scene_subs.append((lines, sent_a, sent_b))
                    continue
                # Now walk forward through script_chars, matching sent_text_j
                # char by char, to find the [start, end] char indices for
                # this sentence.
                sent_start_idx = cursor_char_idx
                k = 0  # position in sent_text_j
                i = sent_start_idx
                while k < len(sent_text_j) and i < len(script_chars):
                    if script_chars[i] == sent_text_j[k]:
                        k += 1
                    i += 1
                sent_end_idx = i  # one past last matched char
                if k < len(sent_text_j):
                    # Couldn't match the whole sentence — fall back
                    sent_a, sent_b = sent_spans[j]
                    stripped = _strip_punctuation(sent_text_j)
                    if stripped:
                        lines = wrap_caption_lines(stripped, max_chars=max_chars, max_lines=1)
                        scene_subs.append((lines, sent_a, sent_b))
                    cursor_char_idx = sent_end_idx
                    continue

                # Split sent_text_j into sub-captions at semantic
                # boundaries. `_split_sentence_into_subs` prefers
                # punctuation > particles > pronouns > common-word
                # boundaries; short sentences (≤ max_chars) stay whole,
                # and sentences ≤ hard_max with no good split also stay
                # whole — letting TTS pace handle it instead of mid-word
                # cuts like "自己" → "自/己".
                sub_chunks = _split_sentence_into_subs(sent_text_j, max_chars, hard_max)
                # Build sub-caption entries. Each sub's slot uses the actual
                # TTS per-char timestamps from alignment.json chars[].
                # Earlier proportional-by-char-count was wrong: it ignored
                # in-sentence pauses (after ，。！？) which take ~100-300ms
                # but contribute 0 chars, causing each sub to over-run
                # into the next voice segment (visible as "subs lag behind
                # voice" especially after the 20s mark on long sentences).
                sent_a, sent_b = sent_spans[j]
                sent_dur = max(sent_b - sent_a, 0.0)
                sent_len = len(sent_text_j) or 1
                cursor_in_sent = 0
                for sub_text in sub_chunks:
                    if not sub_text:
                        continue
                    char_start = cursor_in_sent
                    char_end = cursor_in_sent + len(sub_text) - 1
                    global_start_idx = sent_start_idx + char_start
                    global_end_idx = sent_start_idx + char_end
                    if 0 <= global_start_idx < len(char_entries) and 0 <= global_end_idx < len(char_entries):
                        slot_start = char_entries[global_start_idx]["start"]
                        slot_end = char_entries[global_end_idx]["end"]
                    else:
                        # Bounds guard — fall back to proportional for this sub.
                        if sent_dur > 0:
                            slot_start = sent_a + (char_start / sent_len) * sent_dur
                            slot_end = sent_a + ((char_end + 1) / sent_len) * sent_dur
                        else:
                            slot_start, slot_end = sent_a, sent_b
                    if slot_end <= slot_start:
                        slot_end = slot_start + 0.1
                    # Strip punctuation from the rendered sub-caption (user
                    # preference — punctuation adds visual clutter over
                    # a moving video). Time projection stays on the
                    # original sub_text (with punctuation) so slot times
                    # still track TTS character positions.
                    display_text = _strip_punctuation(sub_text)
                    if not display_text:
                        # Sub was entirely punctuation — skip it; advance
                        # the cursor so subsequent subs still track
                        # correctly.
                        cursor_in_sent += len(sub_text)
                        continue
                    # Word-aware 单行 wrap (max 20 字/行) — 用项目已有的 _pack_lines 工具
                    lines = wrap_caption_lines(display_text, max_chars=max_chars, max_lines=1)
                    scene_subs.append((lines, slot_start, slot_end))
                    cursor_in_sent += len(sub_text)
                cursor_char_idx = sent_end_idx
        if not scene_subs:
            out.append([])
            continue

        # Enforce MIN_SUB_DUR floor. If a sub is shorter than MIN_SUB_DUR,
        # extend it; shift the next sub's start forward accordingly. This
        # keeps the sub legible when TTS reads very fast (e.g. 16 chars
        # in 1.15s after the 30s mark).
        adjusted: list[tuple[list[str], float, float]] = []
        for k, (s_lines, a, b) in enumerate(scene_subs):
            if b - a < MIN_SUB_DUR:
                b = a + MIN_SUB_DUR
            adjusted.append((s_lines, a, b))
        # Re-clamp last sub to scene_end — only when the sub actually
        # extends past the scene end. The previous code checked `last_a
        # < scene_end` and unconditionally set the end to scene_end,
        # which is a *fill* not a clamp: it made the last sub display
        # long after the voice had moved on (e.g. sentence [0]'s tail
        # persisted to the end of a 10s preview even though the next
        # sentence started at 4.88s). The correct behavior is truncation
        # only — if the sub fits inside the scene, leave its end alone
        # and let the gap to scene_end be silence.
        last_lines, last_a, last_b = adjusted[-1]
        if last_b > scene_end:
            adjusted[-1] = (last_lines, last_a, scene_end)
        # Apply SUB_GAP: each sub's start is the previous sub's end + gap
        # (skipping the first sub, which is clamped to scene_start).
        out_subs: list[tuple[list[str], float, float]] = []
        prev_end = scene_start
        for k, (s_lines, a, b) in enumerate(adjusted):
            if k == 0:
                a = scene_start
            else:
                a = max(a, prev_end + SUB_GAP)
            if a >= b:
                a = b - 0.01  # avoid 0-width
            out_subs.append((s_lines, a, b))
            prev_end = b
        out.append(out_subs)
    return out


def split_script_to_cards(script_text, n_cards=5):
    """Split script into ~n_cards chunks by sentence boundaries.

    Tries to keep chunks roughly balanced in length.
    """
    if not script_text:
        return [f"Card {i+1}" for i in range(n_cards)]

    # Split by Chinese sentence delimiters (。！？)
    import re as _re
    sentences = [s.strip() for s in _re.split(r'(?<=[。！？])', script_text) if s.strip()]
    if not sentences:
        sentences = [script_text]

    # If we have many sentences, group them into n_cards
    if len(sentences) <= n_cards:
        # Pad with empty
        return sentences + [""] * (n_cards - len(sentences))

    # Roughly equal distribution
    per = len(sentences) / n_cards
    chunks = []
    for i in range(n_cards):
        start = int(i * per)
        end = int((i + 1) * per) if i < n_cards - 1 else len(sentences)
        chunks.append("".join(sentences[start:end]))
    return chunks


# ── kinetic scene helpers ────────────────────────────────────────────
# 用于"数字/概念/短句"场景，绕开 Pexels 真实图库，直接用 hyperframes 原生
# 渲染（渐变背景 + 动画数字 / 大字卡片）。让 hyperframes 发挥 CSS/GSAP
# 能力，而不是"100% 用网图当幻灯片"。

PALETTES = [
    # (top, bottom) — 10 套渐变色
    ("#1a1d2e", "#3d1f3f"),  # 暗紫
    ("#0f2027", "#2c5364"),  # 深海
    ("#200122", "#6f0000"),  # 暗红
    ("#1f4037", "#99f2c8"),  # 翠绿（浅）
    ("#16222a", "#3a6073"),  # 灰蓝
    ("#3a1c71", "#d76d77"),  # 紫粉
    ("#0b486b", "#f56217"),  # 蓝橙
    ("#1e3c72", "#2a5298"),  # 蓝调
    ("#5d4157", "#a8caba"),  # 紫绿
    ("#000428", "#004e92"),  # 深蓝
]


def _palette_for(index: int) -> tuple[str, str]:
    return PALETTES[index % len(PALETTES)]


# 主标题字号（hook 钩子用）— 1920×1080 vs 1080×1920 不同档
_TITLE_FS = lambda w, h: 220 if w >= h else 280
_LABEL_FS = lambda w, h: 100 if w >= h else 130
_COUNTER_FS = lambda w, h: 480 if w >= h else 600
_UNIT_FS = lambda w, h: 140 if w >= h else 180


def _kinetic_base_css(scene_id: str) -> str:
    """共用 CSS：渐变背景 + 全屏 flex 居中 + 大字白字黑描边。"""
    return f"""
    #{scene_id} {{
      position: absolute; inset: 0;
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      text-align: center;
      color: #fff;
      text-shadow:
        -4px -4px 0 #000, 4px -4px 0 #000,
        -4px 4px 0 #000, 4px 4px 0 #000,
        -4px 0 0 #000, 4px 0 0 #000,
        0 -4px 0 #000, 0 4px 0 #000,
        0 10px 30px rgba(0, 0, 0, 0.6);
    }}
    #{scene_id} .k-label {{
      font-size: var(--k-label-fs); font-weight: 800; letter-spacing: 4px;
      margin-bottom: 24px; opacity: 0.92;
    }}
    #{scene_id} .k-counter {{
      font-size: var(--k-counter-fs); font-weight: 900; line-height: 1;
      font-variant-numeric: tabular-nums;
    }}
    #{scene_id} .k-unit {{
      font-size: var(--k-unit-fs); font-weight: 800; margin-top: 16px; opacity: 0.95;
    }}
    #{scene_id} .k-title {{
      font-size: var(--k-title-fs); font-weight: 900; line-height: 1.1;
      max-width: 86%; padding: 0 5%;
    }}
    #{scene_id} .k-title .word {{
      display: inline-block; opacity: 0; transform: translateY(20px);
    }}
    """


def build_kinetic_text_scene_html(
    text: str, scene_index: int, width: int, height: int, overlay: bool = False
) -> str:
    """纯文字 kinetic 场景 — 大白字逐词淡入。

    overlay=False (默认): 无背景图，自带渐变背景(老行为)。
    overlay=True: 作为 Pexels 图/视频上的前景层，透明 + 底部深色 scrim
    (.kinetic-overlay class) 提高文字可读性。
    """
    scene_id = f"kinetic-txt-{scene_index}"
    top, bottom = _palette_for(scene_index)
    # 按 CJK 字符 + 英文单词切词
    tokens = re.findall(r"[A-Za-z0-9]+|[一-鿿]|[^\s\w]", text)
    words_html = " ".join(f'<span class="word">{escape_html(t)}</span>' for t in tokens)
    css_vars = (
        f"--k-label-fs:{_LABEL_FS(width, height)}px;"
        f"--k-title-fs:{_TITLE_FS(width, height)}px;"
        f"--k-counter-fs:{_COUNTER_FS(width, height)}px;"
        f"--k-unit-fs:{_UNIT_FS(width, height)}px;"
    )
    if overlay:
        # 透明背景 + 底部 scrim class，由 build_image_composition_html 同级 CSS 提供
        bg_style = "background: transparent;"
        classes = "kinetic kinetic-text kinetic-overlay"
    else:
        bg_style = f"background: linear-gradient(135deg, {top} 0%, {bottom} 100%);"
        classes = "kinetic kinetic-text"
    return f"""
    <div class="{classes}" id="{scene_id}" style="
      {bg_style}
      {css_vars}
    ">
      <style>{_kinetic_base_css(scene_id)}</style>
      <div class="k-title">{words_html}</div>
      <script>
      (function() {{
        var sel = '#{scene_id} .word';
        gsap.to(sel, {{
          opacity: 1, y: 0, duration: 0.5, ease: 'power2.out',
          stagger: 0.12,
        }});
      }})();
      </script>
    </div>
    """


def build_animated_counter_scene_html(
    label: str, value: int, unit: str, scene_index: int, width: int, height: int,
    overlay: bool = False,
) -> str:
    """动画数字场景 — GSAP 从 0 滚到 value，用于"人脑六成是脂肪"之类。

    overlay=True: 透明背景 + 底部 scrim (作为 Pexels 图/视频的前景 overlay)。
    """
    scene_id = f"kinetic-num-{scene_index}"
    top, bottom = _palette_for(scene_index + 3)  # 偏移避免和 text 撞色
    css_vars = (
        f"--k-label-fs:{_LABEL_FS(width, height)}px;"
        f"--k-title-fs:{_TITLE_FS(width, height)}px;"
        f"--k-counter-fs:{_COUNTER_FS(width, height)}px;"
        f"--k-unit-fs:{_UNIT_FS(width, height)}px;"
    )
    if overlay:
        bg_style = "background: transparent;"
        classes = "kinetic kinetic-counter kinetic-overlay"
    else:
        bg_style = f"background: linear-gradient(135deg, {top} 0%, {bottom} 100%);"
        classes = "kinetic kinetic-counter"
    return f"""
    <div class="{classes}" id="{scene_id}" style="
      {bg_style}
      {css_vars}
    ">
      <style>{_kinetic_base_css(scene_id)}</style>
      <div class="k-label">{escape_html(label)}</div>
      <div class="k-counter" data-target="{value}">0</div>
      <div class="k-unit">{escape_html(unit)}</div>
      <script>
      (function() {{
        var el = document.querySelector('#{scene_id} .k-counter');
        var target = parseInt(el.getAttribute('data-target'), 10);
        var obj = {{ val: 0 }};
        gsap.to(obj, {{
          val: target, duration: 2.2, ease: 'power2.out',
          onUpdate: function() {{ el.textContent = Math.round(obj.val); }}
        }});
      }})();
      </script>
    </div>
    """


_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(%|％|倍|万|亿|岁|分钟|秒|天|月|年|公斤|克|千|百)?")
# 中文数字: "六成" "六十" "八千万" "三十" "两亿" "五百年" "第七天"
_CN_NUM_CHARS = "零一二三四五六七八九十两"
_CN_NUM_RE = re.compile(
    r"([零一二三四五六七八九十两百千]+)"
    r"\s*(%|％|倍|万|亿|岁|分钟|秒|天|月|年|公斤|克|千|百|第|成)?"
)
# 数字单位提示，用于 counter 场景
_UNIT_FROM_RE = {
    "%": "%", "％": "%", "倍": "倍", "万": "万",
    "亿": "亿", "岁": "岁", "分钟": "分钟", "秒": "秒",
    "天": "天", "月": "月", "年": "年", "公斤": "kg", "克": "g",
    "千": "k", "百": "百", "": "",
    "第": "",  # "第七天" → counter
}


def _parse_cn_num(s):
    """把中文数字串转成 int。覆盖 1-99 以及带 万/千/百 修饰的情况。

    接受: "六" "三十" "六十五" "一百" "两百" "八千" "三万" "八千万"
    未知结构或 0 都返回 1（避免动画停在 0）。
    """
    s = s.strip()
    if not s:
        return 0
    digit = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3,
             "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if s in digit:
        return digit[s]

    # 切分 "X千Y万Z" - 找最高级单位，按它分段累加
    val = 0
    for unit, mult in (("亿", 10**8), ("万", 10**4),
                       ("千", 10**3), ("百", 10**2), ("十", 10)):
        if unit in s:
            head, _, tail = s.partition(unit)
            # head 可能是 "X" / "X十" / "X千" - 递归算
            if head:
                h = _parse_cn_num(head)
            elif unit == "十":
                h = 1  # "十五" → 1*10 + 5
            else:
                h = 1
            val += h * mult
            s = tail
    # 余下个位
    if s and s in digit:
        val += digit[s]
    return val or 1


def _extract_counter_value(chunk):
    """从 chunk 抽出第一个具体数字和单位。返回 (value, unit) 或 None。

    value 归一化到 [1, 999] 范围（counter 场景 GSAP 动画 0→value），
    对 "万"/"亿" 级别做单位换算（"八千万" → (8, "千万吨")、"两亿" → (2, "亿")）。
    unit 是字面上的单位后缀，会原样显示在数字右边。
    """
    # 1) ASCII 数字优先（最准）
    m = _NUMBER_RE.search(chunk)
    if m:
        try:
            v = int(float(m.group(1)))
            unit = (m.group(2) or "").strip()
            return _normalize_counter(v, _UNIT_FROM_RE.get(unit, unit or ""))
        except (ValueError, IndexError):
            pass
    # 2) 中文数字
    m = _CN_NUM_RE.search(chunk)
    if m and any(c in _CN_NUM_CHARS for c in m.group(0)):
        v = _parse_cn_num(m.group(1).rstrip("第").strip())
        if v > 0:
            unit_raw = (m.group(2) or "").strip()
            return _normalize_counter(v, _UNIT_FROM_RE.get(unit_raw, unit_raw or ""))
    return None


def _normalize_counter(value, unit):
    """把 value 压回 [1, 9999]（counter 场景动画 0→value），对"万"/"亿"换算。

    "八千万吨" (80000000) → (8000, "万吨")
    "两亿" (200000000) → (2, "亿")
    "三千年" (3000) → (3000, "年")
    "30 岁" → 原样
    "60 %" → 原样
    """
    if value <= 0:
        return 1, unit
    cap = 9999
    # "六成" = 60% (一成 = 1/10)，把 value*10 当成 % 值
    if "成" in unit and "亿" not in unit and "万" not in unit and "千" not in unit:
        return min(value * 10, cap), "%"
    if "亿" in unit and value >= 10**8:
        head = value // 10**8
        suffix = unit.replace("亿", "").strip()
        new_unit = (suffix + "亿") if suffix else "亿"
        return min(head, cap), new_unit
    if "万" in unit and value >= 10**4:
        head = value // 10**4
        suffix = unit.replace("万", "").strip()
        new_unit = (suffix + "万") if suffix else "万"
        return min(head, cap), new_unit
    if "千" in unit and value >= 1000:
        head = value // 1000
        suffix = unit.replace("千", "").strip()
        new_unit = (suffix + "千") if suffix else "千"
        return min(head, cap), new_unit
    return min(value, cap), unit


def decide_scene_type(chunk: str, scene_index: int) -> str:
    """返回 'counter' | 'kinetic' | 'stock'。"""
    if not chunk or not chunk.strip():
        return "stock"
    text = chunk.strip()
    # 含"有视觉冲击"的数字（>= 5 或有单位）→ counter 场景
    extracted = _extract_counter_value(text)
    if extracted:
        value, unit = extracted
        # 弱数字（"第一刀"/"一辈子" 之类）不当 counter — 阈值 >= 5 或有单位
        if value >= 5 or unit:
            return "counter"
    # 短句（< 18 字，无数字） → kinetic text
    if len(text) < 18:
        return "kinetic"
    return "stock"


def _enrich_with_kinetic(media_items, chunks, width, height, apply_overlay=False):
    """对每个 scene 跑 `decide_scene_type` 分类 + 注入可选的 kinetic overlay。

    apply_overlay=False (默认, 2026-06-18 用户反馈后):
      Pexels 图/视频太干净了,不要再叠大字/数字。函数仅做分类 + 日志,
      直接返回原 media_items。底部 sub-caption 仍然跟着 TTS 显示。

    apply_overlay=True (历史行为, 留着方便未来重开):
      把 ~30% 的"含数字/短句"场景叠加 kinetic overlay 而不是替换。
      返回的 media_items 形状:
        - ("image_overlay", (image_path, kinetic_html)) — Pexels 图 + kinetic overlay
        - ("video_overlay", (video_path, kinetic_html)) — Pexels 视频 + kinetic overlay
        - ("kinetic", html_str) — 无 stock 时纯 kinetic (gradient 背景)
        - ("image", path) / ("video", path) — 不触发 kinetic 时保留 stock
        - ("gradient", path) — pad/fallback gradient
    """
    n = len(media_items)
    out = []
    kinetic_count = 0
    for i, ((kind, path), chunk) in enumerate(zip(media_items, chunks)):
        scene_type = decide_scene_type(chunk, i)
        if not apply_overlay:
            # 仅分类 + 日志,不动 media_items
            if scene_type != "stock":
                kinetic_count += 1
            out.append((kind, path))
            continue
        if scene_type == "counter":
            extracted = _extract_counter_value(chunk) or (0, "")
            value, unit = extracted
            # 把数字部分去掉，剩下的就是 label
            label = re.sub(
                r"\d+(?:\.\d+)?\s*[%％倍万亿岁分钟秒天月年公斤克千百]?",
                "", chunk,
            )
            label = re.sub(
                r"[零一二三四五六七八九十两](?:百|十)?[零一二三四五六七八九十两]?"
                r"(?:\.\d+)?\s*[%％倍万亿岁分钟秒天月年公斤克千百第]?",
                "", label,
            )
            label = re.sub(r"[，。！？.!?]+$", "", label).strip()[:24] or "数据"
            html_str = build_animated_counter_scene_html(
                label, value, unit, i, width, height, overlay=(kind in ("image", "video"))
            )
            if kind == "image":
                out.append(("image_overlay", (path, html_str)))
                kinetic_count += 1
            elif kind == "video":
                out.append(("video_overlay", (path, html_str)))
                kinetic_count += 1
            else:
                # No stock media (gradient/pad fallback) — keep pure kinetic
                out.append(("kinetic", html_str))
                kinetic_count += 1
        elif scene_type == "kinetic":
            short = re.sub(r"\s+", " ", chunk).strip()
            if len(short) > 24:
                short = short[:24] + "…"
            html_str = build_kinetic_text_scene_html(
                short, i, width, height, overlay=(kind in ("image", "video"))
            )
            if kind == "image":
                out.append(("image_overlay", (path, html_str)))
                kinetic_count += 1
            elif kind == "video":
                out.append(("video_overlay", (path, html_str)))
                kinetic_count += 1
            else:
                out.append(("kinetic", html_str))
                kinetic_count += 1
        else:
            out.append((kind, path))
    log(
        f"  scene type mix: {kinetic_count}/{n} kinetic "
        f"({100*kinetic_count//max(n,1)}% — target ~30%, overlay={'on' if apply_overlay else 'off'})"
    )
    return out


def build_image_composition_html(
    media_items,
    chunks,
    total_duration=DEFAULT_DURATION_SEC,
    width=DEFAULT_WIDTH,
    height=DEFAULT_HEIGHT,
    scene_times=None,
    subtimes=None,
):
    """Build hyperframes composition HTML with image backgrounds + Ken Burns + subtitles.

    Each scene:
    - image_path: background image (1080x1920)
    - chunk: caption text
    - per-scene duration: total_duration / n_scenes (equal-time) OR
      scene_times[i] = (start, end) per scene when TTS per-word
      alignment is available — this is the RC3 fix that anchors each
      scene to the TTS-pacing of its text.
    - per-sub-caption timing: when subtimes[i] is a list of (rel_start,
      rel_end) tuples, sub-captions follow TTS sentence-level alignment.
      When subtimes[i] is empty/missing, sub-captions split the scene
      duration equally (the old behaviour, retained as a fallback).
    - Ken Burns: slow zoom in (scale 1.0 → 1.12) + slight pan
    - Subtitle: large text at bottom with dark gradient overlay
    """
    n = len(media_items)
    if scene_times and len(scene_times) == n and any(t is not None for t in scene_times):
        # Alignment-driven: each scene uses the TTS span of its text.
        # Pad scenes (None — from split_script_to_cards trailing "" chunks
        # when n_scenes > n_sentences) inherit the previous cursor so the
        # timeline stays monotonic. They render as zero-length clips with
        # no media / no subs, which is the correct visual for a blank
        # scene. Using `scene_times[-1][1]` as the final stop ignores
        # trailing None pads (their end is unknown), so we track our own
        # cursor.
        starts = []
        cursor = 0.0
        for t in scene_times:
            if t is None:
                starts.append(round(cursor, 3))
            else:
                s, e = t
                starts.append(round(s, 3))
                cursor = e
        starts.append(round(cursor, 3))  # final stop
    else:
        per = total_duration / n
        # hyperframes lint 抓相邻 clip 在 1e-14s 处的浮点尾数重叠。
        # 预生成 starts 列表（每段起、止各 round 一次）避免累计误差。
        starts = [round(i * per, 3) for i in range(n + 1)]
        starts[-1] = round(total_duration, 3)  # 最后一段吃尾差
    # Ken Burns: 抖音科普风动效更克制，只做轻微推进 (1.0 → 1.08)，无方向 pan，
    # ease 也改成 linear，参考视频基本就是静帧 + 字幕。
    kb_variants = [
        {"scale": 1.08, "x": 0, "y": 0},
        {"scale": 1.07, "x": 0, "y": 0},
        {"scale": 1.09, "x": 0, "y": 0},
        {"scale": 1.06, "x": 0, "y": 0},
        {"scale": 1.08, "x": 0, "y": 0},
        {"scale": 1.07, "x": 0, "y": 0},
    ]

    scenes_html = []
    stage_media_html = []
    timeline_tweens = []
    for i, ((media_kind, media_path), chunk) in enumerate(zip(media_items, chunks)):
        # Pad scene (empty chunk from split_script_to_cards trailing ""):
        # skip — emitting it would either stack 11 zero-length clips at the
        # same data-start (overlapping_clips_same_track lint error) or
        # extend the timeline past the last real scene. Both are wrong:
        # pad chunks carry no text / no media, so they should not appear
        # in the rendered composition at all.
        if not chunk:
            continue
        start = starts[i]
        per_this = starts[i + 1] - starts[i]
        kb = kb_variants[i % len(kb_variants)]
        # 抖音科普风字幕：单行短句，max_lines=1，每张图一个 sub-caption。
        caption_chars = 20 if width >= height else 14
        # RC3+ fix: sub-captions + per-slot timing follow alignment when
        # available. Each entry is (lines, slot_start, slot_end) in
        # absolute video time. We pull the *text* from the alignment
        # pre-pass too — otherwise wrap_to_subcaptions(chunk) here would
        # split the chunk at punctuation independently of how the
        # alignment pre-pass split it, producing a different n_subs and
        # silently dropping us back to equal-time fallback.
        _scene_subtimes = (
            subtimes[i] if (subtimes and i < len(subtimes) and subtimes[i]) else None
        )
        if _scene_subtimes:
            subcaptions = [st[0] for st in _scene_subtimes]
            sub_slots = [(st[1], st[2]) for st in _scene_subtimes]
        else:
            subcaptions = wrap_to_subcaptions(chunk, max_chars=caption_chars, max_lines=1)
            n_subs_fb = len(subcaptions)
            slot = per_this / max(n_subs_fb, 1)
            sub_slots = [(start + j * slot, start + (j + 1) * slot) for j in range(n_subs_fb)]
        n_subs = len(subcaptions)
        # No more first_sub_offset hack: alignment gives us the real
        # TTS start time, so the first sub-caption appears exactly when
        # TTS starts speaking. The opening hook is purely decorative
        # and overlays the first sub-caption's text.
        first_sub_offset = 0.0
        if media_kind == "video":
            media_filename = Path(media_path).name
            stage_media_html.append(
                f'<video class="bg bg-video" id="bg-{i+1}" '
                f'src="videos/{media_filename}" muted playsinline loop '
                f'data-track-index="0" data-start="{start}" '
                f'data-duration="{per_this}"></video>'
            )
            media_html = ""
        elif media_kind == "video_overlay":
            # Pexels 视频 + kinetic 数字/大字 overlay
            # path 是 (video_path, kinetic_html) tuple
            vid_path, overlay_html = media_path
            media_filename = Path(vid_path).name
            stage_media_html.append(
                f'<video class="bg bg-video" id="bg-{i+1}" '
                f'src="videos/{media_filename}" muted playsinline loop '
                f'data-track-index="0" data-start="{start}" '
                f'data-duration="{per_this}"></video>'
            )
            media_html = overlay_html
        elif media_kind == "image_overlay":
            # Pexels 图 + kinetic 数字/大字 overlay
            # path 是 (image_path, kinetic_html) tuple
            img_path, overlay_html = media_path
            media_filename = Path(img_path).name
            media_html = (
                f'<div class="bg" id="bg-{i+1}" '
                f'style="background-image:url(images/{media_filename});"></div>'
                + "\n      " + overlay_html
            )
        elif media_kind == "kinetic":
            # 注入预先构建的 kinetic HTML（已含渐变背景 + GSAP timeline）
            media_html = media_path
        else:
            media_filename = Path(media_path).name
            media_html = (
                f'<div class="bg" id="bg-{i+1}" '
                f'style="background-image:url(images/{media_filename});"></div>'
            )
        hook_html = ""
        if i == 0 and media_kind not in ("kinetic", "image_overlay", "video_overlay"):
            hook_text = wrap_caption_lines(chunk, max_chars=16, max_lines=2)
            hook_html = (
                '<div class="hook" id="opening-hook">'
                + "".join(f"<div>{escape_html(line)}</div>" for line in hook_text)
                + "</div>"
            )
        # Emit one <div class="subtitle"> per sub-caption slot
        sub_html_parts = []
        for j, lines in enumerate(subcaptions):
            sub_id = f"sub-{i+1}-{j+1}"
            caption_html = "".join(f'<div class="cap-line">{escape_html(line)}</div>' for line in lines)
            sub_html_parts.append(
                f'<div class="subtitle" id="{sub_id}">{caption_html}</div>'
            )
        # 场景间切换：hyperframes v0.6.89 lint 把 GSAP 动画的 .wipe 当作
        # track 1 上的独立 clip，与下一场景 clip 在同一秒边界重叠 → 报错。
        # 改用 clip 末尾的 Ken Burns + 字幕淡出做柔和切换。
        scenes_html.append(
            f'    <div id="scene-{i+1}" class="clip" data-track-index="1" '
            f'data-start="{start}" data-duration="{per_this}">\n'
            f'      {media_html}\n'
            f'      {hook_html}\n'
            f'      ' + "\n      ".join(sub_html_parts) + '\n'
            f'    </div>'
        )
        # Ken Burns: only for image/video scenes, NOT kinetic
        if media_kind in ("image", "video", "image_overlay", "video_overlay"):
            timeline_tweens.append(
                f"tl.to('#bg-{i+1}', {{ scale: {kb['scale']}, x: {kb['x']}, y: {kb['y']}, "
                f"ease: 'none', duration: {per_this} }}, {start});"
            )
        if i == 0 and media_kind not in ("kinetic", "image_overlay", "video_overlay"):
            timeline_tweens.append(
                "tl.fromTo('#opening-hook', { opacity: 0, scale: 0.92 }, "
                "{ opacity: 1, scale: 1, duration: 0.25, ease: 'power3.out' }, 0.1);"
            )
            # Hook is a flash of attention-grabbing text in the first
            # 1.5s; sub-caption 1 takes over from there. Was 4.5s, which
            # covered the first 1-2 sub-captions and caused visible
            # overlap with regular subtitles.
            timeline_tweens.append(
                "tl.to('#opening-hook', { opacity: 0, duration: 0.25 }, 1.5);"
            )
        # 场景间 wipe 已删除（lint 冲突），靠 Ken Burns + 字幕淡出做切换
        # Sub-caption slots: fade in at slot start, fade out 0.3s before slot end.
        # The very last sub-caption of the very last scene stays visible to the end.
        for j in range(n_subs):
            sub_sel = f"#sub-{i+1}-{j+1}"
            slot_start, slot_end = sub_slots[j]
            slot_dur = slot_end - slot_start
            # Fade-in always 0.2s. Fade-out duration adapts to slot length
            # so short slots ("邻居？" 0.34s) don't have their fade-out
            # spill past the slot's natural end — that was the root cause
            # of "sub-caption appears already faded" for short sentences.
            if slot_dur >= 0.6:
                fade_dur = 0.3
            elif slot_dur >= 0.3:
                fade_dur = 0.15
            else:
                fade_dur = max(0.0, slot_dur - 0.05)
            timeline_tweens.append(
                f"tl.fromTo('{sub_sel}', {{ opacity: 0 }}, "
                f"{{ opacity: 1, duration: 0.2 }}, {slot_start:.2f});"
            )
            is_last = (i == n - 1) and (j == n_subs - 1)
            if not is_last and fade_dur > 0:
                # Fade out *within* the slot, not at the very end, so the
                # next sub-caption can fade in without overlap.
                fade_out_at = max(slot_end - fade_dur, slot_start + 0.2)
                timeline_tweens.append(
                    f"tl.to('{sub_sel}', {{ opacity: 0, duration: {fade_dur:.2f} }}, {fade_out_at:.2f});"
                )

    stage_media_str = "\n".join(stage_media_html)
    scenes_str = "\n".join(scenes_html)
    tweens_str = "\n    ".join(timeline_tweens)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>video composition</title>
  <style>
    [data-composition-id="dynamic"] {{
      width: {width}px; height: {height}px; background: #0a0e1a; color: #fff;
      font-family: "Noto Sans CJK SC", "Noto Sans CJK", -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
      overflow: hidden;
    }}
    .clip {{
      width: 100%; height: 100%; position: relative; overflow: hidden; z-index: 2;
    }}
    .bg {{
      position: absolute; inset: 0;
      background-size: cover; background-position: center;
      width: 100%; height: 100%; object-fit: cover;
      transform: scale(1.0) translate(0, 0);
      will-change: transform;
    }}
    /* kinetic overlay: 叠在 Pexels 图/视频上的前景层。
       透明背景 + 底部黑色 scrim 提高文字可读性。
       纯 kinetic 场景不走这条路——它们用 inline gradient。 */
    .kinetic-overlay {{
      position: absolute; inset: 0;
      background: linear-gradient(180deg, rgba(0, 0, 0, 0) 30%, rgba(0, 0, 0, 0.55) 100%);
      display: flex; flex-direction: column; justify-content: center; align-items: center;
      z-index: 2;
      padding: 0 8%;
      text-align: center;
    }}
    /* Hook 钩子：抖音科普风，去掉黑底黄边，改为居中大白字 + 强黑描边。
       字号更大、视觉冲击更强，4.5 秒后切到普通字幕。 */
    .hook {{
      position: absolute; z-index: 3; left: 6%; right: 6%; top: 18%;
      padding: 0; color: #fff;
      font-size: {96 if width >= height else 108}px; line-height: 1.1;
      font-weight: 900; letter-spacing: 2px; text-align: center;
      border: 0;
      background: transparent;
      text-shadow:
        -4px -4px 0 #000, 4px -4px 0 #000,
        -4px 4px 0 #000, 4px 4px 0 #000,
        -4px 0 0 #000, 4px 0 0 #000,
        0 -4px 0 #000, 0 4px 0 #000,
        0 8px 24px rgba(0, 0, 0, 0.6);
    }}
    /* 抖音科普风字幕：白字 + 强黑描边，无背景框，单行短句居中贴底。
       强描边用 text-shadow 多向叠 8 层模拟 -webkit-text-stroke。 */
    .subtitle {{
      position: absolute; left: 50%; bottom: 7%;
      transform: translateX(-50%);
      max-width: 86%;
      padding: 0;
      background: transparent;
      border: 0;
      display: flex; flex-direction: row; align-items: center; justify-content: center;
      flex-wrap: nowrap;
      gap: 0;
      opacity: 0;
    }}
    .cap-line {{
      font-size: {70 if width >= height else 80}px; font-weight: 900; line-height: 1.15; text-align: center;
      letter-spacing: 1px; color: #fff; white-space: nowrap;
      text-shadow:
        -3px -3px 0 #000, 3px -3px 0 #000,
        -3px 3px 0 #000, 3px 3px 0 #000,
        -3px 0 0 #000, 3px 0 0 #000,
        0 -3px 0 #000, 0 3px 0 #000,
        0 6px 18px rgba(0, 0, 0, 0.7);
    }}
    /* 场景切换横向 wipe：白条 + 微透明，制造 push 感 */
    .wipe {{
      position: absolute; left: 0; right: 0; top: 50%;
      height: 100%; transform-origin: left center;
      background: linear-gradient(180deg, transparent 0%, rgba(255,255,255,0.95) 30%, rgba(255,255,255,0.95) 70%, transparent 100%);
      opacity: 0.85; pointer-events: none; z-index: 10;
      transform: scaleX(0);
    }}
  </style>
</head>
<body>
  <div data-composition-id="dynamic"
       data-width="{width}" data-height="{height}"
       data-start="0" data-duration="{total_duration}">
{stage_media_str}
{scenes_str}
  </div>

  <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
  <script>
    window.__timelines = window.__timelines || {{}};
    const tl = gsap.timeline({{ paused: true }});
    {tweens_str}
    window.__timelines["dynamic"] = tl;
  </script>
</body>
</html>
"""


def build_card_composition_html(chunks, total_duration=30):
    """Build hyperframes composition HTML for the chunks.

    Each chunk gets total_duration/n_cards seconds, with a fade transition.
    """
    n = len(chunks)
    per = total_duration / n
    starts = [round(i * per, 3) for i in range(n + 1)]
    starts[-1] = round(total_duration, 3)
    palette = [
        ("#1e3a8a", "#7c3aed"),  # 1: blue → purple
        ("#7c3aed", "#ec4899"),  # 2: purple → pink
        ("#ec4899", "#f59e0b"),  # 3: pink → amber
        ("#f59e0b", "#10b981"),  # 4: amber → emerald
        ("#10b981", "#1e3a8a"),  # 5: emerald → blue
        ("#0ea5e9", "#6366f1"),
        ("#dc2626", "#f59e0b"),
    ]

    cards_html = []
    timeline_tweens = []
    for i, chunk in enumerate(chunks):
        start = starts[i]
        per_this = starts[i + 1] - starts[i]
        c1, c2 = palette[i % len(palette)]
        bg = f"linear-gradient(135deg, {c1} 0%, {c2} 100%)"
        # Wrap text by character (~13 per line for the 1080 width with padding)
        lines = wrap_text_to_lines(chunk, max_chars=13, max_lines=4)
        text_html = "".join(f'<div class="line">{escape_html(line)}</div>' for line in lines)
        cards_html.append(
            f'    <div id="card-{i+1}" class="clip" data-track-index="0" '
            f'data-start="{start}" data-duration="{per_this}" style="background:{bg};">\n'
            f'      {text_html}\n'
            f'    </div>'
        )
        # Fade in
        timeline_tweens.append(f"tl.to('#card-{i+1}', {{ opacity: 1, duration: 0.3 }}, {start});")
        # Fade out (except last card, which we just leave)
        if i < n - 1:
            timeline_tweens.append(f"tl.to('#card-{i+1}', {{ opacity: 0, duration: 0.3 }}, {start + per - 0.3});")

    cards_str = "\n".join(cards_html)
    tweens_str = "\n    ".join(timeline_tweens)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>video composition</title>
  <style>
    [data-composition-id="dynamic"] {{
      width: 1080px; height: 1920px; background: #0a0e1a; color: #fff;
      font-family: "Noto Sans CJK SC", "Noto Sans CJK", -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
      overflow: hidden;
    }}
    .clip {{
      width: 100%; height: 100%;
      display: flex; flex-direction: column; justify-content: center; align-items: center;
      gap: 24px; padding: 100px 80px; box-sizing: border-box;
      opacity: 0;
    }}
    .line {{
      font-size: 80px; font-weight: bold; line-height: 1.3; text-align: center;
      letter-spacing: 4px;
    }}
  </style>
</head>
<body>
  <div data-composition-id="dynamic"
       data-width="1080" data-height="1920"
       data-start="0" data-duration="{total_duration}">
{cards_str}
  </div>

  <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
  <script>
    window.__timelines = window.__timelines || {{}};
    const tl = gsap.timeline({{ paused: true }});
    {tweens_str}
    window.__timelines["dynamic"] = tl;
  </script>
</body>
</html>
"""


def wrap_text_to_lines(text, max_chars=13, max_lines=4):
    """Break Chinese text into lines of ~max_chars, max max_lines."""
    if not text:
        return [""]
    chars = list(text)
    lines = []
    for i in range(0, len(chars), max_chars):
        lines.append("".join(chars[i:i + max_chars]))
        if len(lines) >= max_lines:
            break
    # If there's leftover, append "..." to last line
    if len(chars) > max_lines * max_chars and lines:
        last = lines[-1]
        if len(last) >= max_chars - 1:
            lines[-1] = last[:-1] + "…"
        else:
            lines[-1] = last + "…"
    return lines


def _tokenize_for_wrap(text):
    """Tokenize mixed CN/EN text for word-aware line wrapping.

    An alphanumeric run (letters and/or digits, with optional code punctuation
    like ._-%/) stays as one token — so "95%", "14", "ffmpeg", "m3u8",
    "h264", "video-studio" all keep their digits and punctuation intact.
    Each CJK char and each non-alphanumeric punct char is its own token.
    A single ASCII space is also a token, so "video-studio 拆分" preserves
    the visible gap between the English term and the CJK run.

    Decimal-number glue (first branch): keep "0.5" / "0．5" / "1.5" /
    "12.5%" as one token so the packer never breaks between the digit,
    the period, and the next digit. Tries this BEFORE the generic alnum
    branch so the period is part of the match.
    """
    import re as _re
    return _re.findall(
        r'\d+[.,．]\d+[%]?|[A-Za-z0-9][A-Za-z0-9_.\-/%]*|[一-鿿]|[^\s\w]| ',
        text,
    )


def _pack_lines(text, max_chars, max_lines):
    """Word-aware line packer with two-line midpoint split.

    v2 algorithm (replaces old greedy fill):
    1. If total visible chars <= max_chars → return single line.
    2. If max_lines >= 2 and any token-boundary split yields two halves each
       <= max_chars → pick the split whose left half is closest to total/2
       (so the two lines are balanced).
    3. Fallback: greedy fill + ellipsis truncate, used only for single-token
       oversize (one word longer than max_chars) or max_lines=1.

    Why this is better than greedy: greedy fill leaves a 3-4 char tail on
    line 2 (e.g. 14+4). Midpoint split produces 9+9 / 10+8 — both lines
    readable, break sits at a token boundary (spaces fall in the "gap"
    because they don't count toward max_chars, so a space token often
    coincides with the chosen split index after a CJK run).
    """
    if not text:
        return [""]
    text = text.strip()
    if not text:
        return [""]

    tokens = _tokenize_for_wrap(text)
    if not tokens:
        return [""]

    # 1. Total visible char count (includes space tokens — they show in render)
    total = sum(len(t) for t in tokens)

    # 2. Single line: whole thing fits
    if total <= max_chars:
        return [text]

    # 3. Two-line midpoint split
    if max_lines >= 2:
        # Build cumulative length array.
        cum = []
        running = 0
        for t in tokens:
            running += len(t)
            cum.append(running)

        half = total / 2
        best_i = None
        best_score = None
        # Need: cut between token i and i+1 (so 0 < i < len(tokens)-1),
        # left half cum[i] <= max_chars and right half (total - cum[i]) <= max_chars.
        for i in range(1, len(tokens) - 1):
            left = cum[i]
            right = total - left
            if left <= max_chars and right <= max_chars:
                score = abs(left - half)
                if best_score is None or score < best_score:
                    best_score = score
                    best_i = i

        if best_i is not None:
            left_line = "".join(tokens[: best_i + 1]).strip()
            right_line = "".join(tokens[best_i + 1 :]).strip()
            if left_line and right_line:
                return [left_line, right_line]

    # 4. Fallback: greedy fill + ellipsis (handles >max_chars single token
    # or pathological cases where no balanced split exists).
    lines = []
    current = []
    current_len = 0  # spaces don't count toward max_chars budget

    def _truncate_last():
        if not lines:
            return
        last = lines[-1]
        if len(last) >= max_chars - 1:
            lines[-1] = last[: max_chars - 1] + "…"
        else:
            lines[-1] = last + "…"

    for tok in tokens:
        tok_len = len(tok)
        is_space = tok == " "
        if tok_len > max_chars and not is_space:
            if current:
                lines.append("".join(current))
                current = []
                current_len = 0
            lines.append(tok[: max_chars - 1] + "…")
            if len(lines) >= max_lines:
                return lines
            continue
        if current and current_len + (0 if is_space else tok_len) > max_chars:
            lines.append("".join(current))
            current = []
            current_len = 0
            if len(lines) >= max_lines:
                _truncate_last()
                return lines
        current.append(tok)
        if not is_space:
            current_len += tok_len

    if current:
        if len(lines) >= max_lines:
            tail = "".join(current)
            if len(tail) <= max_chars and len(lines[-1]) + len(tail) <= max_chars + 2:
                lines[-1] = lines[-1] + tail
            else:
                _truncate_last()
        else:
            lines.append("".join(current))

    # 0.4-merge: if the second line is a tail, try to merge back into one
    # line. Threshold relaxed to max_chars+4 so 14+4=18 (new max) can fold.
    if len(lines) >= 2 and len(lines[-1]) < max_chars * 0.4:
        merged = lines[-2] + lines[-1]
        if len(merged) <= max_chars + 4:
            lines = lines[:-2] + [merged]

    lines = [ln.lstrip() for ln in lines]
    lines = [ln for ln in lines if ln]
    return lines or [""]


def wrap_caption_lines(text, max_chars=20, max_lines=2):
    """Wrap caption text for 抖音-style subtitles. Word-aware; max 2 lines."""
    if not text:
        return [""]
    return _pack_lines(text, max_chars, max_lines)


def wrap_to_subcaptions(text, max_chars=18, max_lines=2):
    """Pack a long chunk into a list of sub-captions (each <= max_lines lines).

    A "sub-caption" is one timed slot in the scene. The render daemon
    turns each sub-caption into its own <div class="subtitle"> with
    a fade-in/fade-out window, so a 60-char chunk reads as 2-3
    sub-captions over the scene's 10s instead of one unreadable blob.

    Returns a list of sub-captions; each sub-caption is a list of lines.
    """
    if not text:
        return [[""]]
    text = text.strip()
    if not text:
        return [[""]]

    import re as _re
    parts = [p.strip() for p in _re.split(r'(?<=[。！？,，;；])', text) if p.strip()]
    if not parts:
        return [_pack_lines(text, max_chars, max_lines)]

    cap_chars = max_chars * max_lines
    subcaptions = []
    current_parts = []
    current_len = 0

    for part in parts:
        part_len = len(part)
        if part_len > cap_chars:
            if current_parts:
                subcaptions.append(_pack_lines("".join(current_parts), max_chars, max_lines))
                current_parts = []
                current_len = 0
            subcaptions.append(_pack_lines(part, max_chars, max_lines))
            continue
        if current_parts and current_len + part_len > cap_chars:
            subcaptions.append(_pack_lines("".join(current_parts), max_chars, max_lines))
            current_parts = [part]
            current_len = part_len
        else:
            current_parts.append(part)
            current_len += part_len

    if current_parts:
        subcaptions.append(_pack_lines("".join(current_parts), max_chars, max_lines))

    return [s for s in subcaptions if any(line.strip() for line in s)]


def get_duration_sec(mp4_path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(mp4_path)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return float(result.stdout.strip())


def process_one(job):
    job_id = job["id"]
    theme = job.get("theme", "")
    log(f"rendering {job_id}: theme={theme!r}")

    job["status"] = "rendering"
    job["error"] = None
    job.setdefault("render", {})
    job["render"]["render_started_at"] = now_iso()
    save_job(job)

    run_dir = VIDEO_RUNS_DIR / job_id
    render_dir = run_dir / "composition"
    video_dir = run_dir / "video"
    video_dir.mkdir(parents=True, exist_ok=True)

    try:
        render_cfg = job.get("render") or {}
        width = int(render_cfg.get("width") or DEFAULT_WIDTH)
        height = int(render_cfg.get("height") or DEFAULT_HEIGHT)
        total_duration = float(render_cfg.get("duration_sec") or DEFAULT_DURATION_SEC)
        fps = int(render_cfg.get("fps") or DEFAULT_FPS)
        out_mp4 = render_placeholder(
            job_id,
            render_dir,
            job.get("script", ""),
            theme=theme,
            width=width,
            height=height,
            total_duration=total_duration,
            fps=fps,
        )
        final_raw = video_dir / "raw.mp4"
        shutil.move(str(out_mp4), str(final_raw))

        duration = get_duration_sec(final_raw)

        job["render"]["mp4_path"] = str(final_raw)
        job["render"]["render_completed_at"] = now_iso()
        job["status"] = "rendered"
        job.setdefault("logs", []).append(
            f"{now_iso()} render done ({duration:.1f}s, {final_raw.stat().st_size} bytes)"
        )
        save_job(job)
        log(f"{job_id} -> rendered, duration={duration:.1f}s")

        NARRATE_TRIGGER.touch()
        log(f"touched {NARRATE_TRIGGER.name}")
        return True

    except Exception as e:
        log(f"{job_id} RENDER FAILED: {e}")
        job["status"] = "error"
        job["error"] = f"render daemon: {type(e).__name__}: {e}"
        job.setdefault("logs", []).append(f"{now_iso()} RENDER FAILED: {e}")
        save_job(job)
        return False


def main():
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_RUNS_DIR.mkdir(parents=True, exist_ok=True)

    with LOCK_PATH.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another writer is running, skipping")
            return 0

        # Debounce
        if RENDER_TRIGGER.exists():
            deadline = time.time() + 12
            while time.time() < deadline:
                mtime = RENDER_TRIGGER.stat().st_mtime
                age = time.time() - mtime
                if age >= 3:
                    break
                time.sleep(min(3, max(0.2, 3 - age)))

        # Throttle (render is expensive; 60s gap between runs)
        if LAST_RUN_MARKER.exists():
            try:
                last = float(LAST_RUN_MARKER.read_text(encoding="utf-8").strip() or "0")
            except ValueError:
                last = 0
            gap = time.time() - last
            if gap < 60 and last:
                wait = 60 - gap
                log(f"throttling: previous run {gap:.1f}s ago, sleeping {wait:.1f}s")
                time.sleep(wait)

        processed = 0
        for _ in range(1):  # max 1 per run
            jobs = pending_jobs()
            if not jobs:
                break
            process_one(jobs[0])
            processed += 1

        LAST_RUN_MARKER.write_text(f"{time.time()}\n", encoding="utf-8")
        log(f"processed={processed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
