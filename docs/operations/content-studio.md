# Content Studio — 操作手册

本文档面向运维与排障，覆盖 Content Studio (端口 `10000`) 的环境变量、数据库迁移、测试数据重置、Provider 配置、Worker 恢复行为与日志位置。它假设读者熟悉 systemd、Docker Compose 与 SQLite。

## 1. 环境变量

所有变量以 `STUDIO_` 为前缀，由 `studio.config.Settings` 读取。未列出的变量请忽略 —— 代码不会读取它们。

| 变量 | 必需 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `STUDIO_DATABASE_URL` | 是 | `sqlite+pysqlite:///./studio.db` | SQLAlchemy 数据库 URL。生产必须指向共享卷（`sqlite+pysqlite:////data/studio.db`）让 web 与 worker 同时访问。 |
| `STUDIO_CONTENT_STUDIO_PASSWORD` | 是 | `""` | Web 登录密码。空值会让所有登录尝试 401 失败。 |
| `STUDIO_CONTENT_STUDIO_SESSION_SECRET` | 否 | 随机进程密钥 | 会话 cookie 的 HMAC 密钥。多 worker 部署必须设置同一值，否则任意 worker 重启都会让所有会话失效。 |
| `STUDIO_PRODUCTION` | 否 | `False` | 当为 `True` 时，session cookie 标记 `Secure`，仅在 HTTPS 下发送。 |
| `STUDIO_PROVIDER_NAME` | 否 | `stub` | 模型 provider 名称。当前正式部署使用 `AnthropicProvider`，由 `worker_main` 直接构造，无需此变量。 |
| `STUDIO_SEARCH_PROVIDER_URL` | 是（线上） | `""` | 高风险事实核验的 HTTP 搜索端点。空值时 `worker_main` 会记录警告并降级（所有高风险事实 → 验证失败）。 |
| `STUDIO_SEARCH_PROVIDER_TOKEN` | 否 | `""` | 搜索端点可选 bearer token。 |
| `STUDIO_LEASE_SECONDS` | 否 | `300` | 单次 lease 时长；当前实现使用 `studio.jobs.LEASE_SECONDS = 900`，需修改源码调整。 |
| `STUDIO_SSE_POLL_INTERVAL_MS` | 否 | `500` | SSE 流轮询间隔（毫秒）。 |
| `STUDIO_SSE_HEARTBEAT_MS` | 否 | `15000` | SSE 心跳周期（毫秒）。 |
| `STUDIO_SSE_MAX_RUNTIME_MS` | 否 | `0`（无限） | 单个 SSE 连接最大时长；测试可调小以加速断言。 |
| `STUDIO_WORKER_ID` | 否 | `<hostname>-<uuid>` | worker 标识，便于在 `stage_jobs` 中追踪 lease 来源。 |
| `STUDIO_LOG_LEVEL` | 否 | `INFO` | `studio.worker_main` 的日志级别。 |

老系统（端口 `9998`）的环境变量请保留原状 —— 本项目不得触碰。

## 2. 数据库迁移

迁移使用 Alembic，应用启动前必须执行 `alembic upgrade head`。当前仓库只覆盖 Content Studio 的表（`projects`、`artifacts`、`project_artifact_heads`、`stage_jobs`、`editorial_comments`）；老系统的 `jobs/video/*.json` 与 SQLite 迁移与本文档无关。

```bash
# 在安装根目录（与 pyproject.toml 同级）
uv run alembic upgrade head
```

迁移是幂等的；重复执行不会重复建表。Alembic 通过 `migrations/env.py` 解析 `STUDIO_DATABASE_URL` 或 `CONTENT_STUDIO_DB`，二者择一即可。

### 失败排查

* **`sqlite3.OperationalError: database is locked`** — `studio.db` 同时被多个进程持有。Web 与 worker 不应该并发写；通过 `studio-data` 卷共享文件时务必使用 SQLite WAL（默认已开启）。如复现，请确认只启动了一个 worker 容器 / systemd unit。
* **`alembic.util.exc.CommandError: Can't locate revision identified by 'xxxx'`** — 迁移链断裂，请确认部署介质中包含完整的 `migrations/versions/` 目录。

## 3. 备份-free 测试数据重置

Content Studio 默认 OFFLINE（`uv run content-studio evaluate` 不调用真实模型）。在多次评估运行之间需要一份干净的数据库，只需删除共享卷内的 SQLite 文件并让下一次启动重建：

```bash
# Docker 部署：清空卷后重启
docker compose -f docker-compose.next.yml down
docker volume rm video-studio_studio-data
docker compose -f docker-compose.next.yml up -d

# systemd 部署
sudo systemctl stop video-studio-next-worker.service
sudo rm -f /data/studio.db /data/studio.db-wal /data/studio.db-shm
sudo systemctl start video-studio-next-worker.service
```

应用启动时会通过 `Base.metadata.create_all` 自动建表（SQLAlchemy ORM 模式）；Alembic 用于初始版本号 + 后续迁移。

> 不要备份这些 SQLite 文件 —— Content Studio 测试数据设计为可丢弃。

## 4. Provider 配置

### Anthropic（模型）

Worker 进程在启动时构造 `studio.providers.AnthropicProvider`。它要求 `anthropic` SDK 能从标准环境变量（`ANTHROPIC_API_KEY`）或容器内的 secrets 文件中读取凭证。

```bash
# systemd：在 /etc/video-studio-next/secrets.env 中维护
echo 'ANTHROPIC_API_KEY=sk-ant-...' | sudo tee /etc/video-studio-next/secrets.env
sudo chmod 600 /etc/video-studio-next/secrets.env
sudo systemctl edit video-studio-next-worker.service
# 添加：EnvironmentFile=/etc/video-studio-next/secrets.env

# Docker：在 .env 文件或 secrets manager 中维护
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> /opt/video-studio-next/.env
```

凭证缺失会在 worker 启动时立即抛出，systemd unit 会以非零退出码重启。检查日志：`journalctl -u video-studio-next-worker.service -n 50`。

### 搜索（高风险事实核验）

设置 `STUDIO_SEARCH_PROVIDER_URL` 指向返回 JSON 的端点。响应必须形如：

```json
{
  "results": [
    {
      "url": "https://example.com/path",
      "title": "...",
      "snippet": "...",
      "published_at": "2026-01-01T00:00:00Z"
    }
  ]
}
```

`studio.providers.HttpSearchProvider` 会把每个 `result` 验证为 `SourceDocument`。空响应会让所有高风险事实 → `unverified`，CI 评估会因此失败。

## 5. Worker 恢复

* **Lease 窗口**：默认 `studio.jobs.LEASE_SECONDS = 900` 秒（15 分钟）。
* **最大尝试次数**：`studio.jobs.MAX_ATTEMPTS = 3`。超过后任务被永久标记 `failed`，错误码 `lease_expired`，错误消息 `attempts_exceeded`。
* **重启策略**：`Restart=on-failure` + `RestartSec=5`。每次失败 systemd 等待 5 秒后重启；只要 lease 仍归属同一个 worker，下次 claim 成功。
* **指数退避**：当前未实现 —— 重启间隔固定 5 秒。如需退避，编辑 systemd unit 中的 `RestartSec=` 并 `daemon-reload`。
* **幂等性**：所有 handler 都是「读输入 → 写新 artifact」；重启不会产生半成品 artifact；已完成任务不会重复执行。

```bash
# 查看 worker 最近一次重启
systemctl show video-studio-next-worker.service -p NRestarts,LastRestartStartTimestamp

# 查看某个 job 的状态（替换 job_id）
sqlite3 /data/studio.db 'SELECT id, stage, status, attempts, error_code FROM stage_jobs WHERE id = "...";'

# 手动重试已 failed 的 job（路由级；不需要直接 SQL）
curl -X POST -H "X-CSRF-Token: $CSRF" http://localhost:10000/api/projects/$PROJECT/jobs/$JOB/retry
```

### 手动恢复流程

1. 找到 `error_code=lease_expired` 且 `attempts >= 3` 的 job。
2. 通过 HTTP retry 路由触发一次重置；新 attempt 计数从 0 开始。
3. 如果 retry 持续失败，检查 model provider 凭证与搜索端点。

## 6. 日志

### systemd

```bash
# 实时 tail
journalctl -u video-studio-next-worker.service -f

# 最近 200 行
journalctl -u video-studio-next-worker.service -n 200

# 配合 web 服务一起看
journalctl -u 'video-studio-next-*' -f
```

### Docker

```bash
docker compose -f docker-compose.next.yml logs -f content-studio-worker
docker compose -f docker-compose.next.yml logs -f content-studio-web
```

### 应用层日志

`studio.worker_main` 在启动 / 关闭 / provider 初始化失败时输出结构化日志（时间戳 + level + 名字 + 消息）。每条日志一行，便于 grep。默认级别 `INFO`，可通过 `STUDIO_LOG_LEVEL=DEBUG` 调高。

每个 stage 的处理路径不打印每条 handler 调用 —— 长跑测试输出会淹没真正有用的信号。需要更细粒度的日志时启用 `STUDIO_LOG_LEVEL=DEBUG`，并把范围限定到 `studio.handlers`。

## 7. 健康检查

```bash
# Web
curl http://localhost:10000/api/health
# 返回 {"ok": true, "app": "content-studio"}

# Worker（Docker 内部）
docker compose -f docker-compose.next.yml exec content-studio-worker \
  pgrep -f 'python -m studio.worker_main'

# Worker（systemd）
systemctl is-active video-studio-next-worker.service
```

## 8. 验收

`scripts/run_content_acceptance.sh` 是一站式 OFFLINE 验收入口：跑全套 pytest、`npm test`、`npm run build`、`content-studio evaluate`，并把结果落到 `evaluation/results/<run-id>/summary.md`。线上生成必须显式 `--online` 且 `STUDIO_ONLINE_AUTHORIZED=1`；详见脚本帮助。

```bash
bash scripts/run_content_acceptance.sh --offline   # 默认；CI 用
bash scripts/run_content_acceptance.sh --help     # 查看更多开关
```

## 9. 老系统（端口 9998）

老系统与 Content Studio 并行运行；它的 systemd units、脚本、数据库与日志不受本文档管理。任何变更请通过老系统的运维文档进行。**禁止在本仓库对 `jobs/`、`runs/`、`logs/`、`archive/`、`__pycache__/` 或 `.video-*` 触发文件进行修改。**