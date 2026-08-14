# video-studio

把一个**主题**（如“光伏发电的原理”）制作成中文科普短视频：提纲确认、旁白与封面、逐场景配图、烧入字幕、AI 配音、强制对齐、可选背景乐以及成片发布。

Web 端支持 `16:9`、`9:16`、`1:1` 三种画幅，时长可在 5–600 秒之间配置（默认 110 秒）。完整模式经过四个宿主机守护进程；`preview_only` 模式跳过耗时的配图与 Hyperframes 渲染，用于快速检查文稿、配音和字幕时序。

## 产物长什么样

- **输入**：主题、画幅、目标时长，以及是否启用 `preview_only`。
- **人工节点**：LLM 先生成提纲，用户可编辑知识点、叙事角度和开场钩子，确认后才开始写完整旁白。
- **完整输出**：0.8 秒钩子封面 → 逐场景配图 → 烧入字幕 → AI 配音 → 可选背景乐。
- **快速预览**：黑底字幕 + 配音的 `preview-{N}s.mp4`，不生成正式场景素材。
- **本地产物**：完整成片在 `runs/{job_id}/final.mp4`；成功发布后，job 的 `final.mp4_url` 保存 Cloudflare R2 的 7 天 presigned URL。

真实跑过的主题包括：*光伏发电的原理*、*糖为什么是战略物资*、*如果不吃脂肪会怎么样*、*飞机起降为什么要拉窗板*。

## 工作原理

Web 应用负责创建和管理 job，四个 systemd path unit 分别唤醒 script、director、render、narrate 守护进程。提纲是当前唯一的人工确认节点；确认以后，各阶段自动级联。

```text
Web UI（Flask + gunicorn :9998）
  POST /api/jobs
    → jobs/video/v_<id>.json（pending）
    → .video-script-trigger
         │
         ▼
script 守护进程
  pending → outlining → ready_outline
         │
         └─ 用户编辑并确认提纲
              → pending_script → writing → ready_script
              → .video-director-trigger
                       │
                       ▼
director 守护进程
  ready_script → 生成 runs/<id>/shotlist.json → ready_shotlist
  → .video-render-trigger
                       │
                       ▼
render 守护进程
  ready_shotlist → rendering → 生成 video/raw.mp4 → rendered
  → .video-narrate-trigger
                       │
                       ▼
narrate 守护进程
  rendered → narrating
  → TTS → stable-ts 强制对齐 → 按真实音频时序重渲染
  → 可选 BGM → FFmpeg 合成 → R2 上传 → final
```

完整状态链为：

```text
pending → outlining → ready_outline → pending_script → writing
→ ready_script → ready_shotlist → rendering → rendered → narrating → final
```

任一守护进程失败都会把 job 置为 `error` 并写入错误信息。job 状态保存在 `jobs/video/v_*.json`；项目根目录下的 `.video-{script,director,render,narrate}-trigger` 只负责唤醒对应阶段。

### `preview_only` 快速路径

`preview_only=true` 时仍会执行提纲确认、写稿、TTS 和强制对齐，但 script 阶段完成后直接把 job 置为 `rendered` 并触发 narrate。narrate 使用 `preview_caption_ffmpeg.py` 生成黑底字幕视频，不经过 director、素材生成或 Hyperframes。

封面校验、stable-ts 强制对齐、v9 字幕切分、脚本长度和重跑入口等内部机制见 [docs/architecture.md](docs/architecture.md)。

## 快速开始

Web 容器只运行 API 和 UI；完整视频流水线在宿主机上由 systemd 执行。Web 使用 Python 3.11，宿主机还需要准备 FFmpeg、渲染环境、stable-ts 以及各素材/TTS 服务所需配置。

```bash
# Web
pip install -r requirements.txt
gunicorn -c gunicorn.conf.py app:app      # :9998

# 或使用容器
docker compose up -d --build

# 守护进程（宿主机）
sudo cp systemd/*.service systemd/*.path /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
  video-studio-script-watcher.path \
  video-studio-director-watcher.path \
  video-studio-render-watcher.path \
  video-studio-narrate-watcher.path
```

健康检查：

```bash
curl http://127.0.0.1:9998/api/health
```

正常响应包含 `"ok": true`。

主要环境变量：

- `APP_PASSWORD`：Web 登录密码。
- `APP_COOKIE_SECRET`：登录 Cookie 的 HMAC secret。
- `VOICE_STUDIO_DIR`：可选；覆盖 TTS 脚本和音色注册表所在目录，默认使用本仓库。
- `LLM_CONFIG_FILE`：可选；覆盖 `llm_config.json` 路径。
- `R2_CREDENTIALS_FILE`：可选；覆盖 R2 凭证文件路径。
- `TZ=Asia/Shanghai`：保持容器、宿主机和 job 时间一致。

## 目录布局

```text
app.py                            Flask Web 应用（UI + JSON API）
gunicorn.conf.py                  2 个 sync worker，默认监听 :9998
Dockerfile / docker-compose.yml   Web 容器配置
llm_config.json.example           LLM 连接配置示例
SKILL.md                          agent 操作 video job 的规范流程
docs/architecture.md              封面、对齐、字幕等内部机制
docs/superpowers/                 特性设计与实施记录（默认被 gitignore）
scripts/
  process_video_script_jobs.py      提纲 + 旁白 + cover.json
  process_video_director_jobs.py    旁白分块 → shotlist.json
  process_video_render_jobs.py      场景素材 + HTML composition + raw.mp4
  process_video_narrate_jobs.py     TTS + 对齐 + 重渲染 + 混音 + 发布
  llm_client.py                     Claude Messages API 薄封装
  align_audio_stable_ts.py          Whisper/stable-ts 强制对齐
  preview_caption_ffmpeg.py         preview_only 黑底字幕快速路径
  minimax_tts.py / *_subs.py        本仓库自带的 MiniMax TTS 封装
  minimax_image_gen.py              MiniMax 场景图生成
  pixabay_*.py / pexels_*.py        素材搜索与缓存
  upload_to_oss.py                  发布到 Cloudflare R2
  voice_registry.json               本仓库音色注册表
  test_*.py                         单元与 smoke/integration 测试
systemd/                          4 个 path unit + 4 个 oneshot service
templates/                        Web UI、登录页和占位视频模板
jobs/video/                       活跃 job JSON
archive/video/                    已归档 job JSON
runs/{job_id}/                    每个 job 的工作目录与产物
```

典型 `runs/{job_id}/`：

```text
script.txt                       最终旁白
cover.json                       0.8 秒封面文案与高亮范围
shotlist.json                    director 生成的逐场景视觉规格
alignment.json                   基于真实音频的字/句时间戳
composition/
  index.html                     Hyperframes composition
  chunks.json                    固定的场景切分
  images/ / videos/              场景素材
  video-only.mp4                 无音轨渲染结果
video/raw.mp4                    render 阶段输出
audio/voice.mp3                  TTS 配音
audio/mixed.mp3                  配音或配音+BGM
final.mp4                        完整成片
preview-{N}s.mp4                 preview_only 产物
```

提纲保存在 job JSON 的 `outline` 字段，不单独写入 `runs/`。

## 测试

测试脚本可逐个运行，也可以遍历执行：

```bash
for t in scripts/test_*.py; do python3 "$t"; done
```

覆盖范围包括：提纲、脚本长度与修复、LLM 响应解析、director/shotlist、场景切分、字幕折行、封面 layout、alignment→scene/sub times、视觉规格、Pixabay 缓存、渲染 HTML 以及 narrate 音频延迟。

多数单元测试不调用外部服务，但完整遍历中包含 smoke/integration 路径；部分测试需要可写的 `/var/log/video-studio`、网络访问或本机渲染依赖。排查单一模块时，优先直接运行对应的 `scripts/test_*.py`。

## LLM、TTS 与发布依赖

- script、director 和视觉规格提取通过 `scripts/llm_client.py` 调用 Claude Messages API；连接信息默认从项目根目录的 `llm_config.json` 读取，环境变量优先。
- `scripts/minimax_tts.py`、`scripts/minimax_tts_subs.py` 和 `scripts/voice_registry.json` 均有项目内副本。`VOICE_STUDIO_DIR` 可改指其他兼容目录。
- 新建 job 默认音色为 `Chinese_casual_instructor_nv1`（显示名“活力讲师”，speed 1.15）。旧 job 会继续使用各自 JSON 中保存的音色。
- R2 凭证优先从 `R2_CREDENTIALS_FILE` 读取，其次读取 gitignored 的 `scripts/r2_credentials.md`；最终 URL 是最长 7 天有效的 presigned URL。
- `scripts/minimax_api_key.txt`、`scripts/pexels_api_key.txt`、`scripts/pixabay_api_key.txt`、`scripts/r2_credentials.md` 和 `llm_config.json` 都被 `.gitignore` 排除。
