# video-studio

60–90s auto-produced short videos. A sibling of [voice-studio](https://github.com/harryper/voice-studio) that runs the `mode='video'` track of the shared Web workflow: topic in → narration script → hyperframes mp4 → MiniMax TTS voice + BGM → final mp4.

The web UI, the three pipeline daemons, and the run artifacts all live in this repo. TTS calls (MiniMax) and the voice registry are shared with `voice-studio` — see [Cross-skill dependency](#cross-skill-dependency).

## Pipeline

Three stages, each driven by a systemd path unit that watches a trigger file:

```
                    ┌────────────────────────────────────────────────────┐
                    │  Web UI  (Flask + gunicorn on :9998)                │
                    │  POST /api/jobs  →  creates v_<id>.json (pending)   │
                    │                 →  touches .video-script-trigger    │
                    └────────────────────┬───────────────────────────────┘
                                         ▼
   .video-script-trigger  ──▶  script daemon   LLM writes narration
                                         │  status → ready_script
                                         ▼   touches .video-render-trigger
   .video-render-trigger   ──▶  render daemon   puppeteer + headless chrome
                                         │  produces raw.mp4 (no audio)
                                         │  status → rendered
                                         ▼   touches .video-narrate-trigger
   .video-narrate-trigger  ──▶  narrate daemon  TTS + forced alignment
                                              + BGM mix + ffmpeg merge
                                              status → final
```

Auto-pilot: no human review gates. The user submits a topic, the three stages cascade.

Trigger files are bare-metal `touch` markers (`.video-{stage}-trigger` in the project root). The web app and daemons all read/write job state in `jobs/video/v_*.json`; the trigger file just wakes the next daemon.

## preview_only mode

A fast path that skips the full render (image fetch + hyperframes). The narrate daemon runs `scripts/preview_caption_ffmpeg.py` to produce a black-background mp4 with the voice track and burned-in ASS subtitles. ~3–6s for a 60s clip instead of ~5min.

The forced-alignment + sub-caption timing logic is shared with the full render path, so preview is the right place to iterate on subtitle/voice sync.

## Forced alignment

TTS-returned word timestamps are *predictions* of when the model plans to speak, not measurements. After ~20s the drift compounds and users perceive "subs lag behind voice". `scripts/align_audio_stable_ts.py` runs Whisper's cross-attention alignment against the actual audio waveform to produce per-character timestamps, written to `runs/{job_id}/alignment.json` with the same schema as the TTS-driven path so downstream consumers don't care which one ran.

The aligner splits the script on `。！？!?.`. The ASCII period is in that set because it correctly terminates English sentences (`i.e. 5` → `i.e.` + `5`) but it also severs decimal numbers (`前 0.5 秒` → `前 0.` + `5 秒`). `_merge_decimal_split_sentences` re-glues pairs that are obviously two halves of the same decimal number — narrow condition so legitimate English splits are preserved.

## Layout

```
app.py                          Flask web app (UI + JSON API)
gunicorn.conf.py                2 sync workers, 60s timeout
Dockerfile / docker-compose.yml Containerized web; binds :9998
SKILL.md                        Project status & phase log (P1/P2/P3)
reference-style-video.md        Style brief fed to the script LLM
reference-scripts/              Stylistic templates (not copied)
scripts/
  process_video_script_jobs.py    script daemon (LLM narration)
  process_video_render_jobs.py    render daemon (puppeteer + chrome)
  process_video_narrate_jobs.py   narrate daemon (TTS + BGM + merge)
  align_audio_stable_ts.py        Whisper forced-alignment
  preview_caption_ffmpeg.py      black-bg preview mp4 (fast)
  preview_caption_video.py       hyperframes preview (unused in preview_only)
  minimax_tts.py / *_subs.py      TTS wrapper (cross-skill symlink target)
  pexels_image.py / pexels_video.py  Pexels stock photo/video fetch
  upload_to_oss.py                publish to R2
  test_align.py                   unit tests: decimal merge
  test_wrap.py                    unit tests: caption wrap (CJK/ASCII)
  test_html_output.py             unit tests: hyperframes HTML
  voice_registry.json             shared with voice-studio
systemd/                        3 path units + 3 oneshot services
templates/                      index.html, login.html, video_placeholder.html
jobs/video/                     active job JSON (one per v_*.json)
runs/{job_id}/                  per-job artifacts:
  script.txt                    LLM-written narration
  alignment.json                per-char + per-sentence TTS timing
  composition/index.html        hyperframes composition (P2+)
  video/raw.mp4                 rendered video (no audio)
  audio/voice.mp3               TTS voice
  audio/mixed.mp3               voice + BGM
  final.mp4                     video + audio muxed
  preview-{N}s.mp4              preview_only output (N = duration_sec)
```

## Cross-skill dependency

`scripts/minimax_tts.py`, `minimax_tts_subs.py`, and `voice_registry.json` are read from `voice-studio` by absolute path, never imported. The systemd `Environment=PATH` includes `voice-studio/scripts/` so subprocess calls resolve. Default voice is `Chinese (Mandarin)_Radio_Host` (display: 电台男主播, speed 1.0).

`scripts/minimax_api_key.txt` and `pexels_api_key.txt` hold credentials and are gitignored.

## Run locally

The web container is just the API + UI. Daemons run on the host via systemd and are required for jobs to actually progress.

```bash
# Web
pip install -r requirements.txt
gunicorn -c gunicorn.conf.py app:app      # :9998

# Daemons (host-side, requires voice-studio on PATH)
sudo cp systemd/*.service systemd/*.path /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
  video-studio-script-watcher.path \
  video-studio-render-watcher.path \
  video-studio-narrate-watcher.path
```

Health check: `curl http://127.0.0.1:9998/api/health` should return `{"ok": true}`.

Required env vars: `APP_PASSWORD` (login), `APP_COOKIE_SECRET` (cookie HMAC), `VOICE_STUDIO_DIR` (cross-skill path), `TZ=Asia/Shanghai` (host wall clock).

## Tests

```bash
python3 scripts/test_align.py     # decimal-period merge: 9/9
python3 scripts/test_wrap.py      # caption wrap:        14/14
python3 scripts/test_html_output.py
```

Tests have no external dependencies and run in <1s total. Run them after touching `scripts/align_audio_stable_ts.py`, `scripts/process_video_render_jobs.py` wrap functions, or `templates/index.html`.

## Known issues / deferred

- `_load_alignment_subtimes` has a "Re-clamp last sub to scene_end" branch that unconditionally extends the last sub to `scene_end` (variable name says clamp, code says fill). Currently masked by the preview path's `clip_subs()` clipping to `args.duration`, but it's a latent bug.
- The same function's `contained_idx` filter requires `b <= scene_end+0.05`, so sentences overlapping the end of a short preview are dropped. Fixed locally in `preview_caption_ffmpeg.py` by passing `voice_seconds` as `scene_end`; the full-render path has the same issue.
- Render daemon: 60s+ videos need `RENDER_TIMEOUT_SEC=600` (already set); 90s+ may need a further bump or lower fps.
