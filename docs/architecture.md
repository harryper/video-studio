# video-studio 内部机制

README 讲的是"是什么 / 怎么跑"。本文档是维护者参考，讲清楚流水线里几个不直观的机制：**为什么这样做、约束在哪、改哪些代码会影响它**。设计史（v3/v9 之类的迭代过程）和逐版本决策见 `docs/superpowers/specs/`。

- [封面 splash](#封面-splash)
- [强制对齐](#强制对齐)
- [字幕切分（v9）](#字幕切分v9)
- [preview_only 模式](#preview_only-模式)
- [脚本长度](#脚本长度)
- [LLM 后端](#llm-后端)
- [Web 重跑入口](#web-重跑入口)
- [已知问题](#已知问题)

## 封面 splash

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
- **hl 必须是钩眼词**：含数字 OR 在 `_COVER_HOOK_MARKERS` 否定/转折词集合 OR 在 `_HOOK_SUBSTR` 子串集合里。LLM 写 `[2,4]="是调"` 这种"两个连续非钩眼字"会被校验拒掉，触发 fallback 兜底。
- **sub 严禁剧透**：不能含"因为/所以/其实/真相是/直接说/本质是"。
- **封面时长 < 1s**：跟用户预期一致，不要做成 splash 转场动画。

回归测试：`scripts/test_cover_layout.py` 覆盖 layout 渲染 + 高亮 OOB 边界 + fallback 钩眼词选择 + parse_cover_validation 硬规则 + 首场景 `starts[0]=COVER_DURATION_SEC` + audio delay filter chain 形状。

## 强制对齐

TTS 返回的词级时间戳是模型"打算"什么时候说，不是实测。20s 之后漂移会累积，用户就感觉"字幕比声音慢半拍"。`scripts/align_audio_stable_ts.py` 跑 Whisper 的 cross-attention 对齐，对真实音频波形做逐字时间戳，落到 `runs/{job_id}/alignment.json`，schema 跟 TTS 路径完全一致，下游消费者无感。

aligner 用 `。！？!?.` 切句。ASCII 句点 `.` 在切分集里因为它确实能断英文句子（`i.e. 5` → `i.e.` + `5`），但同一个分隔符也会腰斩小数（`前 0.5 秒` → `前 0.` + `5 秒`）。`_merge_decimal_split_sentences` 把"明显是同一段小数的两半"重新粘回去——条件故意收窄，不吞 `i.e. 5` / `Dr. Smith` 这类合法切分。

回归测试：`scripts/test_align.py`（小数点合并）。

## 字幕切分（v9）

`_split_sentence_into_subs` 在 `scripts/process_video_render_jobs.py`：**每个 `_SPLIT_PUNCT` 字符（`,` `、` `。` `!` `?` 等）切一个 sub**，不贪心填满到 20 字。每个 PUNCT-boundary clause 各自成为一个 sub-caption，由 `wrap_caption_lines` 单行渲染（必要时 ≤ 2 行）。> 20 字且内部无 PUNCT 的 clause 兜底走 `_split_long_clause`。

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

节奏目标：5-8 subs / 10-15s scene，每个 ~1.5s（≈ 10 字 @ TTS speed=1.15）。回归测试：`scripts/test_wrap.py::test_v9_strict_punct_split`（精确匹配上表 7 sub）。

**脚本创作约束**：clause 之间必须用 ASCII `,` / 全角 `、` 隔开（不是逗号连续的 run-on 长句），每个 clause 2-12 字理想。避免单 clause > 20 字（会触发 `_split_long_clause` 兜底，节奏乱）。这条约束已记入 memory（`feedback_subtitle_strict_punct_v9.md`），新脚本创作和配音都按这个走。

## preview_only 模式

跳过完整渲染（图片抓取 + hyperframes）的快速路径。narrate 守护进程改跑 `scripts/preview_caption_ffmpeg.py`，生成黑底 mp4，叠配音轨和烧入式 ASS 字幕。60s 片段约 3–6s 出片，比 ~5min 的完整渲染快两个数量级。

强制对齐 + 字幕时序逻辑跟完整渲染共用同一套代码，所以 preview 是调试字幕/配音同步的正确入口。

## 脚本长度

`scripts/process_video_script_jobs.py`：`MIN_SCRIPT_CHARS = 300`，`MAX_SCRIPT_CHARS = 1200`。短文（300-449 字，比如 30-60s 抖音小知识）和长文（450-1200 字，200s 抖音科普对标大约 1080 字）都接受。Style guide target 是 560-640 字，下限 300 是为了不卡死短文下限——LLM 输出噪声大。回归测试：`scripts/test_script_length_bounds.py`。

## LLM 后端

守护进程（`process_video_script_jobs.py` / `extract_scene_keywords.py`）通过 `scripts/llm_client.py` 直接调 Claude Messages API。连接信息从 `/root/.claude/settings.json` 的 `env` 块读 `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL`，**全部经变量读取，没有任何写死的 URL / token / 模型名**。env 同名变量优先于文件，settings 路径本身可经 `CLAUDE_SETTINGS_FILE` 覆盖。

代理端点偶尔要求"official Claude Code client"指纹时，把 `LLM_CLIENT_HEADERS='{"user-agent":"…","x-stainless-…":"…"}'` 配到 systemd unit 的 `Environment=` 即可，不动代码。

script 守护进程拿到的 JSON 响应 `{script, cover}` 由 daemon 自己落 `runs/<id>/script.txt` + `runs/<id>/cover.json`（不是 LLM 自己写文件）。cover 字段先过 `parse_cover_validation`，不通过就 None、render 端走 `cover_fallback`。JSON 解析对常见 LLM 错误（prose envelope、数组套对象、截断）做了兜底，跟代理兼容性更稳。回归测试：`scripts/test_llm_client.py` + `scripts/test_script_engine_decouple.py` + `scripts/test_keywords_engine_decouple.py`。

## Web 重跑入口

详情面板顶部三个按钮，用来从任一阶段重跑（守护进程只拣特定 status 的 job，所以重跑前必须把 status reset 到对应值）：

- **重跑脚本** → `POST /api/jobs/<id>/script`：status 重置 `pending` + 清 `error` + touch `.video-script-trigger`。
- **重跑渲染** → `POST /api/jobs/<id>/render`：status → `ready_script` + touch `.video-render-trigger`。
- **重跑配音** → `POST /api/jobs/<id>/narrate`：status → `rendered` + touch `.video-narrate-trigger`。

三个按钮共用 `rerunWithFeedback()` helper：请求中禁用 + 显示 `⏳ 已触发` / `✓ 已触发` / `✗ 失败`（防连点），1.5s 后恢复原文字。

> Gunicorn worker 是 fork 模式，加新端点后必须 `pkill -HUP gunicorn` 才会加载新代码。

## 已知问题

- render 守护进程：60s+ 视频需要 `RENDER_TIMEOUT_SEC=600`（已经设了）；90s+ 可能还得再调或降 fps。
- `_load_alignment_subtimes` 跨场景句子的归属（句子同时落在两个场景里时，分配给哪个场景）当前是"两端都收、各自剪到自己的时间范围"，对超长句子可能出现两个场景各显示一段的轻微重叠。常见用法下场景按句界切，触发不到。
