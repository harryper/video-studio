#!/usr/bin/env python3
"""Host-side narrate writer for video-studio jobs (mode='video').

Last stage of the video pipeline:
- Reads jobs/video/ for 'rendered' jobs
- Synthesizes TTS voice from job.script
- Loops BGM to match voice duration, mixes at job.audio.bgm_volume
- ffmpeg merges with the rendered mp4
- Uploads final mp4 to R2
- On success: status -> 'final'

Mirrors process_pending_voice_jobs.py structure.
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


# TTS scripts (minimax_tts.py, voice_registry.json) live alongside this
# file in scripts/. Override VOICE_STUDIO_DIR env var to use a different
# location (e.g. a shared scripts dir) — kept for backwards compat.
VOICE_STUDIO_DIR = Path(os.environ.get(
    "VOICE_STUDIO_DIR",
    str(Path(__file__).resolve().parent),
))

SKILL_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = SKILL_DIR.parents[1]
JOBS_DIR = SKILL_DIR / "jobs" / "video"
VIDEO_RUNS_DIR = Path("/root/.openclaw/workspace/skills/video-studio/runs")
BGM_PATH = SKILL_DIR / "assets" / "bgm_default.mp3"
TTS_SCRIPT = VOICE_STUDIO_DIR / "scripts" / "minimax_tts.py"
UPLOAD_SCRIPT = SKILL_DIR / "scripts" / "upload_to_oss.py"
VOICE_REGISTRY = VOICE_STUDIO_DIR / "scripts" / "voice_registry.json"

LOCK_PATH = SKILL_DIR / ".video-narrate-writer.lock"
NARRATE_TRIGGER = SKILL_DIR / ".video-narrate-trigger"
LAST_RUN_MARKER = SKILL_DIR / ".video-narrate-writer.lastrun"
LOG_FILE = Path("/var/log/video-studio/video-narrate-watcher.log")


def log(msg):
    line = f"[video-narrate-writer] {msg}"
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
        if job.get("mode") == "video" and job.get("status") == "rendered":
            jobs.append(job)
    return sorted(jobs, key=lambda j: j.get("updated_at", ""))


def safe_slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower())[:30].strip("-")


def tts_synthesize(text, voice, speed, out_mp3):
    """Call minimax_tts.py with the resolved voice config."""
    text_file = out_mp3.parent / "script.txt"
    text_file.write_text(text, encoding="utf-8")
    cmd = [
        "python3", str(TTS_SCRIPT),
        "--text", str(text_file),
        "--out", str(out_mp3),
        "--voice", voice,
        "--speed", str(speed),
        "--retries", "1",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"TTS failed: {(result.stderr or result.stdout)[-500:]}")
    if not out_mp3.exists():
        raise RuntimeError("TTS exit 0 but mp3 missing")


SUBS_SCRIPT = SKILL_DIR / "scripts" / "minimax_tts_subs.py"


def _fetch_tts_subs(text, voice, speed, out_json):
    """Call minimax_tts_subs.py to fetch per-word timestamps.

    Independent of the audio synthesis call: same endpoint, different
    request, only downloads the subtitle_file JSON. Failure here means
    the caller will fall back to equal-time scene splits.
    """
    text_file = out_json.parent / "script.txt"
    text_file.write_text(text, encoding="utf-8")
    cmd = [
        "python3", str(SUBS_SCRIPT),
        "--text", str(text_file),
        "--out", str(out_json),
        "--voice", voice,
        "--speed", str(speed),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(
            f"subs fetch failed (exit={result.returncode}): "
            f"{(result.stderr or result.stdout)[-500:]}"
        )
    if not out_json.exists():
        raise RuntimeError("subs fetch exit 0 but json missing")


def _build_alignment_from_tts_subs(job_id):
    """Translate voice.subtitle.json → alignment.json (the schema render daemon expects).

    Source: MiniMax subtitle JSON = list of segments, each with
        timestamped_words = [{word, word_begin, word_end, time_begin(ms), time_end(ms)}, ...]
    Plus segment.text (full sentence including punctuation).

    Target: alignment.json = {
      voice_seconds, script_chars,
      chars: [{c, start, end, word}, ...],
      sentences: [{text, start, end, word_indices}, ...]
    }

    Sentence boundaries follow the ORIGINAL script's 。！？ punctuation
    positions, not the TTS provider's own splits (the provider splits at
    <= 50 chars, which doesn't match our script's natural sentence breaks).
    TTS strips 。！？ from timestamped_words but keeps them in segment.text;
    we walk the original script and interpolate punctuation timestamps
    from the previous char's end to the next char's start.

    Returns True on success, False if inputs are missing.
    """
    import json as _json
    run_dir = VIDEO_RUNS_DIR / job_id
    sub_json = run_dir / "audio" / "voice.subtitle.json"
    out_json = run_dir / "alignment.json"
    script_path = run_dir / "script.txt"
    if not sub_json.exists() or not script_path.exists():
        return False
    script = script_path.read_text(encoding="utf-8").strip()
    sub = _json.loads(sub_json.read_text(encoding="utf-8"))

    # Flatten all timestamped_words across segments
    tts_words = []
    for seg in sub:
        for w in seg.get("timestamped_words", []):
            tts_words.append((w["word"], w["time_begin"], w["time_end"]))

    # Build per-character timestamps by projecting script chars onto TTS
    # word intervals. This is robust against TTS word-level grouping
    # (e.g. "2024" returned as one word) and punctuation preservation.
    #
    # For each TTS word we record [tb_ms, te_ms]. We then walk script
    # chars (skipping whitespace/newline) and for each char find the TTS
    # word whose interval contains (or immediately precedes) the
    # character's expected position. We assign:
    #   - start = max(char_pos / script_len, tts_word.tb) — clamped
    #   - end   = the same for the next char's start
    #
    # Simpler version: walk tts_words cumulatively. For TTS word k with
    # text "abc", assume each char in "abc" occupies an equal slice of
    # [tb_k, te_k]. Then a script char matches the tts word whose slice
    # covers its expected cumulative position. We track the cumulative
    # script position to choose the right slice.
    char_entries = []
    tts_index = 0
    script_pos = 0  # position within script excluding whitespace
    script_total = len([c for c in script if c.strip() and c != "\n"])
    if not tts_words or script_total == 0:
        pass
    else:
        # Pre-compute per-char tts boundaries. For TTS word with text w
        # and time [tb, te], split into len(w) equal intervals.
        flat_chars = []  # list of (c, tb, te) at single-char granularity
        for w, tb, te in tts_words:
            n = len(w)
            if n == 0:
                continue
            span = te - tb
            for i, ch in enumerate(w):
                c_tb = tb + span * i / n
                c_te = tb + span * (i + 1) / n
                flat_chars.append((ch, c_tb, c_te))
        # Now script and flat_chars are both per-character lists. Walk
        # them in parallel: when they don't match, search forward. When
        # flat_chars contains a multi-char cluster (e.g. "2024") and
        # script has the same chars, consume them 1:1.
        flat_i = 0
        for sc in script:
            if sc.strip() == "" or sc == "\n":
                continue
            if flat_i >= len(flat_chars):
                break
            ch, tb, te = flat_chars[flat_i]
            if ch != sc:
                # Search forward up to 20 chars for matching char
                matched = None
                for probe in range(flat_i + 1, min(flat_i + 21, len(flat_chars))):
                    if flat_chars[probe][0] == sc:
                        matched = probe
                        break
                if matched is not None:
                    ch, tb, te = flat_chars[matched]
            char_entries.append({"c": sc, "start": round(tb / 1000, 3), "end": round(te / 1000, 3), "word": ch})
            flat_i += 1

    # Build sentence spans from script punctuation positions. The
    # flat_chars walk above already produced one char_entry per
    # non-whitespace script char (including punctuation) in script
    # order, so we just slice sentence windows here.
    sentences = []
    cur = []
    next_idx = 0
    for ch in char_entries:
        cur.append(ch)
        if ch["c"] in "。！？!?\.":
            text = "".join(c["c"] for c in cur)
            sentences.append({
                "text": text,
                "start": cur[0]["start"],
                "end": cur[-1]["end"],
                "word_indices": list(range(next_idx, next_idx + len(cur))),
            })
            next_idx += len(cur)
            cur = []
    if cur:
        text = "".join(c["c"] for c in cur)
        sentences.append({
            "text": text,
            "start": cur[0]["start"],
            "end": cur[-1]["end"],
            "word_indices": list(range(next_idx, next_idx + len(cur))),
        })

    voice_sec = char_entries[-1]["end"] if char_entries else 0.0
    out = {
        "voice_seconds": round(voice_sec, 3),
        "script_chars": len(script),
        "model": "MiniMax-t2a-v2-word-timestamps",
        "word_count": len([c for c in char_entries if c["word"]]),
        "char_count_aligned": len(char_entries),
        "sentence_count": len(sentences),
        "chars": char_entries,
        "sentences": sentences,
    }
    out_json.write_text(_json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def _resolve_raw_mp4(job_id):
    """Path to the canonical raw.mp4 that render_placeholder writes to.

    render_placeholder writes composition/video-only.mp4 then copies it to
    video/raw.mp4 (see render_jobs process_one). After a re-render we need
    to point back at the freshly written file.
    """
    return VIDEO_RUNS_DIR / job_id / "video" / "raw.mp4"


def rerender_with_alignment(job_id, script_text):
    """Re-run render_placeholder once alignment.json is available.

    The first render pass (in render daemon) couldn't see alignment.json
    because TTS hasn't run yet. Now that TTS server timestamps have
    produced real per-
    scene TTS timestamps, we re-render so scene boundaries and caption
    timing match what the voice actually says.

    Pexels images/videos and LLM keywords are cached on disk, so this is
    effectively "regenerate index.html + rerun hyperframes" — ~12 min
    for 110s @ 15fps, no network calls.
    """
    run_dir = VIDEO_RUNS_DIR / job_id
    render_dir = run_dir / "composition"
    if not (render_dir / "images").exists():
        raise RuntimeError(f"no composition dir at {render_dir}, cannot rerender")

    job_meta = json.loads((JOBS_DIR / f"{job_id}.json").read_text(encoding="utf-8"))
    width = int((job_meta.get("render") or {}).get("width", 1920))
    height = int((job_meta.get("render") or {}).get("height", 1080))
    fps = int((job_meta.get("render") or {}).get("fps", 15))
    # Use the TTS-measured voice duration (already computed earlier in
    # process_one) as the new total_duration so the 18 scene spans line
    # up with how long TTS actually took, not the pre-narrate estimate.
    voice_mp3 = run_dir / "audio" / "voice.mp3"
    if voice_mp3.exists():
        measured = get_duration_sec(voice_mp3)
    else:
        measured = float((job_meta.get("render") or {}).get("duration_sec") or 110)
    log(f"  re-rendering with alignment: {width}x{height}@{fps}fps, "
        f"total_duration={measured:.1f}s (voice-measured)")

    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "process_video_render_jobs",
        Path(__file__).resolve().parent / "process_video_render_jobs.py",
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _mod.render_placeholder(
        job_id=job_id,
        render_dir=render_dir,
        script_text=script_text,
        theme=job_meta.get("theme", ""),
        width=width,
        height=height,
        total_duration=measured,
        fps=fps,
    )
    # render_placeholder writes composition/video-only.mp4; copy to video/raw.mp4
    # so downstream merge_video_audio picks up the alignment-driven version.
    new_raw = _resolve_raw_mp4(job_id)
    new_raw.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(render_dir / "video-only.mp4", new_raw)
    log(f"  re-rendered raw.mp4: {new_raw.stat().st_size} bytes, "
        f"duration={get_duration_sec(new_raw):.1f}s")


def mix_voice_with_bgm_loop(
    voice_mp3, bgm_mp3, out_mp3, bgm_volume, target_duration
):
    """Pad voice with silence and loop BGM to the target duration."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(voice_mp3),
        "-stream_loop", "-1", "-i", str(bgm_mp3),
        "-filter_complex",
        (
            f"[0:a]apad,atrim=0:{target_duration},asetpts=PTS-STARTPTS[voice];"
            f"[1:a]volume={bgm_volume},atrim=0:{target_duration},"
            f"asetpts=PTS-STARTPTS[bgm];"
            "[voice][bgm]amix=inputs=2:duration=longest:dropout_transition=0[a]"
        ),
        "-map", "[a]",
        "-c:a", "libmp3lame", "-b:a", "192k",
        "-t", str(target_duration),
        str(out_mp3),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg mix failed: {(result.stderr or result.stdout)[-1000:]}")


def merge_video_audio(video_mp4, audio_mp3, out_mp4):
    """Merge once; never truncate either stream — extend the shorter one instead.

    RC1 fix: previous code used `-t v_dur` when audio was shorter than video,
    which silently dropped the last 3-9s of TTS. Now both streams are clamped
    to output_duration = max(v_dur, a_dur):
    - video shorter than audio → tpad=clone extends the last video frame
    - audio shorter than video → apad adds silence to the audio tail
    """
    v_dur = get_duration_sec(video_mp4)
    a_dur = get_duration_sec(audio_mp3)
    output_duration = max(v_dur, a_dur)
    log(f"  video={v_dur:.1f}s, audio={a_dur:.1f}s, output={output_duration:.1f}s")

    if a_dur > v_dur + 0.1:
        extension = a_dur - v_dur
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_mp4),
            "-i", str(audio_mp3),
            "-filter_complex",
            f"[0:v]tpad=stop_mode=clone:stop_duration={extension:.3f}[v]",
            "-map", "[v]",
            "-map", "1:a",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-c:a", "aac",
            "-b:a", "192k",
            "-t", str(output_duration),
            str(out_mp4),
        ]
    else:
        pad = max(output_duration - a_dur, 0.0)
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_mp4),
            "-i", str(audio_mp3),
            "-filter_complex",
            f"[1:a]apad=pad_dur={pad:.3f}[a]",
            "-map", "0:v",
            "-map", "[a]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-t", str(output_duration),
            str(out_mp4),
        ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg merge failed: {(result.stderr or result.stdout)[-1000:]}")


def upload_mp4(local_path, slug, short_id, kind):
    from datetime import datetime as _dt
    date_str = _dt.now().strftime("%Y-%m-%d")
    key = f"{date_str}/video-studio/video-{slug}-{short_id}-{kind}.mp4"
    cmd = [
        "python3", str(UPLOAD_SCRIPT),
        "--file", str(local_path),
        "--key", key,
        "--content-type", "video/mp4",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"upload failed: {(result.stderr or result.stdout)[-500:]}")
    return result.stdout.strip()


def get_duration_sec(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return float(result.stdout.strip())


def process_one(job):
    job_id = job["id"]
    theme = job.get("theme", "")
    log(f"narrating {job_id}: theme={theme!r}")

    job["status"] = "narrating"
    job["error"] = None
    save_job(job)

    run_dir = VIDEO_RUNS_DIR / job_id
    audio_dir = run_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    final_dir = run_dir
    final_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Resolve voice from registry + job override
        registry = json.loads(VOICE_REGISTRY.read_text(encoding="utf-8"))
        audio_cfg = job.get("audio", {})
        voice = audio_cfg.get("voice", "Chinese (Mandarin)_Radio_Host")
        speed = float(audio_cfg.get("speed", 1.0))
        bgm_enabled = bool(audio_cfg.get("bgm_enabled", False))
        bgm_volume = float(audio_cfg.get("bgm_volume", 0.15))
        display = registry.get(voice, {}).get("display_name", voice)
        log(f"  voice={display} (id={voice}), speed={speed}, bgm={bgm_enabled}, bgm_volume={bgm_volume}")

        script = (job.get("script") or "").strip()
        if not script:
            raise RuntimeError("job.script is empty; cannot narrate")

        # 1. TTS audio (local scripts/minimax_tts.py)
        voice_mp3 = audio_dir / "voice.mp3"
        tts_synthesize(script, voice, speed, voice_mp3)
        log(f"  TTS done: {voice_mp3.stat().st_size} bytes")

        # 1a. Per-word timestamps from the same TTS endpoint. Independent
        # of audio: makes a second HTTP request with subtitle_enable=true
        # + subtitle_type=word, downloads data.subtitle_file JSON. Failure
        # here is non-fatal — render daemon will fall back to equal-time.
        sub_json = audio_dir / "voice.subtitle.json"
        try:
            _fetch_tts_subs(script, voice, speed, sub_json)
            log(f"  TTS subs fetched: {sub_json.stat().st_size} B")
        except Exception as e:
            log(f"  ⚠ TTS subs fetch failed (non-fatal): {e}")

        # 1b. Build alignment.json from TTS-provided word timestamps.
        # No subprocess, no ASR — server guarantees per-char ms accuracy.
        if sub_json.exists():
            try:
                if _build_alignment_from_tts_subs(job_id):
                    log(f"  alignment built from TTS word timestamps")
                else:
                    log(f"  ⚠ subtitle JSON present but alignment build returned False; falling back to equal-time")
            except Exception as e:
                log(f"  ⚠ alignment from TTS failed (non-fatal): {e}")
        else:
            log(f"  ⚠ no voice.subtitle.json; falling back to equal-time")

        # 1c. Re-render video with alignment-driven scene timing
        # (RC3 + pipeline order fix). Render daemon ran first (with no
        # alignment.json available), so its video-only.mp4 used equal-time
        # splits. Now that alignment.json exists, re-run render_placeholder
        # to rebuild composition/index.html + video-only.mp4 with real
        # per-scene TTS spans. Image/video downloads are cached so this is
        # ~hyperframes-screenshot time only (~12 min for 110s @ 15fps).
        try:
            rerender_with_alignment(job_id, script)
            job["render"]["mp4_path"] = str(_resolve_raw_mp4(job_id))
        except Exception as e:
            log(f"  ⚠ alignment-aware re-render failed (non-fatal, using pre-narrate video): {e}")

        video_mp4 = Path(job["render"]["mp4_path"])
        if not video_mp4.exists():
            raise RuntimeError(f"rendered video not found: {video_mp4}")
        video_duration = get_duration_sec(video_mp4)
        voice_duration = get_duration_sec(voice_mp3)
        mix_duration = max(video_duration, voice_duration)
        # Drift check: voice should fit video budget within ±2s.
        # If script daemon computed target_seconds correctly, |drift| <= 1s.
        drift = abs(voice_duration - video_duration)
        if drift > 2.0:
            log(
                f"  ⚠ duration drift {drift:.2f}s "
                f"(video={video_duration:.2f}s, voice={voice_duration:.2f}s) — "
                f"字幕可能与配音不同步"
            )
        else:
            log(f"  duration drift {drift:.2f}s (video={video_duration:.2f}s, voice={voice_duration:.2f}s)")

        # 2. Mix with BGM (only if enabled). When disabled, mixed.mp3 is just
        # voice.mp3 renamed/copied so the merge step keeps working unchanged.
        mixed_mp3 = audio_dir / "mixed.mp3"
        if bgm_enabled:
            mix_voice_with_bgm_loop(
                voice_mp3, BGM_PATH, mixed_mp3, bgm_volume, mix_duration
            )
            log(f"  BGM mix done: {mixed_mp3.stat().st_size} bytes")
        else:
            shutil.copyfile(voice_mp3, mixed_mp3)
            log(f"  BGM disabled — using voice-only: {mixed_mp3.stat().st_size} bytes")

        # 3. Merge with video
        final_mp4 = final_dir / "final.mp4"
        merge_video_audio(video_mp4, mixed_mp3, final_mp4)
        log(f"  merge done: {final_mp4.stat().st_size} bytes")

        # 4. Probe
        size_bytes = final_mp4.stat().st_size
        duration = get_duration_sec(final_mp4)

        # 4b. Persist actual TTS rate for next job's duration budget
        # RC2/RC5: blind 5.4 chars/sec × 1.08 + 5s padding kept drifting +3~+9s.
        # Write the measured voice duration / rate back so script daemon can
        # converge on the real cadence instead of guessing.
        char_count = len(job.get("script", "") or "")
        if char_count > 0 and voice_duration > 0:
            job.setdefault("script_meta", {})
            job["script_meta"]["actual_seconds"] = round(voice_duration, 2)
            job["script_meta"]["actual_rate"] = round(voice_duration / char_count, 3)
            log(f"  actual_rate={voice_duration / char_count:.3f} chars/sec (n={char_count})")

        # 5. Upload
        short_id = job_id.split("_")[-1] if "_" in job_id else job_id[-6:]
        slug = safe_slug(theme) or "untitled"
        r2_url = upload_mp4(final_mp4, slug, short_id, "final")
        log(f"  uploaded: {r2_url[:100]}...")

        # 6. Update job
        job["audio"]["voice_mp3_path"] = str(voice_mp3)
        job["audio"]["mixed_mp3_path"] = str(mixed_mp3)
        job["final"] = {
            "mp4_path": str(final_mp4),
            "mp4_url": r2_url,
            "duration_sec": duration,
            "size_bytes": size_bytes,
        }
        job["status"] = "final"
        job.setdefault("logs", []).append(
            f"{now_iso()} final done: {duration:.1f}s, {size_bytes} bytes, uploaded"
        )
        save_job(job)
        log(f"{job_id} -> final, duration={duration:.1f}s")
        return True

    except Exception as e:
        log(f"{job_id} NARRATE FAILED: {e}")
        job["status"] = "error"
        job["error"] = f"narrate daemon: {type(e).__name__}: {e}"
        job.setdefault("logs", []).append(f"{now_iso()} NARRATE FAILED: {e}")
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

        if NARRATE_TRIGGER.exists():
            deadline = time.time() + 12
            while time.time() < deadline:
                mtime = NARRATE_TRIGGER.stat().st_mtime
                age = time.time() - mtime
                if age >= 3:
                    break
                time.sleep(min(3, max(0.2, 3 - age)))

        if LAST_RUN_MARKER.exists():
            try:
                last = float(LAST_RUN_MARKER.read_text(encoding="utf-8").strip() or "0")
            except ValueError:
                last = 0
            gap = time.time() - last
            if gap < 30 and last:
                wait = 30 - gap
                log(f"throttling: previous run {gap:.1f}s ago, sleeping {wait:.1f}s")
                time.sleep(wait)

        processed = 0
        for _ in range(1):
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
