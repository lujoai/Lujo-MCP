# Lujo-MCP 环境部署与功能启用指南

> **当前版本**。默认且唯一的运行时存储后端为 `STORAGE_BACKEND=memory`（PostgreSQL/asyncpg 后端已正式移除，精确值 `postgresql` 会被启动即拒绝）；KB 调试经验默认写穿本地 SQLite「笔记本」（`KB_PERSIST_ENABLED`/`KB_PERSIST_PATH` 可调）；Redis、Playwright、熔断器和 OpenTelemetry 按场景显式启用。

> 目标：把“代码已存在的能力”转换成“团队可复现启用、可验证交付的能力”。  
> 功能完成度与当前验证状态以内部文档为准。

## 一、适用范围

本指南覆盖以下需要额外环境或开关才能启用的能力：

1. Redis 状态后端与 L2 缓存
2. Playwright `verify_ui` / `auto_test`
3. 熔断器
4. OpenTelemetry

## 二、推荐准备方式

### 方式一：本机已有服务

- Redis：监听 `localhost:6379`
- OTLP Collector：监听 `localhost:4317`（可选）

### 方式二：Docker Compose

仓库内已提供 `docker-compose.yaml`，但要求本机 Docker daemon 已启动。

预检命令：

```powershell
docker --version
docker info
```

若 `docker info` 失败，说明 daemon 未启动，此时无法走 Compose 路径。

## 三、最小启用配置

### 0. 存储：PostgreSQL 已移除（零配置，无需启用）

`STORAGE_BACKEND=memory` 是唯一合法值且为默认，无需任何配置。旧部署遗留的 `PG_*` / `POSTGRES_PASSWORD` / `DATABASE_URL` 环境变量不会重新启用 PostgreSQL、也不会导致启动失败，可安全删除（详见 TROUBLESHOOTING.md L 节）。

### 1. Redis 状态后端与缓存

```env
STATE_BACKEND=redis
REDIS_URL=redis://localhost:6379/0
```

验证命令：

```powershell
python -m pytest tests/integration/test_runtime_enablement.py -q -k redis
python -m pytest tests/integration/test_redis_cache_integration.py -q
```

### 2. Playwright UI verify / auto_test

项目当前未在 `requirements*.txt` 中内置安装 Playwright，需要手动补装：

```powershell
pip install playwright
playwright install chromium
```

本地联调常用配置：

```env
UI_URL_ALLOW_PRIVATE=true
TOOL_TIMEOUT_SECONDS=120
```

若只允许固定内网或本机地址，优先使用：

```env
UI_URL_ALLOW_PRIVATE=false
UI_URL_ALLOWLIST=localhost,127.0.0.1,test.internal
```

验证命令：

```powershell
python -m pytest tests/integration/test_mcp_verify_ui.py -q
python -m pytest tests/integration/test_ui_verify_live.py -q
```

> 说明：当前仓库除了“协议通道不阻塞”验证外，已经补充了本地 HTTP 页面上的真实浏览器交互验证。

### 3. 熔断器

```env
CIRCUIT_BREAKER_ENABLED=true
CB_LLM_MAX_FAILURES=5
CB_LLM_RESET_TIMEOUT=30
CB_PG_MAX_FAILURES=3
CB_PG_RESET_TIMEOUT=15
```

验证命令：

```powershell
python -m pytest tests/unit/test_circuit_breaker.py -q
```

若需要真实环境验证，应在 PG / LLM 服务可控失败场景下补跑：

```powershell
python -m pytest tests/integration/test_runtime_enablement.py -q -k circuit
python -m pytest tests/integration/test_circuit_breaker_recovery.py -q
```

### 4. OpenTelemetry

```env
OTEL_ENABLED=true
OTEL_SERVICE_NAME=Lujo-MCP
OTEL_EXPORTER_ENDPOINT=http://localhost:4317
OTEL_METRICS_INTERVAL_MS=60000
```

验证命令：

```powershell
python -m pytest tests/unit/test_otel.py -q
python -m pytest tests/integration/test_runtime_enablement.py -q -k otel
python -m pytest tests/integration/test_otel_collector_integration.py -q
```

## 四、推荐验证顺序

1. 先确认基础依赖：Redis / Playwright / OTLP Collector 是否可达
2. 再核对 `.env` 中的对应变量是否正确（如 `REDIS_URL`）
3. 先跑对应模块的单元测试
4. 再跑环境集成测试
5. 最后更新内部稳定性验证报告

## 五、当前已知环境问题

### 1. Docker Compose 路径

当前机器若只装了 Docker CLI、未启动 daemon，会在 `docker compose up` 时失败，典型报错为：

```text
failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine
```

### 2. 本地 PostgreSQL 路径（已随 PG 后端移除而不适用）

PostgreSQL 后端已移除，本节原「核对 PG 凭据」的排障内容不再需要；`.env` 中的遗留 `PG_*` 键直接删除即可。

## 六、验收输出要求

完成任一环境能力验证后，至少要同步三处：

1. 更新内部稳定性验证报告的结论
2. 如出现新问题，补录到内部 TODO 台账
3. 如完成度发生变化，更新内部交付矩阵
