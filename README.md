# video-studio

60–90 秒短视频自动创作。本仓库是 [voice-studio](https://github.com/harryper/voice-studio) 的兄弟项目，承载共享 Web 工作流中 `mode='video'` 这一条线：输入主题 → 旁白脚本 → hyperframes 视频 → MiniMax TTS 配音 + 背景乐 → 最终 mp4。

Web UI、三个流水线守护进程、运行产物都放在本仓库。TTS 调用（MiniMax）和 voice 注册表跟 `voice-studio` 共享——见 [跨 skill 依赖](#跨-skill-依赖)。

## 流水线

三个阶段，每段由一个监听触发文件的 systemd path unit 驱动：

```
                    ┌────────────────────────────────────────────────────┐
                    │  Web UI  (Flask + gunicorn on :9998)               │
                    │  POST /api/jobs  →  创建 v_<id>.json (pending)      │
                    │                 →  触摸 .video-script-trigger      │
                    └────────────────────┬───────────────────────────────┘
                                         ▼
   .video-script-trigger  ──▶  script 守护进程  LLM 写旁白
                                         │  status → ready_script
                                         ▼  触摸 .video-render-trigger
   .video-render-trigger   ──▶  render 守护进程  puppeteer + headless chrome
                                         │  生成 raw.mp4（无音轨）
                                         │  status → rendered
                                         ▼  触摸 .video-narrate-trigger
   .video-narrate-trigger  ──▶  narrate 守护进程  TTS + 强制对齐
                                              + 背景乐混音 + ffmpeg 合成
                                              status → final
```

全自动：没有人审环节。用户只提交主题，三段自动级联。

触发器就是裸的 `touch` 标记文件（项目根目录下的 `.video-{阶段}-trigger`）。Web 应用和守护进程都读写 `jobs/video/v_*.json` 里的 job 状态；触发器只负责唤醒下一段守护进程。

## preview_only 模式

跳过完整渲染（图片抓取 + hyperframes）的快速路径。narrate 守护进程改跑 `scripts/preview_caption_ffmpeg.py`，生成黑底 mp4，叠配音轨和烧入式 ASS 字幕。60s 片段约 3–6s 出片，比 ~5min 的完整渲染快两个数量级。

强制对齐 + 字幕时序逻辑跟完整渲染共用同一套代码，所以 preview 是调试字幕/配音同步的正确入口。

## 强制对齐

TTS 返回的词级时间戳是模型"打算"什么时候说，不是实测。20s 之后漂移会累积，用户就感觉"字幕比声音慢半拍"。`scripts/align_audio_stable_ts.py` 跑 Whisper 的 cross-attention 对齐，对真实音频波形做逐字时间戳，落到 `runs/{job_id}/alignment.json`，schema 跟 TTS 路径完全一致，下游消费者无感。

aligner 用 `。！？!?.` 切句。ASCII 句点 `.` 在切分集里因为它确实能断英文句子（`i.e. 5` → `i.e.` + `5`），但同一个分隔符也会腰斩小数（`前 0.5 秒` → `前 0.` + `5 秒`）。`_merge_decimal_split_sentences` 把"明显是同一段小数的两半"重新粘回去——条件故意收窄，不吞 `i.e. 5` / `Dr. Smith` 这类合法切分。

## 目录布局

```
app.py                          Flask Web 应用（UI + JSON API）
gunicorn.conf.py                2 个 sync worker，60s 超时
Dockerfile / docker-compose.yml 容器化 Web；绑定 :9998
SKILL.md                        项目状态 / 阶段日志（P1/P2/P3）
reference-style-video.md        喂给脚本 LLM 的风格简报
reference-scripts/              风格样例（不会被复制）
scripts/
  process_video_script_jobs.py    script 守护进程（LLM 旁白）
  process_video_render_jobs.py    render 守护进程（puppeteer + chrome）
  process_video_narrate_jobs.py   narrate 守护进程（TTS + 背景乐 + 合成）
  align_audio_stable_ts.py        Whisper 强制对齐
  preview_caption_ffmpeg.py      黑底 preview mp4（快速路径）
  preview_caption_video.py       hyperframes preview（preview_only 不用）
  minimax_tts.py / *_subs.py      TTS 封装（voice-studio 软链接目标）
  pexels_image.py / pexels_video.py  Pexels 素材抓取
  upload_to_oss.py                发布到 R2
  test_align.py                   单测：小数点合并
  test_wrap.py                    单测：字幕折行（CJK/ASCII）
  test_html_output.py             单测：hyperframes HTML
  voice_registry.json             跟 voice-studio 共享
systemd/                        3 个 path unit + 3 个 oneshot service
templates/                      index.html, login.html, video_placeholder.html
jobs/video/                     活跃 job JSON（一个 v_*.json 一条）
runs/{job_id}/                  每个 job 的产物：
  script.txt                    LLM 写的旁白
  alignment.json                逐字 + 逐句 TTS 时序
  composition/index.html        hyperframes 合成（P2+）
  video/raw.mp4                 渲染出来的视频（无音轨）
  audio/voice.mp3               TTS 配音
  audio/mixed.mp3               配音 + 背景乐
  final.mp4                     视频 + 音频合成
  preview-{N}s.mp4              preview_only 输出（N = duration_sec）
```

## 跨 skill 依赖

`scripts/minimax_tts.py`、`minimax_tts_subs.py`、`voice_registry.json` 都按绝对路径从 `voice-studio` 读，不通过 import。systemd 的 `Environment=PATH` 把 `voice-studio/scripts/` 加进去，子进程能解析。默认音色是 `Chinese (Mandarin)_Radio_Host`（显示名：电台男主播，speed 1.0）。

`scripts/minimax_api_key.txt` 和 `pexels_api_key.txt` 持有密钥，被 `.gitignore` 排除。

## 本地跑

Web 容器只跑 API + UI。守护进程在宿主机上由 systemd 跑，job 才能真正推进。

```bash
# Web
pip install -r requirements.txt
gunicorn -c gunicorn.conf.py app:app      # :9998

# 守护进程（宿主机，需要 voice-studio 在 PATH 里）
sudo cp systemd/*.service systemd/*.path /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
  video-studio-script-watcher.path \
  video-studio-render-watcher.path \
  video-studio-narrate-watcher.path
```

健康检查：`curl http://127.0.0.1:9998/api/health` 应返回 `{"ok": true}`。

需要的环境变量：`APP_PASSWORD`（登录）、`APP_COOKIE_SECRET`（cookie HMAC）、`VOICE_STUDIO_DIR`（跨 skill 路径）、`TZ=Asia/Shanghai`（跟宿主机时钟对齐）。

## 测试

```bash
python3 scripts/test_align.py     # 小数点合并：9/9
python3 scripts/test_wrap.py      # 字幕折行：  14/14
python3 scripts/test_html_output.py
```

测试没有外部依赖，全部加起来 < 1s。改完 `scripts/align_audio_stable_ts.py`、`scripts/process_video_render_jobs.py` 里的折行函数、或者 `templates/index.html` 之后跑一下。

## 已知问题 / 已记未修

- `_load_alignment_subtimes` 里有一处 "Re-clamp last sub to scene_end"，实际上是无条件把最后一个 sub 延伸到 `scene_end`（变量名说 clamp，代码做 fill）。目前在 preview 路径上被 `clip_subs()` 按 `args.duration` 截掉，没爆出来，但这是潜在 bug。
- 同一个函数的 `contained_idx` 过滤要求 `b <= scene_end+0.05`，所以跟短 preview 末尾重叠的句子会被丢掉。`preview_caption_ffmpeg.py` 已经在本地用 `voice_seconds` 代替 `scene_end` 绕过；完整渲染路径上同样的问题没修。
- render 守护进程：60s+ 视频需要 `RENDER_TIMEOUT_SEC=600`（已经设了）；90s+ 可能还得再调或降 fps。
