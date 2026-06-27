---
name: video-studio
description: "Use when working on voice-studio Web 项目中 mode='video' 任务（60–90s 短视频自动创作，支持 16:9 / 9:16 / 1:1 多画幅，默认 16:9 1920×1080）。场景：用户在 voice-studio Web UI 的 🎬 视频 tab 提交主题、或在 chat 里说要重跑某个 v_<id>.job 的脚本/渲染/配音、或要排查字幕时序漂移/封面 splash/强制对齐/v9 字幕切分等。不要在 chat 里手写脚本、跑 TTS、或绕过 Web job 工作流直接产 mp4。"
---

# video-studio skill

技术 id：`video-studio`。面向用户的名字：**video-studio skill**。

本 skill 是 **voice-studio 的兄弟**，不是独立产品。它承载 voice-studio Web 项目中 `mode='video'` 这一条线的**共享风格素材 + 跑批产物**。Web 应用、三个流水线守护进程、UI 都住在 `skills/voice-studio/`，本文档说明视频线独有的部分以及跟 voice-studio 的边界。

## 唯一规范流程

所有视频工作都走 voice-studio Web 项目，🎬 视频 tab。

`mode='video'` job 的状态机：

```
pending ──▶ ready_script ──▶ rendered ──▶ final
   │              │              │
   │ script 守护进程 │ render 守护进程│ narrate 守护进程
   │ LLM 写旁白     │ puppeteer 出 mp4│ TTS+对齐+混音+合成
```

**Auto-pilot**：零人工审。用户提交主题后三段自动级联，触发器走裸的 `touch` 标记文件（`.video-{script,render,narrate}-trigger`）。Web 应用和守护进程都读写 `jobs/video/v_*.json` 里的 job 状态，触发器只负责唤醒下一段守护进程。

主 chat 会话只通过 Web job 工作流编排。不要在 chat 里直接起草脚本、跑 TTS、合成 mp4 来交付视频任务——那些工作归守护进程。

## 守护进程与触发器（宿主机 systemd）

| 触发文件 | path unit | service | 写 | 读 |
|---|---|---|---|---|
| `.video-script-trigger` | `video-studio-script-watcher.path` | `video-studio-script-watcher.service` | `jobs/video/{id}.json` (status=ready_script) | `jobs/video/{id}.json` (status=pending) |
| `.video-render-trigger` | `video-studio-render-watcher.path` | `video-studio-render-watcher.service` | `jobs/video/{id}.json` (render.mp4_url) | `jobs/video/{id}.json` (status=ready_script) |
| `.video-narrate-trigger` | `video-studio-narrate-watcher.path` | `video-studio-narrate-watcher.service` | `jobs/video/{id}.json` (final.mp4_url) | `jobs/video/{id}.json` (status=rendered) |

每个守护进程成功后会 `touch` 下一个触发文件，级联下一段。守护进程只拣对应 status 的 job，error 状态的 job 必须先被 Web 重跑按钮 reset 回 `pending`/`ready_script`/`rendered` 才会被拾起。

Gunicorn worker 是 fork 模式，添加新端点后必须 `pkill -HUP gunicorn` 才会加载新代码。

## 风格素材

- `reference-style-video.md` — 视频旁白风格规范（150s 短视频节奏、信息密度高、5s 内反常识、10–15s 一卡）。script 守护进程的 LLM prompt 必读这份。
- `reference-scripts/01-ai-terms.md` — 风格样例（"AI 名词其实就 5 个"，215 字，7 卡节奏）。script 守护进程**不复制**这份，只用作风格锚点。

## 跑批产物（runs/{job_id}/）

| 文件 | 说明 |
|---|---|
| `script.txt` | LLM 写的旁白 |
| `cover.json` | 封面 splash 数据（main + main_highlight + sub） |
| `keywords.json` | 场景-关键词映射（素材检索用） |
| `alignment.json` | 强制对齐落盘的逐字 + 逐句 TTS 时序（stable-ts） |
| `composition/index.html` | hyperframes 动态合成 |
| `composition/video-only.mp4` | 渲染出来的视频（无音轨） |
| `composition/chunks.json` | chunk 切分中间产物 |
| `composition/images/`、`composition/videos/` | Pixabay/Pexels 抓回的素材 |
| `video/raw.mp4` | 跟 `composition/video-only.mp4` 同步 |
| `audio/voice.mp3` | TTS 配音 |
| `audio/voice.subtitle.json` | TTS 词级时间戳（仅 alignment_engine=tts 时落盘） |
| `audio/mixed.mp3` | 配音 + 背景乐混音（含封面 0.8s 静音前置） |
| `audio/script.txt` | 喂给 TTS 的脚本副本 |
| `final.mp4` | 视频 + 音频合成 |
| `preview-{N}s.mp4` | preview_only 模式输出 |

## 封面 splash（cover splash）

视频最前 `COVER_DURATION_SEC = 0.8`s（`scripts/process_video_render_jobs.py:55`）是脚本 LLM 写的"钩子画面"：4–6 字主标（反常识/数字冲击），主标里 1–2 字黄字钩眼，副标留悬念不剧透。

数据流：

```
script 守护进程 (parse_cover_validation)
  → 校验：hl 不在首/末字、不全段、不问号结尾、sub 不含"因为...所以..."/"真相是"
  → 失败 → cover_fallback 用 _COVER_HOOK_MARKERS 从正文捞反常识句兜底
  → 落盘 runs/{job_id}/cover.json
  → job.script_meta.cover 同步进 job JSON
render 守护进程
  → 读 cover.json → render_cover_layout 出 HTML
  → 视频最前 0.8s 渲染封面 scene
  → 后续内容场景的 scene_times / subtimes 整体后移 COVER_DURATION_SEC
narrate 守护进程
  → 检测 cover.json → merge_video_audio(audio_delay_sec=0.8)
  → ffmpeg filter chain 前置 adelay=800|800，首个配音字从 t=0.8+0.32=1.12s 响
  → 与 sub-2-1 fade-in (TTS[0] + COVER) 时刻对齐
```

首场景（封面转场后）只展示字幕 + 干净配图，不叠 hook 文字（v3.2 删除 `hook_html` 生成块 + opening-hook tweens，避免跟首句字幕重复）。

设计约束（v3.1 后 settled）：

- **hl 必须是钩眼词**：含数字 OR 在 `_COVER_HOOK_MARKERS` 否定/转折词集合 OR 在 `_HOOK_SUBSTR` 子串集合里。LLM 写 `[2,4]="是调"` 这种"两个连续非钩眼字"会被校验拒掉，触发 fallback 兜底。
- **sub 严禁剧透**：不能含"因为/所以/其实/真相是/直接说/本质是"。
- **封面时长 < 1s**：跟用户预期一致，不要做成 splash 转场动画。

回归测试：`scripts/test_cover_layout.py`（25/25），覆盖 layout 渲染 + 高亮 OOB 边界 + fallback 钩眼词选择 + parse_cover_validation 硬规则 + 首场景 `starts[0]=COVER_DURATION_SEC` + audio delay filter chain 形状。

## 强制对齐（forced alignment）

TTS 返回的词级时间戳是模型"打算"什么时候说，不是实测。20s 之后漂移会累积，用户就感觉"字幕比声音慢半拍"。`scripts/align_audio_stable_ts.py` 跑 Whisper 的 cross-attention 对齐，对真实音频波形做逐字时间戳，落到 `runs/{job_id}/alignment.json`，schema 跟 TTS 路径完全一致，下游消费者无感。

aligner 用 `。！？!?.` 切句。ASCII 句点 `.` 在切分集里因为它确实能断英文句子（`i.e. 5` → `i.e.` + `5`），但同一个分隔符也会腰斩小数（`前 0.5 秒` → `前 0.` + `5 秒`）。`_merge_decimal_split_sentences` 把"明显是同一段小数的两半"重新粘回去——条件故意收窄，不吞 `i.e. 5` / `Dr. Smith` 这类合法切分。

回归测试：`scripts/test_align.py`（9/9）。

## 字幕切分（v9 settled design）

`_split_sentence_into_subs` 在 `scripts/process_video_render_jobs.py`：**每个 `_SPLIT_PUNCT` 字符（`。！？，；：、,?!` 等）切一个 sub**，不贪心填满到 20 字。每个 PUNCT-boundary clause 各自成为一个 sub-caption，由 `wrap_caption_lines` 单行渲染（必要时 ≤ 2 行）。> 20 字且内部无 PUNCT 的 clause 兜底走 `_split_long_clause`（v7-v8.1 候选扫描 + hard-cut 逻辑）。

为什么这样切：每 clause 一行 → 字幕节拍更碎、跟读更轻。Gold standard（用户认定的 7-sub 触发句）：

| # | sub | 字数 |
|---|---|---|
| 0 | 一个能秒掉整个朝代的神仙 | 12 |
| 1 | 忍了 | 2 |
| 2 | 这一忍就是整整28年 | 10 |
| 3 | 中间隔了2次封神 | 8 |
| 4 | 3次朝堂清洗 | 6 |
| 5 | 5次人间王朝更替 | 8 |
| 6 | 你就知道这克制有多深 | 10 |

节奏目标：5–8 subs / 10–15s scene，每个 ~1.5s（≈ 10 字 @ TTS speed=1.15）。

回归测试：`scripts/test_wrap.py`（20/20，含 `test_v9_strict_punct_split` 精确匹配 7 sub）。

**脚本创作约束**：clause 之间必须用 ASCII `,` / 全角 `、` 隔开（不是逗号连续的 run-on 长句），每个 clause 2–12 字理想。避免单 clause > 20 字（会触发 `_split_long_clause` 兜底，节奏乱）。这条约束已经记入 memory（`feedback_subtitle_strict_punct_v9.md`），新脚本创作和配音都按这个走。

## 脚本长度

`scripts/process_video_script_jobs.py`：

- `MIN_SCRIPT_CHARS = 300`
- `MAX_SCRIPT_CHARS = 1200`

短文（300–449 字，比如 30–60s 抖音小知识）和长文（450–1200 字，200s 抖音科普对标大约 1080 字）都接受。Style guide target 是 560–640 字，下限 300 是为了不卡死短文下限——LLM 输出噪声大。

脚本长度校验是 **duration-aware**：长视频（duration > 100s）按 `ESTIMATED_CHARS_PER_SECOND * duration * 1.3 + 100` 动态放大 max，避免 300s 视频 1515 字被误拒。回归测试：`scripts/test_script_length_bounds.py`（6/6）。

## preview_only 模式

跳过完整渲染（图片抓取 + hyperframes）的快速路径。narrate 守护进程改跑 `scripts/preview_caption_ffmpeg.py`，生成黑底 mp4，叠配音轨和烧入式 ASS 字幕。60s 片段约 3–6s 出片，比 ~5min 的完整渲染快两个数量级。

强制对齐 + 字幕时序逻辑跟完整渲染共用同一套代码，所以 preview 是调试字幕/配音同步的正确入口。

## Web 重跑入口

详情面板顶部三个按钮共用 `rerunWithFeedback()` helper：请求中禁用 + 显示 `⏳ 已触发` / `✓ 已触发` / `✗ 失败`（防连点），1.5s 后恢复原文字。

- **重跑脚本** → `POST /api/jobs/<id>/script`：status 重置 `pending` + 清 `error` + touch `.video-script-trigger`。
- **重跑渲染** → `POST /api/jobs/<id>/render`：status → `ready_script` + touch `.video-render-trigger`。
- **重跑配音** → `POST /api/jobs/<id>/narrate`：status → `rendered` + touch `.video-narrate-trigger`。

## 音色注册（跟 voice-studio 共享）

`/root/.openclaw/workspace/skills/voice-studio/scripts/voice_registry.json` 是 TTS 音色配置的单一来源。视频线默认音色：

- **id**：`Chinese (Mandarin)_Kind-hearted_Antie`
- **显示名**：热心大婶
- **speed**：1.15（app.py 创建 job 时硬编码；registry default_speed 仍为 1.0）

`app.py:305` 新建 job 时写这个默认；`process_video_narrate_jobs.py:592` 的 fallback 也同步成同一个 id。旧版本曾用 `Radio_Host` / `azure_yunze_clone` / `Warm_Girl`（`a0b8274` 切到 Warm_Girl，本次切到 Kind-hearted_Antie）。`jobs/video/` 里创建时间早于本次切换的 job JSON 仍显式持有旧 voice，需要重新提交才会用新默认。

可用音色清单见 `scripts/voice_registry.json`（`Radio_Host` / `Warm_Girl` / `Kind-hearted_Antie` / `azure_yunze_clone`）。

## 渲染性能（本 VM，2026-06）

- 30s @ 30fps = 900 frames
- 单帧 ~130ms（puppeteer + headless chrome）
- 30s 视频约 5 分钟
- `RENDER_TIMEOUT_SEC = 3600`（`scripts/process_video_render_jobs.py:102`，1 小时上限，足够覆盖 90s+ 视频）

## OSS / R2 命名

- 最终 mp4：`voice-studio/video-studio/video-{slug}-{shortid}-final.mp4`（7-day pre-signed URL）
- 仅渲染的 mp4：同上，加 `-rendered` 后缀
- 翻唱 cover（mode=music_cover）：`--theme cover`

## 跨 skill 依赖

- `scripts/minimax_tts.py`、`minimax_tts_subs.py` 都按绝对路径从 `voice-studio` 读；`scripts/voice_registry.json` 是 video-studio 本地副本（含视频线专属的 Warm_Girl / Kind-hearted_Antie 条目），跟 voice-studio 的 registry 不同步——narrate 守护进程读本地副本
- systemd 的 `Environment=PATH` 把 `voice-studio/scripts/` 加进去，子进程能解析
- 默认音色变更历史：`Radio_Host` → `azure_yunze_clone` → `Warm_Girl`（`a0b8274`）→ `Kind-hearted_Antie`（本次切换）
- 密钥：`scripts/minimax_api_key.txt`、`pexels_api_key.txt`、`pixabay_api_key.txt`（`.gitignore` 排除）

## 目录布局

```
app.py                          Flask Web 应用（UI + JSON API，:9998）
gunicorn.conf.py                2 个 sync worker，60s 超时
Dockerfile / docker-compose.yml 容器化 Web，绑定 :9998
SKILL.md                        项目状态 / 阶段日志
reference-style-video.md        喂给脚本 LLM 的风格简报
reference-scripts/              风格样例（不会被复制）
scripts/
  process_video_script_jobs.py    script 守护进程（LLM 旁白 + cover.json 校验）
  process_video_render_jobs.py    render 守护进程（puppeteer + chrome + cover splash）
  process_video_narrate_jobs.py   narrate 守护进程（TTS + 背景乐 + audio delay + 合成）
  align_audio_stable_ts.py        Whisper 强制对齐
  preview_caption_ffmpeg.py       黑底 preview mp4（快速路径）
  preview_caption_video.py        hyperframes preview（preview_only 不用）
  minimax_tts.py / *_subs.py      TTS 封装（voice-studio 共享）
  pixabay_image.py / pixabay_video.py / pixabay_cache.py  Pixabay 素材抓取+缓存（主源）
  pexels_image.py / pexels_video.py  Pexels 旧接口（保留兼容，部分 job 还在用）
  extract_scene_keywords.py       关键词抽取（场景-关键词映射）
  upload_to_oss.py                发布到 R2
  test_align.py                   单测：小数点合并（9/9）
  test_wrap.py                    单测：字幕折行 + v9 split（20/20）
  test_html_output.py             smoke：hyperframes HTML 子字幕结构
  test_cover_layout.py            单测：封面 layout + 校验规则 + audio delay（25/25）
  test_alignment_subtimes.py      单测：_load_alignment_subtimes（4/4）
  test_alignment_scene_times.py   单测：alignment → scene_times（3/3）
  test_kinetic_overlay.py         单测：kinetic overlay 时序（9/9）
  test_visual_specs.py            单测：visual specs 渲染规范（18/18）
  test_pixabay_cache.py           单测：pixabay 缓存命中（15/15）
  test_script_length_bounds.py    单测：脚本长度 MIN/MAX 校验（6/6）
  test_script_repair.py           单测：脚本修复启发式（5/5）
  test_skip_pexels.py             单测：pexels skip 决策（7/7）
  voice_registry.json             跟 voice-studio 共享
systemd/                        3 个 path unit + 3 个 oneshot service
templates/                      index.html, login.html, video_placeholder.html
jobs/video/                     活跃 job JSON（一个 v_*.json 一条）
runs/{job_id}/                  每个 job 的产物（见上文"跑批产物"）
```

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
python3 scripts/test_align.py                 # 小数点合并：              9/9
python3 scripts/test_wrap.py                  # 字幕折行 + v9 split：    20/20
python3 scripts/test_cover_layout.py          # 封面 layout + 校验 + audio delay：25/25
python3 scripts/test_alignment_subtimes.py    # _load_alignment_subtimes：4/4
python3 scripts/test_alignment_scene_times.py # alignment → scene_times：3/3
python3 scripts/test_kinetic_overlay.py       # kinetic overlay 时序：   9/9
python3 scripts/test_visual_specs.py          # visual specs 渲染规范：  18/18
python3 scripts/test_pixabay_cache.py         # pixabay 缓存命中：       15/15
python3 scripts/test_script_length_bounds.py  # 脚本长度 MIN/MAX 校验：  6/6
python3 scripts/test_script_repair.py         # 脚本修复启发式：         5/5
python3 scripts/test_skip_pexels.py           # pexels skip 决策：       7/7
python3 scripts/test_html_output.py           # smoke：hyperframes HTML 子字幕结构
```

测试没有外部依赖，全部加起来 < 1s。改完 `scripts/align_audio_stable_ts.py`、`scripts/process_video_render_jobs.py` 里的折行函数、`_load_alignment_subtimes`、封面 layout / 校验 / fallback、或者 `templates/index.html` 之后跑一下。

## 阶段状态

| 阶段 | 状态 | 说明 |
|---|---|---|
| **P1** | ✅ 完成（2026-06-11） | 骨架：3 守护进程 + UI tab + 1 个成功 e2e demo（占位 HTML，30s 视频） |
| **P2** | ✅ 完成（2026-06-27） | 动态 HTML（按脚本生成）+ 时长匹配修复 + cover splash layout + v9 字幕切分 + duration-aware 脚本长度 + 16:9 max_chars 调整 + Kind-hearted_Antie 默认音色 + Pixabay 主源 + cascade 立即 touch |
| **P3** | ⏳ 待定 | LLM 直接生成 hyperframes composition（取代模板化动态 HTML）；review agent；cron 接入；多画幅 9:16 / 1:1；多音色 UI |