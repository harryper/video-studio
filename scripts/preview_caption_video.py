#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate a 30s black-bg + caption + voice preview mp4 to eyeball sync.

Why: `_load_alignment_subtimes` has been iterated 4 times and we still
can't tell if subs track the voice. Generate a 30s preview (full script
as 1 scene) so we can watch and pinpoint desync.

Reuses (no edits):
  - process_video_render_jobs._load_alignment_subtimes (the function we're
    investigating)
  - process_video_render_jobs.split_script_to_cards / wrap_to_subcaptions /
    escape_html

Output: runs/<job_id>/preview-<duration>s.mp4
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from process_video_render_jobs import (
    DEFAULT_WIDTH, DEFAULT_HEIGHT,
    _load_alignment_subtimes,
    escape_html,
    split_script_to_cards,
)


def build_preview_html(
    clipped: list[list[tuple[list[str], float, float]]],
    script: str,
    duration: float,
    width: int,
    height: int,
) -> str:
    """Emit minimal HTML: black bg, sub-caption divs, GSAP timeline.

    No .bg image/video divs (we want black only). CSS rules copied from
    the production composition HTML (`.subtitle`, `.cap-line`, `.hook`).
    """
    # Build sub-caption div list — single scene, indexed 0..n-1
    sub_divs = []
    timeline_steps = []
    counter = 0
    for scene_subs in clipped:
        for s_lines, a, b in scene_subs:
            if not s_lines:
                continue
            text_html = "".join(
                f'<div class="cap-line">{escape_html(line)}</div>'
                for line in s_lines
            )
            sub_divs.append(
                f'<div id="sub-{counter}" class="subtitle" data-start="{a:.3f}" data-end="{b:.3f}">{text_html}</div>'
            )
            timeline_steps.append((counter, a, b))
            counter += 1

    css = """
    [data-composition-id="dynamic"] {
      width: %dpx; height: %dpx; background: #000; color: #fff;
      font-family: sans-serif;
      overflow: hidden;
    }
    .subtitle {
      position: absolute; left: 50%%; bottom: 7%%;
      transform: translateX(-50%%);
      max-width: 86%%;
      padding: 0;
      background: transparent;
      border: 0;
      display: flex; flex-direction: row; align-items: center; justify-content: center;
      flex-wrap: nowrap;
      gap: 0;
      opacity: 0;
    }
    .cap-line {
      font-size: 70px; font-weight: 900; line-height: 1.15; text-align: center;
      letter-spacing: 1px; color: #fff; white-space: nowrap;
      text-shadow:
        -3px -3px 0 #000, 3px -3px 0 #000,
        -3px 3px 0 #000, 3px 3px 0 #000,
        -3px 0 0 #000, 3px 0 0 #000,
        0 -3px 0 #000, 0 3px 0 #000,
        0 6px 18px rgba(0, 0, 0, 0.7);
    }
    """ % (width, height)

    # GSAP timeline that fades each sub in/out at its slot_start/slot_end
    timeline_js_parts = []
    for counter_i, a, b in timeline_steps:
        timeline_js_parts.append(
            f"  tl.to('#sub-{counter_i}', {{opacity:1, duration:0.05}}, {a:.3f});"
        )
        timeline_js_parts.append(
            f"  tl.to('#sub-{counter_i}', {{opacity:0, duration:0.1}}, {max(b - 0.1, a + 0.01):.3f});"
        )
    timeline_js = "\n".join(timeline_js_parts) if timeline_js_parts else "  // no subs"

    # Top-corner debug overlay shows current scene time so we can match
    # visual to voice.
    debug_overlay = """
    <div id="time-readout" style="position:absolute;top:2%;left:2%;color:#0f0;font-family:monospace;font-size:32px;background:rgba(0,0,0,0.7);padding:8px 14px;z-index:99;">t=0.00s</div>
    <script>
      (function(){
        const el = document.getElementById('time-readout');
        function tick() {
          el.textContent = 't=' + (performance.now()/1000).toFixed(2) + 's';
          requestAnimationFrame(tick);
        }
        requestAnimationFrame(tick);
      })();
    </script>
    """

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <title>caption sync preview ({duration}s)</title>
  <style>{css}</style>
  <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
</head>
<body>
  <div data-composition-id="dynamic"
       data-width="{width}" data-height="{height}"
       data-start="0" data-duration="{duration:.3f}">
{chr(10).join(sub_divs)}
{debug_overlay}
  </div>
  <script>
    window.__timelines = window.__timelines || {{}};
    const tl = gsap.timeline({{ paused: true }});
{timeline_js}
    window.__timelines["dynamic"] = tl;
  </script>
</body>
</html>
"""
    return html


def truncate_voice(voice_mp3: Path, duration: float, out_mp3: Path) -> None:
    """ffmpeg -t <duration> -c copy voice trimming (fast, no re-encode)."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(voice_mp3),
            "-t", f"{duration:.3f}", "-c", "copy", str(out_mp3),
        ],
        check=True, capture_output=True,
    )


def mux_audio_video(video_mp4: Path, voice_mp3: Path, out_mp4: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(video_mp4), "-i", str(voice_mp3),
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            "-shortest", str(out_mp4),
        ],
        check=True, capture_output=True,
    )


def render_with_hyperframes(html_path: Path, out_mp4: Path, work_dir: Path) -> None:
    cmd = [
        "npx", "--yes", "hyperframes@0.6.89", "render",
        "--fps", "15", "--workers", "1", "--low-memory-mode",
        "--output", str(out_mp4),
    ]
    result = subprocess.run(
        cmd, cwd=str(work_dir), capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "")[-1500:]
        raise RuntimeError(f"hyperframes render failed: {msg}")
    if not out_mp4.exists() or out_mp4.stat().st_size < 1000:
        raise RuntimeError(
            f"hyperframes exit 0 but {out_mp4} missing or empty "
            f"(exists={out_mp4.exists()}, size={out_mp4.stat().st_size if out_mp4.exists() else 'N/A'})"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--job-id", default="v_cc4e766b")
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    ap.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
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
    print(f"[preview] job={args.job_id} script_chars={len(script)} voice_seconds={voice_seconds:.2f}")

    # Single-scene split for clean comparison
    chunks = split_script_to_cards(script, n_cards=1)
    if not chunks or not chunks[0]:
        print("ERROR: split_script_to_cards returned empty", file=sys.stderr)
        return 1
    scene_times = [(0.0, args.duration)]

    subtimes = _load_alignment_subtimes(
        args.job_id, scene_times, chunks, width=args.width, height=args.height,
    )
    if not subtimes:
        print("ERROR: _load_alignment_subtimes returned None", file=sys.stderr)
        return 1

    # Clip subs to [0, duration], drop anything past duration
    clipped = []
    for scene_subs in subtimes:
        new_scene = []
        for s_lines, a, b in scene_subs:
            if a >= args.duration:
                continue
            b = min(b, args.duration)
            if b - a < 0.1:
                continue
            new_scene.append((s_lines, a, b))
        clipped.append(new_scene)
    n_subs = sum(len(s) for s in clipped)
    print(f"[preview] {n_subs} sub-captions in [0, {args.duration}s]:")
    for scene_subs in clipped:
        for s_lines, a, b in scene_subs:
            print(f"  {a:6.3f}-{b:6.3f}s ({b-a:.3f}s)  {''.join(s_lines)!r}")

    # Build HTML and write
    out_dir = run_dir / "composition_preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    html = build_preview_html(clipped, script, args.duration, args.width, args.height)
    html_path = out_dir / "index.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"[preview] wrote {html_path}")

    # Render video via hyperframes
    video_only = out_dir / "video-only.mp4"
    print(f"[preview] rendering video-only.mp4 (this may take 1-3 min)...")
    render_with_hyperframes(html_path, video_only, out_dir)
    print(f"[preview] video-only.mp4 = {video_only.stat().st_size//1024//1024}MB")

    # Truncate voice and mux
    voice_trim = out_dir / f"voice-{int(args.duration)}s.mp3"
    truncate_voice(voice_path, args.duration, voice_trim)
    final_mp4 = run_dir / f"preview-{int(args.duration)}s.mp4"
    mux_audio_video(video_only, voice_trim, final_mp4)
    size_mb = final_mp4.stat().st_size // (1024 * 1024)
    print(f"OK: {final_mp4} ({size_mb}MB, {args.duration}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())