# Content Studio — 重建版

Content Studio 是一套可重复运行的科普稿生产系统：给定一个主题，自动生成诊断、调研事实包、3 个差异化 pitch、叙事结构、旁白草稿并允许编辑通过行内批注做定向改写，最终产出可批准 (approve) 的稿件与不可变 speech plan。

Web 服务跑在 **端口 10000**，与老系统（端口 9998）并行运行 —— 互不干扰，可在同一台机器上同时验证。

## 与老系统 (port 9998) 的关系

| | Content Studio (新) | 老 video-studio |
| --- | --- | --- |
| 端口 | `10000` | `9998` |
| 入口 | `studio/api/app.py` (FastAPI) | `app.py` (Flask) |
| 数据 | SQLite via `STUDIO_DATABASE_URL` + `studio-data` 卷 | `jobs/` + `runs/` 文件 |
| 工作流 | 8 个 stage 走 LeaseQueue | 4 个 systemd watcher 守护进程 |
| Provider 抽象 | `studio.providers.{Anthropic,HttpSearch}Provider` | 直接调用 `scripts/llm_client.py` |

**老系统在本仓库内仍然可运行，但任何针对它的修改都不属于本计划。** 新系统不得触碰 `jobs/`、`runs/`、`logs/`、`archive/`、`__pycache__/`、`.video-*` 触发文件，也不得改动 `systemd/video-studio-*-watcher.{service,path}`。

## 离线 vs 在线运行

默认测试与日常开发使用 **离线模式** —— 完全不需要 LLM / 搜索 / TTS / 对象存储：

```bash
# 一行启动离线评估（跑全部离线 fixture）
uv run content-studio evaluate --output evaluation/runs

# 验收：包含 pytest + npm test + npm run build + evaluate
bash scripts/run_content_acceptance.sh --offline
```

在线模式仅用于人工验收：使用真实模型生成 24 个主题的真实稿件。这会消耗 API 额度，因此默认拒绝启动：

```bash
STUDIO_ONLINE_AUTHORIZED=1 bash scripts/run_content_acceptance.sh --online
```

### Online acceptance

Task 14 尚未把 24 主题线上生成接入到 `run_content_acceptance.sh`；脚本进入 `--online` 分支会立即以 exit 2 退出并指向本节，不会伪造 PASS。Operator 需要走下列手工流程：

1. 设置 `STUDIO_ONLINE_AUTHORIZED=1` 以及模型 / 搜索供应商的密钥（详见 `docs/operations/content-studio.md`）。
2. 在容器外或 worker 容器内手动调用 `uv run python -m studio.worker_main`（或 `docker compose -f docker-compose.next.yml up -d content-studio-worker`），针对 `evaluation/topics.yaml` 的 24 个主题跑完整 8 stage。
3. 生成的盲评产物导出到 `evaluation/results/<run-id>/online/`，并把 envelope 放在 `evaluation/results/<run-id>/online/results.json`。
4. 把步骤文件和 envelope 路径记录在 `evaluation/results/<run-id>/summary.md` 的 `## Online` 段落下方。

## 端口与认证

* Web：10000（绑定到 `0.0.0.0`）。
* 默认开启单用户 session 认证。设置 `STUDIO_CONTENT_STUDIO_PASSWORD` 后才能登录。
* 设置 `STUDIO_PRODUCTION=1` 后 cookie 强制 `Secure`，必须配 HTTPS。

## 数据落地

* **SQLite 数据库**：默认 `studio.db`，容器化部署位于 `/data/studio.db`，由 `studio-data` Docker 卷承载。
* **工件存储**：以 JSON 列形式存储在 `artifacts` 表里（immutable append-only）。
* **媒体产物**：当前阶段不产出媒体。video / image / audio 输出将在后续视频流水线计划中引入。

## 启动 Worker

Worker 与 Web 共享同一镜像，但作为独立服务运行：

```bash
# Docker
docker compose -f docker-compose.next.yml up -d content-studio-worker

# systemd（生产）
sudo cp systemd/video-studio-next-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now video-studio-next-worker.service
```

worker 进程通过 SIGTERM / SIGINT 安全退出；当前 lease (900s) 足以覆盖每个 stage 的同步执行。

## 目录速览

```text
studio/
  api/                       FastAPI 路由 + 鉴权 + 错误信封
  content/                   诊断、调研、pitch、叙事、草稿、review、speech 服务
  providers/                 Anthropic / 搜索 / fake provider
  cli.py                     `content-studio evaluate` 入口
  worker.py                  StageDispatcher (claim → handle → finish/fail)
  worker_main.py             长跑 worker entrypoint（systemd / Docker 入口）
  jobs.py                    LeaseQueue（lease + 恢复 + MAX_ATTEMPTS）
migrations/                  Alembic 迁移（content core + project topic）
evaluation/                  离线评估 rubric + topics
web/                         React + Vite SPA
systemd/
  video-studio-next-worker.service   worker 单元
  video-studio-*-watcher.{service,path}    老系统（请勿触碰）
docs/operations/content-studio.md    运维手册
scripts/run_content_acceptance.sh    验收入口
tests/test_deployment_contract.py    部署契约测试（Task 14）
docker-compose.next.yml              Content Studio compose（含 web + worker）
Dockerfile.next                      共享镜像
```

## 验收清单（Content Studio Stop Gate）

满足以下条件即视为 Content Studio 实现阶段结束（cutover 计划另议）：

1. `uv run pytest -q` 全绿
3. `npm --prefix web test` 全绿
4. `npm --prefix web run build` 无 TypeScript / 打包错误
5. `docker compose -f docker-compose.next.yml config` 无错（Docker 环境）
6. `bash scripts/run_content_acceptance.sh --offline` exit 0，输出 `aggregate_pass_rate=1.00`
7. 已生成一份线上 acceptance bundle（人工触发 `STUDIO_ONLINE_AUTHORIZED=1` + `--online`）
8. `git status --short` 不出现 `jobs/`、`runs/`、`.video-*`、老 system service 等遗留路径的改动

## 进一步阅读

* `docs/operations/content-studio.md` — 运维手册（env、迁移、provider、worker 恢复、日志）
* `docs/superpowers/specs/2026-08-17-content-studio-rebuild-design.md` — 重建设计文档
* `docs/superpowers/plans/2026-08-17-content-studio-implementation.md` — 任务拆分
* `evaluation/rubric.yaml` — 评分维度与阈值
* `README.md` — 老系统 (port 9998) 文档

## 许可证与归属

沿用原 `video-studio` 仓库的协议。