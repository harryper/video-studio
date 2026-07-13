# 解绑 openclaw：路径 + 引擎（实施计划）

真身 = `/root/opc/video-studio`。老的 `/root/.openclaw/workspace/skills/video-studio` 废弃。

> **认证关键事实**：`/root/.claude/settings.json` 里是**第三方代理**——`ANTHROPIC_AUTH_TOKEN`（`fe_oa_…` bearer）+ `ANTHROPIC_BASE_URL=https://api-cc.freemodel.dev`，`ANTHROPIC_MODEL=claude-opus-4-8`。不是官方 Anthropic 端点。代理能力未知，所以引擎改写**只用核心 Messages API**（纯文本进出、不开 thinking，跟当前 `--thinking off` 一致），**保留现有健壮 JSON 解析**，不赌 structured outputs / adaptive thinking。
>
> **安全**：token 只在运行时从 settings.json 读，绝不写进任何文件 / 不打印 / 不进 git。

---

## 一、路径解绑（机械层，低风险）

### Python 常量 → `__file__` 派生（不写死目录）
- `scripts/process_video_script_jobs.py:31` `RUNS_DIR` → `SKILL_DIR/"runs"`
- `scripts/process_video_script_jobs.py:80` `REFERENCE_STYLE` → `SKILL_DIR/"reference-style-video.md"`
- `scripts/process_video_render_jobs.py:38` `VIDEO_RUNS_DIR` → `SKILL_DIR/"runs"`
- `scripts/process_video_render_jobs.py:40` `VIDEO_STYLE_HELPER` → `SKILL_DIR/"reference-style-video.md"`
- `scripts/process_video_narrate_jobs.py:39` `VIDEO_RUNS_DIR` → `SKILL_DIR/"runs"`
- `benchmarks/xingzhe/analyze.py:12-15` → 派生自 `__file__`

### systemd / docker（绝对路径，指向 `/root/opc/video-studio`）
- 6 个 unit：`WorkingDirectory` / `Environment=PATH` / `ExecStart` / `PathModified`
  - PATH 里去掉 `voice-studio/scripts` 和 `public-downloads` 的 `.openclaw` 前缀，改 `/root/opc/...`（voice-studio 路径需确认，见下）
- `docker-compose.yml:13,19,23`：两处 mount + `VOICE_STUDIO_DIR`

### 非运行时（顺手）
- `.claude/settings.local.json`：Read 规则 + 3 条 cp 命令的 `.openclaw` 路径
- `scripts/test_cover_layout.py:5` docstring 的 `Run:` 路径

---

## 二、引擎解绑（openclaw agent → 直连 Claude API）

### 公共：`_anthropic_client()` helper（新增，或放进小模块 `scripts/llm_client.py`）
- 读 `/root/.claude/settings.json` 的 `env` 块 → 取 `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL`（env 变量已设则优先）
- 构造 `anthropic.Anthropic(auth_token=..., base_url=...)`
- 一个 `complete(system, user, max_tokens) -> str` 薄封装，返回 `resp.content[0].text`，不传 `thinking`
- `anthropic` 加进 `requirements.txt`（守护进程在宿主机 systemd 下跑，需宿主 python3 装好）

### keywords 守护进程（`extract_scene_keywords.py`）——小改
- `_call_llm`：把 `subprocess openclaw` 换成 `complete(SYSTEM_PROMPT, build_spec_prompt(...))`，返回 `response.content[0].text`
- **保留** `_parse_spec_array` 整套（strategy 2 的 openclaw 信封解析对直连无害，会自然 fall through 到 strategy 1/3/4）、`_coerce_spec`、长度对齐 `_fits` + 一次重试、缓存
- 删除：`NODE`/`OPENCLAW` 常量、`full_message` 的 `[系统指令]/[用户]` 拼接（改用真正的 system 参数）

### script 守护进程（`process_video_script_jobs.py`）——塌缩成"单次补全 + 守护进程做 I/O"
- 新 `generate_script(job)`：
  1. 守护进程**自己读** `reference-style-video.md` + `reference-memes.md`，连同 `MEME_GUIDE`/`COVER_INSTRUCTIONS` 内联进 prompt（确定性，不再让模型自己读文件）
  2. `complete()` 返回一个 JSON `{"script": "...", "cover": {"main","main_highlight":[s,e],"sub"}}`；用健壮提取（复用 keywords 式的括号匹配兜底）
  3. 守护进程**自己写** `runs/{id}/script.txt` + `runs/{id}/cover.json`
- **保留不动**：`finalize_from_script_file`、`parse_cover_validation`、`cover_fallback`、`_is_valid_highlight`、长度校验、`build_prompt`/`build_repair_prompt`（措辞微调：删掉"用文件写入工具落盘"这类 agent 专属指令）
- `repair_script_length`：改成再调一次 `complete()` 拿修订稿，覆写 script.txt
- `process_one`：简化——去掉"agent 是否自己改了 job JSON"分支、去掉 session 抓取分支；正常 try/except 包 API 调用
- **删除**：`run_agent`、`scrape_session_error`、`_session_jsonl_path`、`OPENCLAW`/`OPENCLAW_ROOT`/`NODE`、session-key 逻辑、`WORKSPACE_DIR` 作为 cwd 的用法

---

## 三、R2 / agent-memory 解绑（`upload_to_oss.py`）
- **已定位**：凭证真身在 `/root/.openclaw/workspace/skills/agent-memory/memories/storage/r2-oss-media-upload.md`（agent-memory **没搬**到 `/root/opc`）。文件含**活密钥**，头部注明"do not expose in replies"。
- `CREDENTIALS_FILE` → 读 env `R2_CREDENTIALS_FILE`，否则回落项目内 `SKILL_DIR/scripts/r2_credentials.md`（gitignored）
- 用 `cp` 把真身复制进项目内路径（**绝不 echo 密钥**，不用 Write 内联）
- `.gitignore` 增加 `scripts/r2_credentials.md`

---

## 四、测试 + 文档
- 新增离线单测（mock `anthropic.Anthropic`，<1s、无外部依赖）：
  - `test_llm_client.py`：settings.json 解析、client 构造
  - keywords：mock 文本响应 → 归一化 specs（含坏 JSON 兜底）
  - script：mock `{script,cover}` 响应 → 写出 script.txt + cover.json；坏 cover → fallback
- README / SKILL.md / `docs/architecture.md`：去掉 openclaw 框架叙述，写"LLM 后端 = 直连 Claude API（经 settings.json 配置的端点）"

---

## ⚠️ 阻断（2026-07-13 发现）：settings.json 的凭证不能直连 SDK

live 冒烟结果：`api-cc.freemodel.dev` + `fe_oa_…` token 返回
> "Access Denied: This service is restricted to authorized use through the official Claude Code client only."

即代理**只认官方 Claude Code 客户端**，SDK / requests / 任何自动化工具都被拒。所以"直连 Claude API via anthropic SDK"这条路用**这套凭证**走不通。引擎解绑需要换一个可编程访问的凭证/端点，见下方待你决策。

## 五、待 Bash 分类器恢复后的验证（不假装 done）
1. **定位 R2 凭证真身**（opc vs openclaw）再决定复制来源
2. **`anthropic` SDK 装得上**（宿主 python3）
3. **live 冒烟**：一次 `complete()` 打通代理端点 → 确认基本 Messages API 可用；keywords + script 各跑一个真 job 端到端
4. 确认 voice-studio 在 `/root/opc` 下的真实路径（systemd PATH 用），没有就保留 `.openclaw` 那条并标注
5. 全量单测 `for t in scripts/test_*.py`

任一验证失败 → 如实报告，不标 done。
