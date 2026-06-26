---
name: video-studio
description: "video-studio skill: shared reference-style + run artifacts for the video mode of voice-studio Web project. The video mode auto-produces 60-90s 1080x1920 short videos with hyperframes rendering + MiniMax TTS. All creation goes through the voice-studio Web job workflow (mode='video' tab)."
---

# video-studio skill

Technical skill id: `video-studio`. User-facing name: **video-studio skill**.

This skill is a **sibling of voice-studio**, not a standalone product. It holds the **shared creative reference material + run artifacts** for `mode='video'` jobs in the voice-studio Web project. The actual product, daemons, and UI all live in `skills/voice-studio/`.

## Canonical Web workflow

All video work goes through the voice-studio Web project, on the 🎬 视频 tab.

For `mode='video'` jobs:
- `pending` → script daemon (LLM) writes narration → `status='ready_script'`
- `ready_script` → render daemon produces hyperframes mp4 (no audio) → `status='rendered'`
- `rendered` → narrate daemon does TTS + BGM + ffmpeg merge → `status='final'`

**Auto-pilot**: zero human review gates. The user just submits a topic; pipeline cascades through all stages.

The main chat session should only orchestrate via the Web job workflow. Do not draft scripts, render HTML, or run TTS directly in chat for video jobs.

## Reference material

- `reference-style-video.md` — canonical abstraction of the video narration style (60-90s, 180-250 chars, sentence ≤ 22 chars, opening conflict, ending interactive hook). The script daemon's LLM prompt references this file.
- `reference-scripts/01-ai-terms.md` — example reference script for "AI 名词其实就 5 个" topic (215 chars, 7 card rhythm). Used as a stylistic template; the script daemon does NOT copy from it.

## Run artifacts

- `runs/{job_id}/script.txt` — the LLM-written narration script
- `runs/{job_id}/composition/index.html` — the dynamic hyperframes composition (P2+: generated from script text)
- `runs/{job_id}/video/raw.mp4` — the rendered mp4 (no audio)
- `runs/{job_id}/audio/voice.mp3` — TTS voice mp3
- `runs/{job_id}/audio/mixed.mp3` — voice + BGM mix (BGM looped to voice duration)
- `runs/{job_id}/audio/script.txt` — copy of the script passed to TTS
- `runs/{job_id}/final.mp4` — the final merged video (video + audio)
- `assets/bgm_default.mp3` — BGM (copy of voice-studio's BGM, 6.6MB)

## Pipeline daemons (live in voice-studio)

| Trigger | Service | Writes | Reads |
|---|---|---|---|
| `.video-script-trigger` | `voice-studio-video-script-watcher` | `jobs/video/{id}.json` (status=ready_script) | `jobs/video/{id}.json` (status=pending) |
| `.video-render-trigger` | `voice-studio-video-render-watcher` | `jobs/video/{id}.json` (render.mp4_url) | `jobs/video/{id}.json` (status=ready_script) |
| `.video-narrate-trigger` | `voice-studio-video-narrate-watcher` | `jobs/video/{id}.json` (final.mp4_url) | `jobs/video/{id}.json` (status=rendered) |

Each daemon touches the next trigger file on success, creating the cascade.

## Voice registry (shared with voice-studio)

`/root/.openclaw/workspace/skills/voice-studio/scripts/voice_registry.json` holds the single source of truth for TTS voice configs. Video default is `Chinese (Mandarin)_Warm_Girl` (display: 温暖少女, speed 1.0).

## Render performance notes (this VM, June 2026)

- 30s @ 30fps = 900 frames
- Render time: ~5 min via puppeteer+chrome headless (~130ms/frame)
- 60s+ videos need `RENDER_TIMEOUT_SEC=600` in `process_video_render_jobs.py` (already set)
- 90s+ may need bump to 900+ or lower fps

## OSS / R2 naming

Final mp4: `voice-studio/video-studio/video-{slug}-{shortid}-final.mp4` (7-day pre-signed URL).
Rendered-only mp4: same prefix with `-rendered` suffix.

## P1 vs P2 vs P3 status

| Phase | Status | Description |
|---|---|---|
| **P1** | ✅ Done (2026-06-11) | Skeleton: 3 daemons + UI tab + 1 successful e2e demo (placeholder HTML, 30s video) |
| **P2** | 🔄 In progress | Dynamic HTML generated from script text (sentence-balanced chunks); fix duration mismatch; write this SKILL.md |
| **P3** | ⏳ TBD | LLM-generated hyperframes compositions (replaces templated dynamic HTML); review agent; cron integration; multi-aspect-ratio (9:16, 16:9, 1:1); multi-voice UI |

## Acceptance criteria (P2 target)

- [x] Final mp4 has matching video + audio duration (no 20s video + 6s audio mismatch)
- [x] Render daemon uses actual script text (not static "VIDEO / placeholder")
- [x] SKILL.md exists at this path
- [x] Script daemon iteration count bounded (3 max writes, down from 5+)
- [ ] One real demo (60-90s script) renders + narrates with matching duration
- [ ] Regression: voice / music / cover modes still work
