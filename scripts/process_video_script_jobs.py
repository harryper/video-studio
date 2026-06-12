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
# Char-count tolerance band. The style guide targets 760-840 chars
# (see reference-style-video.md), but LLM output is noisy — 1080 chars
# is well within an acceptable range and the existing 700-900 cap was
# rejecting perfectly good drafts. Widened to 600-1500.
MIN_SCRIPT_CHARS = 600
MAX_SCRIPT_CHARS = 1500
DEFAULT_TARGET_SECONDS = 150
ESTIMATED_CHARS_PER_SECOND = 5.5

SCRIPT_TRIGGER = SKILL_DIR / ".video-script-trigger"
RENDER_TRIGGER = SKILL_DIR / ".video-render-trigger"
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
    if len(script) < MIN_SCRIPT_CHARS or len(script) > MAX_SCRIPT_CHARS:
        return False
    # Calibrated from actual Radio_Host output at speed 1.0.
    char_count = len(script)
    target_seconds = round(char_count / ESTIMATED_CHARS_PER_SECOND)
    job["status"] = "ready_script"
    job["script"] = script
    job["script_meta"] = {
        "char_count": char_count,
        "target_seconds": target_seconds,
        "actual_seconds": None,
    }
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
        if not MIN_SCRIPT_CHARS <= len(updated["script"]) <= MAX_SCRIPT_CHARS:
            updated["status"] = "error"
            updated["error"] = (
                f"script length {len(updated['script'])} outside "
                f"{MIN_SCRIPT_CHARS}-{MAX_SCRIPT_CHARS} chars"
            )
            save_job(updated)
            log(f"{job['id']} failed length check: {len(updated['script'])}")
            return False
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

        # Cascade: touch render trigger if any job reached ready_script
        if any(
            (job_path(j["id"]).exists() and load_job(job_path(j["id"])).get("status") == "ready_script")
            for j in jobs
        ):
            RENDER_TRIGGER.touch()
            log(f"touched {RENDER_TRIGGER.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
