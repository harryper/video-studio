# video-studio

把一个**主题**（如"光伏发电的原理"）自动做成一条 **60–90 秒的中文科普短视频**：配图 + 烧入字幕 + AI 配音 + 背景乐 + 开场钩子封面，全程零人工审。

本仓库是 [voice-studio](https://github.com/harryper/voice-studio) 的兄弟项目，承载共享 Web 工作流里 `mode='video'` 这一条线。TTS（MiniMax）和音色注册表跟 voice-studio 共享——见 [跨 skill 依赖](#跨-skill-依赖)。

## 产物长什么样

- **输入**：一个主题字符串 + 画幅（`16:9` 1920×1080 默认 / `9:16` 1080×1920 / `1:1` 1080×1080）。
- **输出**：一条 mp4——0.8s 钩子封面 → 逐句配图 + 单行字幕 + 逐字对齐的配音 → 背景乐混音。
- 真实跑过的主题：*光伏发电的原理*、*糖为什么是战略物资*、*如果不吃脂肪会怎么样*、*飞机起降为什么要拉窗板*。
- 成片落在 `runs/{job_id}/final.mp4`，发布后写回 job 的 `mp4_url`（Cloudflare R2 的 presigned 链接，7 天过期，不是永久公开地址）。

## 工作原理

三个阶段，每段是一个监听触发文件的 systemd path unit。用户只提交主题，三段自动级联，**没有人审环节**：

```
   Web UI (Flask + gunicorn :9998)
     POST /api/jobs → 建 v_<id>.json(pending) → touch .video-script-trigger
         │
         ▼
   .video-script-trigger  ─▶ script 守护进程   LLM 写旁白 + 封面
         │  status → ready_script            → touch .video-render-trigger
         ▼
   .video-render-trigger  ─▶ render 守护进程   puppeteer + headless chrome
         │  status → rendered   出 raw.mp4(无音轨) → touch .video-narrate-trigger
         ▼
   .video-narrate-trigger ─▶ narrate 守护进程  TTS + 强制对齐 + 背景乐 + ffmpeg 合成
            status → final   出 final.mp4
```

触发器就是项目根目录下裸的 `touch` 标记文件（`.video-{script,render,narrate}-trigger`）。job 状态全在 `jobs/video/v_*.json`；触发器只负责唤醒下一段守护进程。守护进程只拣对应 status 的 job——所以从 Web UI 重跑某一段时会先把 status reset 回去。

底层几个不直观的机制（封面校验规则、Whisper 强制对齐、v9 字幕切分、preview 快速路径、脚本长度、重跑端点）都写在 **[docs/architecture.md](docs/architecture.md)**。

## 快速开始

Web 容器只跑 API + UI。守护进程在**宿主机**上由 systemd 跑，job 才能真正推进（需要 voice-studio 在 PATH 里）。

```bash
# Web
pip install -r requirements.txt
gunicorn -c gunicorn.conf.py app:app      # :9998

# 守护进程（宿主机）
sudo cp systemd/*.service systemd/*.path /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
  video-studio-script-watcher.path \
  video-studio-render-watcher.path \
  video-studio-narrate-watcher.path
```

健康检查：`curl http://127.0.0.1:9998/api/health` 应返回 `{"ok": true}`。

需要的环境变量：`APP_PASSWORD`（登录）、`APP_COOKIE_SECRET`（cookie HMAC）、`VOICE_STUDIO_DIR`（跨 skill 路径）、`TZ=Asia/Shanghai`。

## 目录布局

```
app.py                          Flask Web 应用（UI + JSON API）
gunicorn.conf.py                2 个 sync worker，60s 超时
Dockerfile / docker-compose.yml 容器化 Web；绑定 :9998
SKILL.md                        agent 在 chat 里操作 job 的规范流程
docs/architecture.md            内部机制（维护者参考）
docs/superpowers/specs/         逐个特性的设计文档 / 迭代史
reference-style-video.md        喂给脚本 LLM 的风格简报
reference-scripts/              风格样例（不会被复制进产物）
scripts/
  process_video_script_jobs.py    script 守护进程（LLM 旁白 + cover.json 校验）
  process_video_render_jobs.py    render 守护进程（puppeteer + chrome + 封面）
  process_video_narrate_jobs.py   narrate 守护进程（TTS + 背景乐 + audio delay + 合成）
  align_audio_stable_ts.py        Whisper 强制对齐
  preview_caption_ffmpeg.py       黑底 preview mp4（快速路径）
  minimax_tts*.py                 TTS 封装（voice-studio 共享）
  pixabay_*.py / pexels_*.py       素材抓取 + 缓存（Pixabay 为主，Pexels 兼容旧 job）
  upload_to_oss.py                发布到 R2
  test_*.py                       单测（见下）
  voice_registry.json             跟 voice-studio 共享
systemd/                        3 个 path unit + 3 个 oneshot service
templates/                      index.html, login.html, video_placeholder.html
jobs/video/                     活跃 job JSON（一个 v_*.json 一条）
runs/{job_id}/                  每个 job 的产物（script.txt / alignment.json / cover.json /
                                keywords.json / composition/ / audio/ / final.mp4 / preview-*.mp4）
```

## 测试

无外部依赖，全部加起来 < 1s。改了对齐 / 折行 / 封面 layout / `templates/index.html` 之后跑一遍：

```bash
for t in scripts/test_*.py; do python3 "$t"; done
```

单测覆盖：小数点合并对齐、字幕折行 + v9 切分、封面 layout + 校验 + audio delay、alignment→subtimes/scene_times、kinetic overlay 时序、visual specs、pixabay 缓存、脚本长度校验、脚本修复启发式、pexels skip 决策；`test_html_output.py` 是 hyperframes HTML 结构 smoke。

## 跨 skill 依赖

`scripts/minimax_tts.py`、`minimax_tts_subs.py`、`voice_registry.json` 都按绝对路径从 voice-studio 读，不通过 import。systemd 的 `Environment=PATH` 把 `voice-studio/scripts/` 加进去，子进程能解析。

默认音色（新建 job 时写）：`Chinese_casual_instructor_nv1`（显示名"活力讲师"，speed 1.15）。早于该默认切换创建的 job JSON 仍显式持有旧音色，需重新提交才会用当前默认。

密钥文件 `scripts/minimax_api_key.txt`、`pexels_api_key.txt`、`pixabay_api_key.txt` 被 `.gitignore` 排除。
