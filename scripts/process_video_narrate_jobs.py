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


# Cross-repo reference: video-studio narrate daemon depends on
# voice-studio's shared scripts (minimax_tts.py, voice_registry.json).
# Override with VOICE_STUDIO_DIR env var if voice-studio is relocated.
VOICE_STUDIO_DIR = Path(os.environ.get(
    "VOICE_STUDIO_DIR",
    "/root/.openclaw/workspace/skills/voice-studio",
))

SKILL_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = SKILL_DIR.parents[1]
JOBS_DIR = SKILL_DIR / "jobs" / "video"
VIDEO_RUNS_DIR = Path("/root/.openclaw/workspace/skills/video-studio/runs")
BGM_PATH = SKILL_DIR / "assets" / "bgm_default.mp3"
TTS_SCRIPT = VOICE_STUDIO_DIR / "scripts" / "minimax_tts.py"
UPLOAD_SCRIPT = SKILL_DIR / "scripts" / "upload_to_cos.py"
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
    """Merge once, without repeating narration; extend the last frame if needed."""
    v_dur = get_duration_sec(video_mp4)
    a_dur = get_duration_sec(audio_mp3)
    output_duration = max(v_dur, a_dur)
    log(f"  video={v_dur:.1f}s, audio={a_dur:.1f}s, output={output_duration:.1f}s")

    if a_dur <= v_dur + 0.1:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_mp4),
            "-i", str(audio_mp3),
            "-map", "0:v",
            "-map", "1:a",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-t", str(v_dur),
            str(out_mp4),
        ]
    else:
        extension = a_dur - v_dur
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_mp4),
            "-i", str(audio_mp3),
            "-filter_complex",
            f"[0:v]tpad=stop_mode=clone:stop_duration={extension}[v]",
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
        bgm_volume = float(audio_cfg.get("bgm_volume", 0.06))
        display = registry.get(voice, {}).get("display_name", voice)
        log(f"  voice={display} (id={voice}), speed={speed}, bgm_volume={bgm_volume}")

        script = (job.get("script") or "").strip()
        if not script:
            raise RuntimeError("job.script is empty; cannot narrate")

        # 1. TTS
        voice_mp3 = audio_dir / "voice.mp3"
        tts_synthesize(script, voice, speed, voice_mp3)
        log(f"  TTS done: {voice_mp3.stat().st_size} bytes")

        video_mp4 = Path(job["render"]["mp4_path"])
        if not video_mp4.exists():
            raise RuntimeError(f"rendered video not found: {video_mp4}")
        video_duration = get_duration_sec(video_mp4)
        voice_duration = get_duration_sec(voice_mp3)
        mix_duration = max(video_duration, voice_duration)

        # 2. Mix with BGM. Voice is never looped; a short tail is BGM-only.
        mixed_mp3 = audio_dir / "mixed.mp3"
        mix_voice_with_bgm_loop(
            voice_mp3, BGM_PATH, mixed_mp3, bgm_volume, mix_duration
        )
        log(f"  mix done: {mixed_mp3.stat().st_size} bytes")

        # 3. Merge with video
        final_mp4 = final_dir / "final.mp4"
        merge_video_audio(video_mp4, mixed_mp3, final_mp4)
        log(f"  merge done: {final_mp4.stat().st_size} bytes")

        # 4. Probe
        size_bytes = final_mp4.stat().st_size
        duration = get_duration_sec(final_mp4)

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
