#!/usr/bin/env python3
"""Host-side writer for video-studio script jobs (mode='video').

Mirrors the structure of process_pending_voice_jobs.py:
- Triggered by .video-script-trigger (systemd path unit)
- Dispatches an `openclaw agent` sub-session to write the narration script
- On success: status -> ready_script, then touches .video-render-trigger

Differences from voice writer:
- Job dir is jobs/video/ (not jobs/voice/)
- Job id prefix is 'v_' (not arbitrary UUID)
- Status target is 'ready_script' (not 'ready')
- Min chars is 700 (not 3300) — video scripts target about 150 seconds
- On success, cascades to render trigger (voice writer has no successor)
"""

import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = SKILL_DIR.parents[1]
OPENCLAW_ROOT = WORKSPACE_DIR.parent
JOBS_DIR = SKILL_DIR / "jobs" / "video"
RUNS_DIR = Path("/root/.openclaw/workspace/skills/video-studio/runs")
LOCK_PATH = SKILL_DIR / ".video-script-writer.lock"
NODE = Path("/usr/bin/node")
OPENCLAW = Path("/usr/lib/node_modules/openclaw/openclaw.mjs")
# Char-count tolerance band. The style guide targets 560-640 chars
# (see reference-style-video.md, 抖音科普短片节奏更紧凑), but LLM output
# is noisy — widened to 450-900.
MIN_SCRIPT_CHARS = 450
MAX_SCRIPT_CHARS = 900
DEFAULT_TARGET_SECONDS = 110
# Empirically calibrated from MiniMax-TTS (model=speech-2.8-hd) Radio_Host:
# 实测 628 字 / speed 1.15 → 112.9s (5.56 chars/sec), 644 字 → 110.1s (5.85)。
# 中文 TTS 实际节奏受标点/换气影响大；为了不过分欠长，多留 4% 余量。
ESTIMATED_CHARS_PER_SECOND = 5.4
DRIFT_SAFETY_SECONDS = 5

SCRIPT_TRIGGER = SKILL_DIR / ".video-script-trigger"
RENDER_TRIGGER = SKILL_DIR / ".video-render-trigger"
NARRATE_TRIGGER = SKILL_DIR / ".video-narrate-trigger"
LAST_RUN_MARKER = SKILL_DIR / ".video-script-writer.lastrun"
REFERENCE_STYLE = Path("/root/.openclaw/workspace/skills/video-studio/reference-style-video.md")
LOG_FILE = Path("/var/log/video-studio/video-script-watcher.log")


def log(msg):
    line = f"[video-script-writer] {msg}"
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
        if job.get("mode") == "video" and job.get("status") == "pending":
            jobs.append(job)
    return sorted(jobs, key=lambda j: j.get("created_at", ""))


def build_prompt(job):
    job_id = job["id"]
    theme = job.get("theme") or ""
    ref_path = REFERENCE_STYLE
    ref_relpath = str(ref_path.relative_to(WORKSPACE_DIR)) if ref_path.exists() else "(reference-style-video.md missing)"
    target_seconds = int(job.get("render", {}).get("duration_sec") or DEFAULT_TARGET_SECONDS)
    target_chars = round(target_seconds * ESTIMATED_CHARS_PER_SECOND)
    min_chars = max(MIN_SCRIPT_CHARS, target_chars - 65)
    max_chars = min(MAX_SCRIPT_CHARS, target_chars + 15)
    return (
        f"为 video-studio Web 项目写一段约 {target_seconds} 秒的短视频旁白稿。主题：{theme}\n\n"
        f"先读并严格遵守：{ref_relpath}\n"
        "（如果该文件不存在，按'开头冲突 / 场景 / 核心判断前 3 段，结尾带互动钩子，中间短句节奏'写）\n\n"
        "要求：\n"
        f"1. 严格控制在 {min_chars}-{max_chars} 中文字，目标约 {target_chars} 字\n"
        "2. 纯文本输出, 不要 markdown / 编号 / 标题 / 空行分隔\n"
        "3. 开头 60-90 字内必须出现冲突 / 场景 / 核心判断\n"
        "4. 结尾带互动 / 站队 / 转发钩子\n"
        f"5. 文稿写入 skills/video-studio/runs/{job_id}/script.txt\n"
        f"6. 更新 jobs/video/{job_id}.json: status=\"ready_script\", script=<全文>, script_meta={{char_count, target_seconds, actual_seconds=null}}, error=null\n"
        f"7. job_id={job_id}\n\n"
        "执行纪律：\n"
        "- **首次写入即终稿**: 不要反复自我检查 / 改写 / 重写。最多 3 次写入, 第一次写完直接落盘。\n"
        "- 不要把全文写在 thinking 或最终回复里, 必须用文件写入工具落盘\n"
        f"- 文稿字数 < {MIN_SCRIPT_CHARS} 或 > {MAX_SCRIPT_CHARS} 视为失败\n"
        "- 不要生成音频, 不要发布, 不要给用户发消息\n"
        "- 最终回复只允许一句话: '已写入 <路径>'"
    )


def run_agent(job):
    prompt = build_prompt(job)
    attempt = int(job.get("writer_attempt") or 0) + 1
    job["writer_attempt"] = attempt
    save_job(job)
    cmd = [
        str(NODE),
        str(OPENCLAW),
        "agent",
        "--agent",
        "main",
        "--session-key",
        f"agent:main:video-studio-writer-{job['id']}-a{attempt}",
        "--message",
        prompt,
        "--thinking",
        "off",
        "--json",
        "--timeout",
        "300",  # 5 min — much shorter than voice writer's 900s
    ]
    return subprocess.run(
        cmd,
        cwd=str(WORKSPACE_DIR),
        text=True,
        capture_output=True,
        timeout=360,
    )


def _session_jsonl_path(job_id):
    """Same lookup pattern as voice writer."""
    sessions_index = OPENCLAW_ROOT / "agents" / "main" / "sessions" / "sessions.json"
    if not sessions_index.exists():
        return None
    try:
        with sessions_index.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    needle = f"agent:main:video-studio-writer-{job_id}"
    info = data.get(needle) or {}
    session_file = info.get("sessionFile")
    if not session_file:
        return None
    return Path(session_file)


def scrape_session_error(job_id, result):
    """Same pattern as voice writer — pull real error from session jsonl."""
    fallback = ((result.stderr or result.stdout or "").strip() or "unknown error")[:800]
    fallback_msg = f"openclaw agent failed on host: {fallback}"

    session_file = _session_jsonl_path(job_id)
    if not session_file or not session_file.exists():
        return fallback_msg

    last_assistant = None
    last_texts = []
    tool_call_count = 0
    had_tool_error = False
    try:
        with session_file.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("message") or {}
                if msg.get("role") == "assistant":
                    last_assistant = msg
                    last_texts = [
                        c.get("text", "") for c in msg.get("content", [])
                        if c.get("type") == "text" and c.get("text")
                    ]
                    for c in msg.get("content", []):
                        if c.get("type") == "toolCall":
                            tool_call_count += 1
                if msg.get("role") == "toolResult" and msg.get("isError"):
                    had_tool_error = True
    except OSError:
        return fallback_msg

    if not last_assistant:
        return fallback_msg

    err_msg = last_assistant.get("errorMessage")
    if err_msg:
        return f"openclaw agent failed: {err_msg}"

    stop_reason = last_assistant.get("stopReason")
    if stop_reason == "error" and not err_msg:
        return f"openclaw agent failed: stopReason=error (no errorMessage); rc={result.returncode}"

    last_text = " ".join(last_texts).strip()

    if stop_reason == "stop" and tool_call_count == 0 and last_text:
        preview = last_text[:160].replace("\n", " ")
        return (
            "openclaw agent returned without any tool call but reported done "
            f"(model hallucination, last text: \"{preview}\")"
        )

    if not last_text and not had_tool_error:
        return (
            f"openclaw agent returned no assistant text and no tool calls; "
            f"rc={result.returncode}; stderr={fallback[:200]}"
        )

    return fallback_msg


def finalize_from_script_file(job):
    """If the agent wrote runs/<id>/script.txt, copy its content into the job."""
    script_path = RUNS_DIR / job["id"] / "script.txt"
    if not script_path.exists():
        return False
    script = script_path.read_text(encoding="utf-8").strip()
    if not script:
        return False
    # preview_only: accept shorter scripts (10s demo scripts can be <450 chars)
    is_preview = bool((job.get("render") or {}).get("preview_only", False))
    min_chars = 50 if is_preview else MIN_SCRIPT_CHARS
    if len(script) < min_chars or len(script) > MAX_SCRIPT_CHARS:
        return False
    # RC2/RC5: prefer the rate measured by the last few final jobs (narrate
    # daemon writes script_meta.actual_rate = voice_seconds/char_count, i.e.
    # seconds per character). We need chars per second for the duration
    # formula, so flip it. Cold start falls back to the calibrated
    # 5.4 chars/sec × speed constant.
    char_count = len(script)
    speed = float((job.get("audio") or {}).get("speed", 1.0))
    history_sec_per_char = []
    try:
        for p in sorted(JOBS_DIR.glob("v_*.json"))[-10:]:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                spc = (d.get("script_meta") or {}).get("actual_rate")
                if (
                    d.get("status") == "final"
                    and isinstance(spc, (int, float))
                    and spc > 0
                ):
                    history_sec_per_char.append(float(spc))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
    except OSError:
        pass
    if history_sec_per_char:
        # 1 / mean(sec/char) = mean char/sec, but to stay robust against
        # outliers we convert each measurement back to char/sec then average.
        cps = [1.0 / s for s in history_sec_per_char]
        effective_rate = sum(cps) / len(cps)
    else:
        effective_rate = ESTIMATED_CHARS_PER_SECOND * speed
    target_seconds = round(char_count / effective_rate)
    # preview_only: respect user-specified duration exactly (10s demo =
    # user-supplied). Non-preview: keep a small 2% + 2s tail for
    # sentence-final pauses instead of the old 8% + 5s double buffer;
    # 30s minimum guards against microscopic shorts breaking the
    # hyperframes renderer.
    preview_only = bool((job.get("render") or {}).get("preview_only", False))
    if preview_only:
        video_duration_sec = int((job.get("render") or {}).get("duration_sec", 10))
        # Trust voice_seconds is close to user target; do NOT add +2 tail.
        # If TTS drifts shorter we let the mp4 end with a brief black tail.
    else:
        video_duration_sec = max(
            round(target_seconds * 1.02) + 2, 30,
        )
    # preview_only: skip the full render daemon (image fetch + hyperframes).
    # Status is set to "rendered" so the narrate daemon picks it up directly
    # and runs preview_caption_ffmpeg to produce a black-bg mp4.
    job["status"] = "rendered" if preview_only else "ready_script"
    job["script"] = script
    job["script_meta"] = {
        "char_count": char_count,
        "target_seconds": target_seconds,
        "effective_rate": effective_rate,
        "actual_seconds": None,
    }
    # 单一时间预算：render 读这个值，TTS 后用 ffprobe 校准，drift 控制在 ±1s
    job.setdefault("render", {})["duration_sec"] = video_duration_sec
    job["error"] = None
    job["updated_at"] = now_iso()
    save_job(job)
    return True


def process_one(job):
    job["status"] = "writing"
    job["error"] = None
    save_job(job)

    try:
        result = run_agent(job)
    except subprocess.TimeoutExpired:
        current = load_job(job_path(job["id"]))
        if finalize_from_script_file(current):
            log(f"{job['id']} ready_script from script file after agent timeout")
            return True
        current["status"] = "error"
        current["error"] = "openclaw agent timed out after 360s"
        save_job(current)
        log(f"{job['id']} timed out")
        return False

    try:
        updated = load_job(job_path(job["id"]))
    except (OSError, json.JSONDecodeError) as exc:
        updated = dict(job)
        log(f"{job['id']} job json unreadable after agent run: {exc}")
        if result.returncode == 0 and finalize_from_script_file(updated):
            log(f"{job['id']} ready from script file after json repair")
            return True
        updated["status"] = "error"
        updated["error"] = f"job json unreadable after agent run: {exc}"
        save_job(updated)
        return False

    if updated.get("status") == "ready_script" and (updated.get("script") or "").strip():
        # preview_only: skip the 450-char minimum (10s demo scripts are short)
        is_preview = bool((updated.get("render") or {}).get("preview_only", False))
        min_chars = 50 if is_preview else MIN_SCRIPT_CHARS
        if not min_chars <= len(updated["script"]) <= MAX_SCRIPT_CHARS:
            updated["status"] = "error"
            updated["error"] = (
                f"script length {len(updated['script'])} outside "
                f"{min_chars}-{MAX_SCRIPT_CHARS} chars"
            )
            save_job(updated)
            log(f"{job['id']} failed length check: {len(updated['script'])} (preview={is_preview})")
            return False
        # preview_only: downgrade from ready_script -> rendered so the
        # cascade below does not touch the render trigger (which would
        # kick off the full image-fetch pipeline). Narrate daemon picks
        # up status=rendered jobs and runs preview_caption_ffmpeg.
        if is_preview:
            updated["status"] = "rendered"
            save_job(updated)
            log(f"{job['id']} rendered (preview_only, {len(updated['script'])} chars)")
            return True
        log(f"{job['id']} ready_script ({len(updated['script'])} chars)")
        return True

    if result.returncode == 0 and finalize_from_script_file(updated):
        log(f"{job['id']} ready_script from script file")
        return True

    # Belt-and-suspenders: even when the agent sub-process exits non-zero,
    # trust the on-disk artefact. If the agent wrote a valid script.txt we
    # treat it as success — the sub-process returncode can be misleading
    # when the agent completes via a final write-tool call.
    if finalize_from_script_file(updated):
        log(f"{job['id']} ready_script from script file (rc={result.returncode})")
        return True

    real_err = scrape_session_error(job["id"], result)
    updated["status"] = "error"
    updated["error"] = real_err
    save_job(updated)
    tail = real_err.replace("\n", " ")[:300]
    log(f"{job['id']} failed: {tail}")
    return False


def main():
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    with LOCK_PATH.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another writer is running, skipping")
            return 0

        # Debounce
        if SCRIPT_TRIGGER.exists():
            deadline = time.time() + 12
            while time.time() < deadline:
                mtime = SCRIPT_TRIGGER.stat().st_mtime
                age = time.time() - mtime
                if age >= 3:
                    break
                time.sleep(min(3, max(0.2, 3 - age)))

        # Throttle
        if LAST_RUN_MARKER.exists():
            try:
                last = float(LAST_RUN_MARKER.read_text(encoding="utf-8").strip() or "0")
            except ValueError:
                last = 0
            gap = time.time() - last
            if gap < 15 and last:
                wait = 15 - gap
                log(f"throttling: previous run {gap:.1f}s ago, sleeping {wait:.1f}s")
                time.sleep(wait)

        processed = 0
        for _ in range(1):  # max 1 job per run, just like voice writer
            jobs = pending_jobs()
            if not jobs:
                break
            process_one(jobs[0])
            processed += 1

        LAST_RUN_MARKER.write_text(f"{time.time()}\n", encoding="utf-8")
        log(f"processed={processed}")

        # Cascade: touch render trigger if any job reached ready_script.
        # For preview_only jobs we skip the render daemon and go straight
        # to narrate (black-bg ffmpeg), so touch NARRATE_TRIGGER instead.
        touched_render = False
        touched_narrate = False
        for j in jobs:
            jp = job_path(j["id"])
            if not jp.exists():
                log(f"  cascade: skip {j['id']} (json missing)")
                continue
            cur = load_job(jp)
            st = cur.get("status")
            is_preview = bool((cur.get("render") or {}).get("preview_only", False))
            log(f"  cascade: {j['id']} status={st!r} preview={is_preview}")
            if st == "rendered" and is_preview:
                NARRATE_TRIGGER.touch()
                touched_narrate = True
            elif st == "ready_script":
                RENDER_TRIGGER.touch()
                touched_render = True
        if touched_render:
            log(f"touched {RENDER_TRIGGER.name}")
        if touched_narrate:
            log(f"touched {NARRATE_TRIGGER.name} (preview_only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
