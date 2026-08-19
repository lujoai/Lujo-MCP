# Lujo-MCP 环境部署与功能启用指南

> 目标：把“代码已存在的能力”转换成“团队可复现启用、可验证交付的能力”。  
> 功能完成度与当前验证状态以内部文档为准。

## 一、适用范围

本指南覆盖以下需要额外环境或开关才能启用的能力：

1. PostgreSQL / asyncpg
2. Redis 状态后端与 L2 缓存
3. Playwright `verify_ui` / `auto_test`
4. 熔断器
5. OpenTelemetry

## 二、推荐准备方式

### 方式一：本机已有服务

- PostgreSQL：监听 `localhost:5432`
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

### 0. 环境变量权威来源

为避免本机 `.env` 与 Docker 配置互相覆盖，先统一以下约定：

1. **应用权威来源**：`PG_HOST`、`PG_PORT`、`PG_DATABASE`、`PG_USER`、`PG_PASSWORD`
2. **Docker 初始化专用**：`POSTGRES_PASSWORD`
3. **外部工具兼容项**：`DATABASE_URL`，应用本身不会读取

建议执行规则：

- 本机直连已有 PostgreSQL 时，以 `PG_*` 为准
- 使用 `docker compose` 时，`POSTGRES_PASSWORD` 必须填写，且建议与 `PG_PASSWORD` 保持一致
- 若填写 `DATABASE_URL`，密码中的 `@`、`:`、`/` 等特殊字符必须先做 URL 编码
- 出现 PG 连接异常时，先核对 `PG_PASSWORD`，不要先改 `pg_hba.conf`

### 1. PostgreSQL（同步 PG）

```env
STORAGE_BACKEND=postgresql
PG_HOST=localhost
PG_PORT=5432
PG_DATABASE=lujo_mcp
PG_USER=postgres
PG_PASSWORD=your_password
POSTGRES_PASSWORD=your_password
```

验证命令：

```powershell
python -m pytest tests/integration/test_pg_integration.py -q
```

本机最小基线建议：

- 数据库服务：`localhost:5432`
- 数据库名：`lujo_mcp`
- 用户：`postgres`
- 密码：以当前本机 PostgreSQL 实际密码为准

若你同时维护 `.env` 与 Docker 环境，推荐把 `PG_PASSWORD` 与 `POSTGRES_PASSWORD` 设为同一个值。

### 2. asyncpg（异步 PG）

```env
STORAGE_BACKEND=postgresql
PG_ASYNC_ENABLED=true
PG_ASYNC_MIN=2
PG_ASYNC_MAX=20
```

建议同步运行：

```powershell
python -m pytest tests/integration/test_runtime_enablement.py -q -k asyncpg
```

### 3. Redis 状态后端与缓存

```env
STATE_BACKEND=redis
REDIS_URL=redis://localhost:6379/0
```

验证命令：

```powershell
python -m pytest tests/integration/test_runtime_enablement.py -q -k redis
python -m pytest tests/integration/test_redis_cache_integration.py -q
```

### 4. Playwright UI verify / auto_test

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

### 5. 熔断器

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

### 6. OpenTelemetry

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

1. 先确认基础依赖：PostgreSQL / Redis / Playwright / OTLP Collector 是否可达
2. 再核对 `.env` 中的权威变量是否正确，尤其是 `PG_PASSWORD`
3. 先跑对应模块的单元测试
4. 再跑环境集成测试
5. 最后更新内部稳定性验证报告

## 五、当前已知环境问题

### 1. Docker Compose 路径

当前机器若只装了 Docker CLI、未启动 daemon，会在 `docker compose up` 时失败，典型报错为：

```text
failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine
```

### 2. 本地 PostgreSQL 路径

本轮已确认本机 PostgreSQL 服务本身可用，之前的 PG 阻塞根因是：

- 本地 `.env` 中 `PG_PASSWORD` 与当前 PostgreSQL 实际密码不一致
- PostgreSQL 日志显示为 `用户 "postgres" Password 认证失败`
- 在修正凭据后，`psql`、`psycopg2`、`asyncpg` smoke test 与 `test_pg_integration.py` 均可通过

因此当前推荐的排查顺序应为：

1. 先核对 `.env` / 本地环境变量中的 `PG_HOST`、`PG_PORT`、`PG_DATABASE`、`PG_USER`、`PG_PASSWORD`
2. 用 `psql` 或数据库 GUI 工具验证同一组凭据能否成功登录
3. 若仍失败，再查看 PostgreSQL 服务器日志，确认是否为认证失败、库不存在或权限不足
4. 只有在凭据确认无误后，才继续排查 `pg_hba.conf`、`postgresql.conf`、SSL 或编码问题

## 六、验收输出要求

完成任一环境能力验证后，至少要同步三处：

1. 更新内部稳定性验证报告的结论
2. 如出现新问题，补录到内部 TODO 台账
3. 如完成度发生变化，更新内部交付矩阵
