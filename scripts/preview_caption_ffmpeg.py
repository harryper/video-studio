#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate a fast black-bg + caption + voice preview mp4 via ffmpeg ASS.

Why: `preview_caption_video.py` uses hyperframes (headless Chromium) to
render HTML+GSAP into video — that takes 30-90s for a 60s clip. During
the sub-caption + voice sync debugging loop we don't need scene
animations or Pexels backgrounds; we just want to hear the voice and
see the sub timing. This script burns an ASS subtitle track onto a
black-background ffmpeg lavfi source and muxes the voice in ~3-6s for
60s clips.

ffmpeg 7.x on this host does NOT have the `drawtext` filter compiled,
but `subtitles=` (libass) IS available, so we use the ASS pipeline.

Reuses (no edits):
  - process_video_render_jobs._load_alignment_subtimes
  - process_video_render_jobs.split_script_to_cards / DEFAULT_WIDTH /
    DEFAULT_HEIGHT

ASS style choices mirror the previous CSS:
  - White text on black outline (BorderStyle 1, Outline 4)
  - Bottom-center alignment (Alignment 2, MarginV 80 ≈ 7% of 1080)
  - Bold 70pt Noto Sans CJK SC

Output: runs/<job_id>/preview-<duration>s.mp4 (same path as the
hyperframes version — new render overwrites the old).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from process_video_render_jobs import (
    DEFAULT_WIDTH,
    DEFAULT_HEIGHT,
    _load_alignment_subtimes,
    split_script_to_cards,
)


# ---------- ASS helpers ----------

def _ass_time(seconds: float) -> str:
    """Convert seconds (float) to ASS H:MM:SS.cc (centiseconds) format."""
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - h * 3600 - m * 60
    cs = int(round((s - int(s)) * 100))
    s_int = int(s)
    if cs >= 100:
        cs = 99
    return f"{h}:{m:02d}:{s_int:02d}.{cs:02d}"


def write_ass_file(
    clipped: list[list[tuple[list[str], float, float]]],
    width: int,
    height: int,
    font: str,
    fontsize: int,
    margin_v: int,
    outline: int,
) -> str:
    """Emit a temporary .ass file and return its path."""
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{fontsize},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{outline},0,2,40,40,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for scene_subs in clipped:
        for s_lines, a, b in scene_subs:
            text = "\\N".join(s_lines)  # ASS line break
            text = text.replace("\n", "\\N")
            lines.append(
                f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},Default,,0,0,0,,{text}\n"
            )
    ass_bytes = "".join(lines).encode("utf-8-sig")  # UTF-8 with BOM
    tmp = tempfile.NamedTemporaryFile(
        mode="wb", suffix=".ass", delete=False, prefix="preview_subs_",
    )
    tmp.write(ass_bytes)
    tmp.close()
    return tmp.name


# ---------- ffmpeg helpers ----------

def build_ffmpeg_cmd(
    ass_path: str,
    voice_path: Path,
    out_path: Path,
    duration: float,
    width: int,
    height: int,
    fps: int,
    crf: int,
) -> list[str]:
    """Construct the ffmpeg invocation. Uses lavfi color source for black bg
    and the `subtitles=` filter to burn ASS onto each frame, then muxes
    the original voice (copy, no re-encode) into the mp4 container."""
    # Escape path for ffmpeg filter: backslash colons, single-quote wrap.
    # ffmpeg subtitles filter accepts a filename; absolute path is safest.
    ass_escaped = ass_path.replace("\\", "/").replace(":", "\\:")
    lavfi = f"color=c=black:s={width}x{height}:r={fps}:d={duration}"
    return [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", lavfi,
        "-i", str(voice_path),
        "-vf", f"subtitles='{ass_escaped}':si=0",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        "-crf", str(crf),
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        str(out_path),
    ]


# ---------- clip helpers ----------

def clip_subs(
    subtimes: list[list[tuple[list[str], float, float]]],
    duration: float,
) -> list[list[tuple[list[str], float, float]]]:
    """Mirror preview_caption_video.py clipping logic: drop subs outside
    [0, duration], clamp end, drop subs shorter than 100ms."""
    clipped = []
    for scene_subs in subtimes:
        new_scene = []
        for s_lines, a, b in scene_subs:
            if a >= duration:
                continue
            b = min(b, duration)
            if b - a < 0.1:
                continue
            new_scene.append((s_lines, a, b))
        clipped.append(new_scene)
    return clipped


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--job-id", default="v_cc4e766b")
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    ap.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--crf", type=int, default=23)
    ap.add_argument("--font", default="Noto Sans CJK SC")
    ap.add_argument("--fontsize", type=int, default=70)
    ap.add_argument("--margin-v", type=int, default=80,
                    help="Bottom margin in px (~7% of 1080)")
    ap.add_argument("--outline", type=int, default=4)
    args = ap.parse_args()

    run_dir = SKILL_DIR / "runs" / args.job_id
    if not run_dir.exists():
        print(f"ERROR: run dir not found: {run_dir}", file=sys.stderr)
        return 1
    aln_path = run_dir / "alignment.json"
    script_path = run_dir / "script.txt"
    voice_path = run_dir / "audio" / "voice.mp3"
    for p in (aln_path, script_path, voice_path):
        if not p.exists():
            print(f"ERROR: missing input: {p}", file=sys.stderr)
            return 1

    script = script_path.read_text(encoding="utf-8").strip()
    aln = json.loads(aln_path.read_text(encoding="utf-8"))
    voice_seconds = aln.get("voice_seconds", 0.0)
    print(f"[ffmpeg-preview] job={args.job_id} script_chars={len(script)} voice_seconds={voice_seconds:.2f}")

    chunks = split_script_to_cards(script, n_cards=1)
    if not chunks or not chunks[0]:
        print("ERROR: split_script_to_cards returned empty", file=sys.stderr)
        return 1
    scene_times = [(0.0, args.duration)]

    subtimes = _load_alignment_subtimes(
        args.job_id, scene_times, chunks,
        width=args.width, height=args.height,
    )
    if not subtimes:
        print("ERROR: _load_alignment_subtimes returned None", file=sys.stderr)
        return 1

    clipped = clip_subs(subtimes, args.duration)
    n_subs = sum(len(s) for s in clipped)
    print(f"[ffmpeg-preview] {n_subs} sub-captions in [0, {args.duration}s]:")
    for scene_subs in clipped:
        for s_lines, a, b in scene_subs:
            print(f"  {a:6.3f}-{b:6.3f}s ({b-a:.3f}s)  {''.join(s_lines)!r}")

    # Trim voice to requested duration (stream copy, fast)
    voice_trim = run_dir / "audio" / f"voice-{int(args.duration)}s.mp3"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(voice_path),
            "-t", f"{args.duration:.3f}",
            "-c", "copy",
            str(voice_trim),
        ],
        check=True, capture_output=True,
    )

    # Emit ASS in a temp file (kept until ffmpeg returns so it can read)
    ass_path = write_ass_file(
        clipped, args.width, args.height,
        args.font, args.fontsize, args.margin_v, args.outline,
    )

    out_mp4 = run_dir / f"preview-{int(args.duration)}s.mp4"
    cmd = build_ffmpeg_cmd(
        ass_path, voice_trim, out_mp4, args.duration,
        args.width, args.height, args.fps, args.crf,
    )
    print(f"[ffmpeg-preview] running ffmpeg …")
    result = subprocess.run(cmd, capture_output=True, text=True)
    # Cleanup the temp ASS file regardless of ffmpeg outcome
    try:
        Path(ass_path).unlink()
    except OSError:
        pass
    if result.returncode != 0:
        print(f"ERROR: ffmpeg failed (exit={result.returncode})")
        print(result.stderr[-2000:] if result.stderr else "")
        return 2
    if not out_mp4.exists() or out_mp4.stat().st_size < 1000:
        print(f"ERROR: ffmpeg exit 0 but {out_mp4} missing or too small",
              file=sys.stderr)
        return 3
    size_mb = out_mp4.stat().st_size // (1024 * 1024)
    print(f"OK: {out_mp4} ({size_mb}MB, {args.duration}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
