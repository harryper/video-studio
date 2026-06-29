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

## 封面 (cover splash)

封面占视频最前 `COVER_DURATION_SEC = 0.8`s（`scripts/process_video_render_jobs.py:55`），是脚本 LLM 写的"钩子画面"：4–6 字主标（反常识/数字冲击），主标里 1–2 字黄字钩眼，副标留悬念不剧透。

数据流：

```
script 守护进程 (parse_cover_validation)
  → 校验: hl 不在首/末字、不全段、不问号结尾、sub 不含"因为...所以..."/"真相是"
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
- **hl 必须是钩眼词**：含数字 OR 在 `_COVER_HOOK_MARKERS` 否定/转折词集合 OR 在 `_HOOK_SUBSTR` 子串集合里。LLM 写 [2,4]="是调" 这种"两个连续非钩眼字"会被校验拒掉，触发 fallback 兜底
- **sub 严禁剧透**：不能含"因为/所以/其实/真相是/直接说/本质是"
- **封面时长 < 1s**：跟用户预期一致，不要做成 splash 转场动画

回归测试：`scripts/test_cover_layout.py`（25/25）覆盖 layout 渲染 + 高亮 OOB 边界 + fallback 钩眼词选择 + parse_cover_validation 硬规则 + 首场景 starts[0]=COVER_DURATION_SEC + audio delay filter chain 形状。

## 强制对齐

TTS 返回的词级时间戳是模型"打算"什么时候说，不是实测。20s 之后漂移会累积，用户就感觉"字幕比声音慢半拍"。`scripts/align_audio_stable_ts.py` 跑 Whisper 的 cross-attention 对齐，对真实音频波形做逐字时间戳，落到 `runs/{job_id}/alignment.json`，schema 跟 TTS 路径完全一致，下游消费者无感。

aligner 用 `。！？!?.` 切句。ASCII 句点 `.` 在切分集里因为它确实能断英文句子（`i.e. 5` → `i.e.` + `5`），但同一个分隔符也会腰斩小数（`前 0.5 秒` → `前 0.` + `5 秒`）。`_merge_decimal_split_sentences` 把"明显是同一段小数的两半"重新粘回去——条件故意收窄，不吞 `i.e. 5` / `Dr. Smith` 这类合法切分。

## 字幕切分（v9 settled design）

`_split_sentence_into_subs` 在 `scripts/process_video_render_jobs.py` L743-866：**每个 `_SPLIT_PUNCT` 字符（`,` `、` `。` `!` `?` 等）切一个 sub**，不贪心填满到 20 字。每个 PUNCT-boundary clause 各自成为一个 sub-caption，由 `wrap_caption_lines` 单行渲染（必要时 ≤ 2 行）。> 20 字且内部无 PUNCT 的 clause 兜底走 `_split_long_clause`（v7-v8.1 候选扫描 + hard-cut 逻辑）。

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

节奏目标：5-8 subs / 10-15s scene，每个 ~1.5s（≈ 10 字 @ TTS speed=1.15）。回归测试：`scripts/test_wrap.py::test_v9_strict_punct_split`（精确匹配 7 sub，27/27 pass）。

**脚本创作约束**：clause 之间必须用 ASCII `,` / 全角 `、` 隔开（不是逗号连续的 run-on 长句），每个 clause 2-12 字理想。避免单 clause > 20 字（会触发 `_split_long_clause` 兜底，节奏乱）。这条约束已经记入 memory（`feedback_subtitle_strict_punct_v9.md`），新脚本创作和配音都按这个走。

## 脚本长度

`scripts/process_video_script_jobs.py`：`MIN_SCRIPT_CHARS = 300`，`MAX_SCRIPT_CHARS = 1200`。短文（300-449 字，比如 30-60s 抖音小知识）和长文（450-1200 字，200s 抖音科普对标大约 1080 字）都接受。Style guide target 是 560-640 字，下限 300 是为了不卡死短文下限——LLM 输出噪声大。

## Web 重跑入口

详情面板顶部三个按钮：

- **重跑脚本** → `POST /api/jobs/<id>/script`：status 重置 `pending` + 清 `error` + touch `.video-script-trigger`。守护进程只拣 `pending` 状态的 job，所以 error 状态的 job 必须先 reset。
- **重跑渲染** → `POST /api/jobs/<id>/render`：status → `ready_script` + touch `.video-render-trigger`。
- **重跑配音** → `POST /api/jobs/<id>/narrate`：status → `rendered` + touch `.video-narrate-trigger`。

三个按钮共用 `rerunWithFeedback()` helper：请求中禁用 + 显示 `⏳ 已触发` / `✓ 已触发` / `✗ 失败`（防连点），1.5s 后恢复原文字。Gunicorn worker 是 fork 模式，加新端点后必须 `pkill -HUP gunicorn` 才会加载新代码。

## 目录布局

```
app.py                          Flask Web 应用（UI + JSON API）
gunicorn.conf.py                2 个 sync worker，60s 超时
Dockerfile / docker-compose.yml 容器化 Web；绑定 :9998
SKILL.md                        项目状态 / 阶段日志（P1/P2/P3）
reference-style-video.md        喂给脚本 LLM 的风格简报
reference-scripts/              风格样例（不会被复制）
scripts/
  process_video_script_jobs.py    script 守护进程（LLM 旁白 + cover.json 校验)
  process_video_render_jobs.py    render 守护进程（puppeteer + chrome + cover splash）
  process_video_narrate_jobs.py   narrate 守护进程（TTS + 背景乐 + audio delay + 合成)
  align_audio_stable_ts.py        Whisper 强制对齐
  preview_caption_ffmpeg.py      黑底 preview mp4（快速路径）
  preview_caption_video.py       hyperframes preview（preview_only 不用）
  minimax_tts.py / *_subs.py      TTS 封装（voice-studio 共享）
  pixabay_image.py / pixabay_video.py / pixabay_cache.py  Pixabay 素材抓取+缓存（已替代 Pexels）
  pexels_image.py / pexels_video.py  Pexels 旧接口（保留兼容，部分 job 还在用）
  extract_scene_keywords.py      关键词抽取（场景-关键词映射）
  upload_to_oss.py                发布到 R2
  test_align.py                   单测：小数点合并
  test_wrap.py                    单测：字幕折行（CJK/ASCII + v9 split）
  test_html_output.py             smoke：hyperframes HTML 子字幕结构
  test_cover_layout.py            单测：封面 layout + 校验规则 + audio delay
  test_alignment_subtimes.py      单测：_load_alignment_subtimes
  test_alignment_scene_times.py   单测：alignment → scene_times
  test_kinetic_overlay.py         单测：kinetic overlay 时序
  test_visual_specs.py            单测：visual specs 渲染规范
  test_pixabay_cache.py           单测：pixabay 缓存命中
  test_script_length_bounds.py    单测：脚本长度 MIN/MAX 校验
  test_script_repair.py           单测：脚本修复启发式
  test_skip_pexels.py             单测：pexels skip 决策
  voice_registry.json             跟 voice-studio 共享
systemd/                        3 个 path unit + 3 个 oneshot service
templates/                      index.html, login.html, video_placeholder.html
jobs/video/                     活跃 job JSON（一个 v_*.json 一条）
runs/{job_id}/                  每个 job 的产物：
  script.txt                    LLM 写的旁白
  alignment.json                逐字 + 逐句 TTS 时序（forced alignment 落盘）
  cover.json                    封面 splash 数据（main + main_highlight + sub）
  keywords.json                 场景-关键词映射（用于素材检索）
  composition/index.html        hyperframes 合成
  composition/video-only.mp4    渲染出来的视频（无音轨）
  composition/chunks.json       chunk 切分中间产物
  composition/images/           Pexels/Pixabay 抓回的图片素材
  composition/videos/           Pexels/Pixabay 抓回的视频素材
  video/raw.mp4                 最终视频（无音轨）— 跟 composition/video-only.mp4 同步
  audio/voice.mp3               TTS 配音
  audio/voice.subtitle.json     TTS 词级时间戳（仅 alignment_engine=tts 时落盘）
  audio/mixed.mp3               配音 + 背景乐（含封面 0.8s 静音前置）
  final.mp4                     视频 + 音频合成
  preview-{N}s.mp4              preview_only 输出（N = duration_sec）
```

## 跨 skill 依赖

`scripts/minimax_tts.py`、`minimax_tts_subs.py`、`voice_registry.json` 都按绝对路径从 `voice-studio` 读，不通过 import。systemd 的 `Environment=PATH` 把 `voice-studio/scripts/` 加进去，子进程能解析。

默认音色（app.py 新建 job 时写）：`Chinese_casual_instructor_nv1`（显示名：活力讲师，speed 1.15）。 旧版本曾用 `Radio_Host` / `azure_yunze_clone` / `Warm_Girl` / `Kind-hearted_Antie`，已在 a0b8274 切到 Warm_Girl 后再次切换。jobs/video/ 里创建时间早于 a0b8274 的 job JSON 里仍显式持有旧 voice，需要重新提交才会用当前默认。

`scripts/minimax_api_key.txt`、`pexels_api_key.txt`、`pixabay_api_key.txt` 持有密钥，被 `.gitignore` 排除。

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
python3 scripts/test_align.py                # 小数点合并：              9/9
python3 scripts/test_wrap.py                 # 字幕折行 + v9 split：     20/20
python3 scripts/test_cover_layout.py         # 封面 layout + 校验 + audio delay： 25/25
python3 scripts/test_alignment_subtimes.py   # _load_alignment_subtimes： 3/3
python3 scripts/test_alignment_scene_times.py # alignment → scene_times： 3/3
python3 scripts/test_kinetic_overlay.py      # kinetic overlay 时序：    9/9
python3 scripts/test_visual_specs.py         # visual specs 渲染规范：   18/18
python3 scripts/test_pixabay_cache.py        # pixabay 缓存命中：        15/15
python3 scripts/test_script_length_bounds.py # 脚本长度 MIN/MAX 校验：   6/6
python3 scripts/test_script_repair.py        # 脚本修复启发式：         5/5
python3 scripts/test_skip_pexels.py          # pexels skip 决策：        7/7
python3 scripts/test_html_output.py          # smoke：hyperframes HTML 子字幕结构
```

测试没有外部依赖，全部加起来 < 1s。改完 `scripts/align_audio_stable_ts.py`、`scripts/process_video_render_jobs.py` 里的折行函数、`_load_alignment_subtimes`、封面 layout / 校验 / fallback、或者 `templates/index.html` 之后跑一下。

## 已知问题 / 已记未修

- render 守护进程：60s+ 视频需要 `RENDER_TIMEOUT_SEC=600`（已经设了）；90s+ 可能还得再调或降 fps。
- `_load_alignment_subtimes` 跨场景句子的归属（句子同时落在两个场景里时，分配给哪个场景的策略）当前是"两端都收、各自剪到自己的时间范围"，对超长句子可能会出现两个场景都各显示一段的轻微重叠。常见用法下场景按句界切，触发不到。
