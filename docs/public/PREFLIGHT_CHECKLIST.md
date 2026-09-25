# 启动前检查与功能启用综合手册 / Pre-flight Checklist & Enablement Guide

**适用版本 / Applicable Version**: v0.9.4（已发布稳定版）/ v0.9.5（代码库候选）
**最后更新 / Last Updated**: 2026-09-24

> **说明**：本文档已整合原独立文件 `ENABLEMENT_GUIDE.md`（功能启用指南）全部内容，为 Lujo-MCP 的环境部署、配置检查与可选功能启用（Redis、Playwright、熔断器、OTel）提供一站式操作与验证手册。
> **发布状态**：当前 npm 线上最新已发布稳定版本为 `v0.9.4`；虚拟帧扫描守卫核心修复已合入 main 分支（commit `390849f` / `2dc9c1c`），相关候选已进入 main（commit `e39aeb7`），`v0.9.5` 仍待发布。默认 `STORAGE_BACKEND=memory`（唯一合法值，PostgreSQL 后端已正式移除）；KB 经验默认写穿本地 SQLite「笔记本」（`KB_PERSIST_ENABLED=true`，可用 `KB_PERSIST_PATH` 指定位置）。**升级须知**：默认仅监听 `127.0.0.1`（容器须显式 `HOST=0.0.0.0`）、`/metrics` 免鉴权豁免仅回环生效、关闭期错误码 `TOOL_BUSY`、RBAC 鉴权拒绝码 `AUTH_ERROR -32003`；旧便捷端点 `POST /debug` 已废弃（返回 410），统一收敛至 `POST /api/debug/run`。

---

## 目录 / Table of Contents

- [使用说明 / Usage](#使用说明--usage)
- [1. 运行环境校验 / Runtime Environment](#1-运行环境校验--runtime-environment)
- [2. 依赖安装验证 / Dependency Verification](#2-依赖安装验证--dependency-verification)
- [3. 配置文件检查 / Configuration Check](#3-配置文件检查--configuration-check)
- [4. 数据库（PostgreSQL 已移除） / Database](#4-数据库postgresql-已移除--database)
- [5. Redis 连通性测试 / Redis Connectivity](#5-redis-连通性测试--redis-connectivity)
- [6. 安全与权限核查 / Security & Permissions](#6-安全与权限核查--security--permissions)
- [7. UI 验证环境检查 / UI Verification Environment](#7-ui-验证环境检查--ui-verification-environment)
- [8. 可观测性检查 / Observability Check](#8-可观测性检查--observability-check)
- [9. 核心功能冒烟测试 / Smoke Test](#9-核心功能冒烟测试--smoke-test)
- [10. Docker 部署检查 / Docker Deployment](#10-docker-部署检查--docker-deployment)
- [快速检查脚本 / Quick Check Scripts](#快速检查脚本--quick-check-scripts)
- [异常处理预案 / Contingency Plans](#异常处理预案--contingency-plans)

---

## 使用说明 / Usage

本清单用于服务首次启动或版本升级后的全面检查。按照编号顺序逐项执行，所有「必选」项通过后方可启动服务。「可选」项根据部署场景自行决定。

This checklist is used for full verification before first startup or after version upgrade. Execute items in order; all **Required** items must pass before starting the service. **Optional** items depend on deployment scenario.

**图例 / Legend**:
- [ ] **[必选]** = 生产环境必须通过 / Required for production
- [ ] **[可选]** = 按需启用 / Optional based on scenario

---

## 1. 运行环境校验 / Runtime Environment

### 1.1 Python 版本

- [ ] **[必选]** Python 版本 >= 3.11

```bash
python --version
# 判定标准 / Pass Criteria: 输出 Python 3.11.x 或更高版本
# 异常处理 / Contingency: 安装 Python 3.12+ (推荐)，见 https://www.python.org/downloads/
```

### 1.2 操作系统兼容性

- [ ] **[必选]** 支持 Linux (x86_64/arm64)、macOS (x86_64/arm64)、Windows (x86_64)

```bash
# 判定标准 / Pass Criteria: 上述平台均可运行
# 异常处理 / Contingency: 确认系统架构在支持范围内
```

### 1.3 端口可用性

- [ ] **[必选]** 服务端口 8000 未被占用（或已修改为其他可用端口）

```bash
# Linux/macOS
ss -tlnp | grep 8000
# Windows PowerShell
netstat -ano | findstr :8000

# 判定标准 / Pass Criteria: 无输出表示端口可用
# 异常处理 / Contingency: 
#   方案 A: 终止占用端口的进程
#   方案 B: 修改 .env 中 PORT=其他端口
```

### 1.4 磁盘空间

- [ ] **[必选]** 项目目录所在磁盘剩余空间 >= 500MB

```bash
# Linux/macOS
df -h .
# Windows PowerShell
Get-PSDrive C | Select-Object Used, Free

# 判定标准 / Pass Criteria: 可用空间 >= 500MB
# 异常处理 / Contingency: 清理磁盘空间或更换部署路径
```

---

## 2. 依赖安装验证 / Dependency Verification

### 2.1 Python 核心依赖

- [ ] **[必选]** 所有核心依赖已安装且版本满足要求

```bash
pip install -r requirements.txt
pip check

# 判定标准 / Pass Criteria: pip check 输出 "No broken requirements found"
# 异常处理 / Contingency:
#   - 网络问题: 配置 pip 镜像源 (如 -i https://pypi.tuna.tsinghua.edu.cn/simple)
#   - 版本冲突: 检查是否有其他项目的全局包冲突，推荐使用虚拟环境
```

### 2.2 关键依赖版本确认

- [ ] **[必选]** 核心包版本验证

| 包名 / Package | 最低版本 / Min Version | 检查命令 / Check Command |
|---|---|---|
| fastapi | >= 0.115.0 | `pip show fastapi` |
| uvicorn | >= 0.49.0 | `pip show uvicorn` |
| mcp | >= 1.0.0 | `pip show mcp` |
| pydantic-settings | >= 2.0.0 | `pip show pydantic-settings` |
| httpx | >= 0.27.0 | `pip show httpx` |

### 2.3 可选依赖（按需）

- [ ] **[可选]** Redis 状态后端依赖（`STATE_BACKEND=redis` 时必选）

| 包名 / Package | 用途 / Purpose |
|---|---|
| redis >= 5.0.0 | 分布式限流与缓存 |

- [ ] **[可选]** 可观测性依赖（`OTEL_ENABLED=true` 时必选）

| 包名 / Package | 用途 / Purpose |
|---|---|
| pybreaker >= 1.0.0 | 熔断器 |
| opentelemetry-api >= 1.20.0 | OTel API |
| opentelemetry-sdk >= 1.20.0 | OTel SDK |
| opentelemetry-exporter-otlp-proto-grpc >= 1.20.0 | OTLP gRPC 导出 |

### 2.4 虚拟环境

- [ ] **[必选]** 推荐使用虚拟环境隔离依赖

```bash
# 创建虚拟环境
python -m venv .venv

# 激活虚拟环境
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell:
.venv\Scripts\Activate.ps1

# 判定标准 / Pass Criteria: 命令行提示符出现 (.venv) 前缀
```

---

## 3. 配置文件检查 / Configuration Check

### 3.1 .env 文件存在性

- [ ] **[必选]** `.env` 文件已创建

```bash
# 如果尚未创建，从模板复制
cp .env.example .env

# 判定标准 / Pass Criteria: .env 文件存在于项目根目录
# 异常处理 / Contingency: 从 .env.example 复制并根据实际环境修改
```

### 3.2 LLM 配置

- [ ] **[必选]** LLM API Key 已配置

```bash
# 检查 .env 中以下配置:
# OPENAI_API_KEY=<your_actual_key>    ← 不能为空
# LLM_PROVIDER=openai                  ← openai | zhipu | custom
# LLM_MODEL=gpt-4o                     ← 模型名称

# 判定标准 / Pass Criteria: OPENAI_API_KEY 非空且为有效密钥
# 异常处理 / Contingency:
#   - 服务启动后 /internal/health 会返回 llm_configured: false（降级模式，公开 /health 只返回 status）
#   - LLM 调用将失败，但不影响其他功能
```

- [ ] **[可选]** LLM 参数合理性

| 参数 / Parameter | 合理范围 / Valid Range | 默认值 / Default |
|---|---|---|
| `LLM_TEMPERATURE` | 0.0 ~ 2.0 | 0.3 |
| `LLM_TIMEOUT` | 10 ~ 300 (秒) | 30 |
| `LLM_MAX_RETRIES` | 0 ~ 10 | 3 |

### 3.3 存储后端配置

- [ ] **[必选]** 存储后端配置正确

```bash
# STORAGE_BACKEND 合法值: "memory"（唯一；PostgreSQL 后端已移除）
# 默认 "memory"，无需额外配置即可启动

# 判定标准 / Pass Criteria:
#   - memory 模式: 无需额外检查
#   - 若 .env 仍写着 postgresql：启动会被直接拒绝（fail-fast，不静默降级），
#     改回 memory 或删除该行即可（见 TROUBLESHOOTING.md L 节）
# 异常处理 / Contingency:
#   - 拼写错误同样会导致启动失败（fail-fast 设计）
```

### 3.4 安全配置

- [ ] **[必选]** 生产环境必须配置 `API_KEY`

```bash
# API_KEY=<your_secret_token>
# 判定标准 / Pass Criteria:
#   - 开发环境: 可为空（服务会以免鉴权模式启动，输出警告日志）
#   - 生产环境: 必须设置非空值
#   - 当 HOST=0.0.0.0 且 API_KEY 为空时，服务拒绝启动（安全保护）
# 异常处理 / Contingency:
#   - 生成随机 API Key: python -c "import secrets; print(secrets.token_urlsafe(32))"
```

- [ ] **[必选]** 脱敏配置检查

```bash
# REDACTION_ENABLED=true    ← 生产环境必须为 true！
# 设为 false 将导致敏感数据（密码、token 等）明文存储
# 判定标准 / Pass Criteria: REDACTION_ENABLED=true
```

- [ ] **[可选]** CORS 配置

```bash
# CORS_ORIGINS=           ← 空串=不下发 CORS 头（默认收紧）
# CORS_ORIGINS=*          ← 允许所有来源（不带凭证）
# CORS_ORIGINS=https://example.com,https://app.example.com  ← 白名单

# 判定标准 / Pass Criteria: 根据实际需求配置，生产环境不建议使用 *
```

### 3.4a 鉴权方式说明

- [ ] **[必选]** 确认鉴权方式与当前实现一致

```bash
# 判定标准 / Pass Criteria:
#   - 本项目鉴权基于 API Key（fail-closed，hmac.compare_digest），不引入 JWT/OAuth
#   - 不需要配置 JWT_SECRET 等密钥；历史 BETA 审查中的 JWT 相关项已确认为误报（无 JWT 实现）
# 说明：详见第 6.7 节 鉴权安全（API Key）
```

- [ ] **[必选]** 生产环境 CORS 必须收紧

```bash
# CORS_ORIGINS=https://your-domain.com,https://app.your-domain.com
# 判定标准 / Pass Criteria: 生产环境禁止 CORS_ORIGINS=*
# 当前代码默认 cors_origins=""（不下发 CORS 头，默认收紧）；生产如跨域需显式配置域名白名单
```

### 3.5 日志配置

- [ ] **[可选]** 日志级别与格式

```bash
# LOG_LEVEL=INFO     ← DEBUG | INFO | WARNING | ERROR
# LOG_FORMAT=json    ← json | text
# DEBUG=false        ← 生产环境必须为 false

# 判定标准 / Pass Criteria: 生产环境 LOG_LEVEL >= INFO, DEBUG=false
```

### 3.6 熔断器配置（可选）

- [ ] **[可选]** 熔断器参数合理性（`CIRCUIT_BREAKER_ENABLED=true` 时检查）

| 参数 / Parameter | 说明 / Description | 推荐值 / Recommended |
|---|---|---|
| `CB_LLM_MAX_FAILURES` | LLM 熔断最大失败数 | 5 |
| `CB_LLM_RESET_TIMEOUT` | LLM 熔断后半开等待(秒) | 30 |

> 注：`CB_*_WINDOW_SIZE` 已随 v0.5.0 死配置收敛移除（pybreaker 用 fail_max 计数 + reset_timeout 复位，无时间窗参数）。

**启用与验证命令**：
```bash
# 单元测试验证
python -m pytest tests/unit/test_circuit_breaker.py -q

# 真实环境集成验证
python -m pytest tests/integration/test_runtime_enablement.py -q -k circuit
python -m pytest tests/integration/test_circuit_breaker_recovery.py -q
```

---

## 4. 数据库（PostgreSQL 已移除）

> PostgreSQL 运行时后端已正式移除：当前版本不建立任何数据库连接，本节原「PG 服务可达 / 数据库与用户 / 密码特殊字符 / 异步存储 / 分区与归档」检查全部不再需要。KB 经验由本地 SQLite 笔记本持久化（零配置、自动建表）。

- [ ] **[必选]** `.env` 中不存在 `STORAGE_BACKEND=postgresql`（存在则启动即拒绝）

```bash
# 判定标准 / Pass Criteria: STORAGE_BACKEND=memory（或未设置，默认 memory）
# 异常处理 / Contingency:
#   - 遗留的 PG_* / POSTGRES_PASSWORD / DATABASE_URL 键会被忽略、不影响启动，可安全删除
#   - 旧 PG kb_entries 数据迁移指引见 TROUBLESHOOTING.md L 节
```

---

## 5. Redis 连通性测试 / Redis Connectivity

> 仅当 `STATE_BACKEND=redis` 时需要执行本节。

### 5.1 Redis 服务可达

- [ ] **[必选]** Redis 服务正在运行

```bash
redis-cli -u <REDIS_URL> ping

# 判定标准 / Pass Criteria: 返回 PONG
# 异常处理 / Contingency:
#   - 检查 Redis 服务是否启动
#   - 检查 REDIS_URL 格式: redis://[password@]host:port/db
#   - Docker 部署: docker compose up -d redis
```

### 5.2 Redis 内存配置

- [ ] **[可选]** Redis 内存限制已设置

```bash
redis-cli CONFIG GET maxmemory
redis-cli CONFIG GET maxmemory-policy

# 推荐配置 / Recommended:
#   maxmemory: 256mb（单机）或根据集群规模调整
#   maxmemory-policy: allkeys-lru

# 判定标准 / Pass Criteria: maxmemory > 0 且策略合理
```

### 5.3 启用与验证测试

```bash
# 验证 Redis 状态后端与缓存集成
python -m pytest tests/integration/test_runtime_enablement.py -q -k redis
python -m pytest tests/integration/test_redis_cache_integration.py -q
```

---

## 6. 安全与权限核查 / Security & Permissions

### 6.1 鉴权配置

- [ ] **[必选]** 生产环境 API_KEY 已设置

```bash
# 验证方式: 启动服务后发送带鉴权的请求
curl -H "Authorization: Bearer <API_KEY>" http://localhost:8000/health

# 判定标准 / Pass Criteria: 返回 HTTP 200
# 不带 API_KEY 的请求应返回 HTTP 401/403
curl http://localhost:8000/health
# 判定标准 / Pass Criteria: 返回 HTTP 401 或 403
```

### 6.2 绑定地址安全

- [ ] **[必选]** 绑定地址与鉴权匹配

```bash
# 安全规则:
#   HOST 默认 127.0.0.1（源码权威默认，单用户本地定位）
#   HOST=0.0.0.0 → 必须设置 API_KEY（否则服务拒绝启动）
#   HOST=127.0.0.1 → API_KEY 可选（仅本地访问）
#   绑非回环地址后 /metrics 不再免鉴权，抓取方必须带 API Key

# 判定标准 / Pass Criteria: 符合上述安全规则
# 异常处理 / Contingency:
#   - 生产环境: 建议 HOST=0.0.0.0 + API_KEY 必须设置
#   - 开发环境: HOST=127.0.0.1 即可（默认值，可不写）
#   - 容器部署: 容器内必须 HOST=0.0.0.0（Dockerfile 与两份 compose 已设），
#     对外暴露面由 ports 的 127.0.0.1 发布地址控制
```

### 6.3 诊断端点

- [ ] **[必选]** 生产环境诊断端点已关闭

```bash
# DEBUG_ENDPOINTS_ENABLED=false  ← 生产必须为 false
# 该开关控制 /api/debug/echo 和 /api/debug/token 端点
# 判定标准 / Pass Criteria: DEBUG_ENDPOINTS_ENABLED=false
```

### 6.4 请求体限制

- [ ] **[可选]** 请求体大小限制

```bash
# MAX_BODY_SIZE=1048576  ← 默认 1MB
# 判定标准 / Pass Criteria: 值 > 0 且合理（不建议超过 10MB）
```

### 6.5 限流配置

- [ ] **[可选]** 限流参数合理性

```bash
# RATE_LIMIT_PER_MINUTE=60  ← 全局每分钟限流
# 判定标准 / Pass Criteria: 值 > 0，根据业务流量调整
```

### 6.6 UI URL 安全

- [ ] **[可选]** UI 验证 URL 安全策略

```bash
# UI_URL_ALLOW_PRIVATE=false  ← 生产环境必须为 false
# UI_URL_ALLOWLIST=           ← 白名单按需配置

# 判定标准 / Pass Criteria:
#   - 生产环境: UI_URL_ALLOW_PRIVATE=false
#   - 开发环境: 可设为 true 或添加 localhost 到白名单
```

### 6.7 鉴权安全（API Key）

- [ ] **[必选]** 鉴权配置安全性检查

```bash
# 检查项：
#   1. API_KEY / API_KEYS 已配置（未配置 = 不鉴权，仅限内网/回环使用）
#   2. 非回环绑定（如 0.0.0.0）必须配置 API Key，否则启动校验拒绝/告警
#   3. 鉴权使用 API Key 多 key 恒定时间比较轮换（hmac.compare_digest），无 JWT 依赖
# 判定标准 / Pass Criteria: 上述三项全部满足
# 说明：本项目鉴权基于 API Key（fail-closed），不引入 JWT/OAuth；历史 BETA 审查中的
#       JWT 相关项（BETA-P1-02/03/04 等）已确认为误报（无 JWT 实现）
```

### 6.8 RBAC 工具覆盖

- [ ] **[必选]** 新增 MCP 工具必须在 TOOL_ROLE_REQUIREMENTS 注册

```bash
# 验证方式：检查 app/mcp/tools/__init__.py 中 TOOL_ROLE_REQUIREMENTS 的 keys
# 是否覆盖 register_all_tools() 注册的所有工具
# 判定标准 / Pass Criteria: 两个集合完全一致
# 说明：已有 TestToolRoleRequirementsCoverage 测试对覆盖完整性做程序化校验（原 BETA-P2-12 已修复）
```

---

## 7. UI 验证环境检查 / UI Verification Environment

> 仅当需要使用 `verify_ui` 或 `auto_test` 工具时执行本节。

### 7.1 Playwright 安装

- [ ] **[必选]** Playwright 及浏览器已安装

```bash
pip install playwright
playwright install chromium

# 判定标准 / Pass Criteria: 命令执行无报错
# 异常处理 / Contingency:
#   - Linux 可能需要安装系统依赖: playwright install-deps
#   - 网络问题: 配置代理或使用镜像
```

### 7.2 目标页面可达

- [ ] **[必选]** 待验证的 Web 页面可访问

```bash
# 验证目标 URL 可访问
curl -I <target_url>

# 判定标准 / Pass Criteria: HTTP 状态码 200/301/302
# 异常处理 / Contingency:
#   - 本地页面: 确保开发服务器已启动
#   - SSRF 限制: 如果是内网地址，需设置 UI_URL_ALLOW_PRIVATE=true
#     或将主机添加到 UI_URL_ALLOWLIST
```

### 7.3 启用与验证测试

```bash
# 验证 UI 验证与浏览器真实交互
python -m pytest tests/integration/test_mcp_verify_ui.py -q
python -m pytest tests/integration/test_ui_verify_live.py -q
```

---

## 8. 可观测性检查 / Observability Check

> 仅当 `OTEL_ENABLED=true` 时需要执行本节。

### 8.1 OTLP 端点可达

- [ ] **[可选]** OTel Collector 端点可访问

```bash
# 默认 gRPC 端口 4317
# Linux/macOS:
nc -zv <otel_host> 4317
# Windows PowerShell:
Test-NetConnection -ComputerName <otel_host> -Port 4317

# 判定标准 / Pass Criteria: 端口可达
# 异常处理 / Contingency:
#   - 检查 OTel Collector 是否启动
#   - 检查 OTEL_EXPORTER_ENDPOINT 配置
#   - 如端点不可达，OTel 导出会失败但不影响主服务
```

### 8.2 Prometheus 端点

- [ ] **[可选]** `/metrics` 端点可用

```bash
# 启动服务后访问
curl http://localhost:8000/metrics

# 判定标准 / Pass Criteria: 返回 Prometheus 文本格式指标数据
# 注意: 如 METRICS_AUTH_ENABLED=true，需携带 API_KEY
```

### 8.3 启用与验证测试

```bash
# 验证 OpenTelemetry 埋点与上报集成
python -m pytest tests/unit/test_otel.py -q
python -m pytest tests/integration/test_runtime_enablement.py -q -k otel
python -m pytest tests/integration/test_otel_collector_integration.py -q
```

---

## 9. 核心功能冒烟测试 / Smoke Test

### 9.1 单元测试

- [ ] **[必选]** 单元测试全部通过

```bash
pytest tests/unit/ -q --tb=short

# 判定标准 / Pass Criteria: 全部通过（允许 skip，不允许 fail/error）
# 异常处理 / Contingency:
#   - 查看失败测试的错误信息
#   - 确认依赖版本是否正确
#   - 参考 TROUBLESHOOTING.md 排查
```

### 9.2 服务启动

- [ ] **[必选]** 服务可正常启动

```bash
python -m app.main

# 判定标准 / Pass Criteria:
#   - 日志输出 "服务启动 | lujo-mcp v0.9.5 | ..."（本地源码候选显示 0.9.5；若运行 npm 发布版则为 0.9.4）
#   - 无 ERROR 级别日志
#   - 进程未退出
# 异常处理 / Contingency:
#   - 查看启动错误日志
#   - 常见原因: 端口占用、配置错误、依赖缺失
#   - 参考 TROUBLESHOOTING.md
```

### 9.3 健康检查

- [ ] **[必选]** `/health` 端点返回正常

```bash
curl http://localhost:8000/health
# 判定标准 / Pass Criteria:
#   {"status": "ok"}    ← 公开 /health 只返回状态字段（不暴露内部配置）
# 状态说明:
#   - ok: 所有组件正常
#   - degraded: 部分组件异常（如 LLM 未配置），服务仍可用
#   - unhealthy: 核心组件异常，需要排查
```

- [ ] **[必选]** `/internal/health` 返回完整配置字段

```bash
# 完整字段（service / version / storage / llm_configured）在 /internal/health：
# 仅内网/回环可直接访问（本机 curl 免鉴权），外网需携带 API Key
curl http://localhost:8000/internal/health

# 判定标准 / Pass Criteria:
#   {
#     "status": "ok",
#     "service": "lujo-mcp",
#     "version": "0.9.5",          ← 本地源码候选显示 0.9.5；npm 发布版显示 0.9.4
#     "storage": "memory",         ← 唯一合法值（PostgreSQL 后端已移除）
#     "llm_configured": true       ← false 表示 LLM 未配置
#   }
```

### 9.4 MCP 协议端点

- [ ] **[必选]** MCP 端点可访问

```bash
# HTTP Streamable 模式
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'

# 判定标准 / Pass Criteria: 返回 JSON-RPC 响应，包含 serverInfo
# 异常处理 / Contingency:
#   - 检查 MCP 路由是否正常注册
#   - 查看服务日志中的错误信息
```

### 9.5 Dashboard

- [ ] **[可选]** Web 控制台可访问

```bash
# 浏览器访问 http://localhost:8000/dashboard
# 判定标准 / Pass Criteria: 页面正常加载，无 404 错误
```

### 9.6 Dashboard SSE 实时推送

- [ ] **[可选]** Dashboard SSE 端点可访问

```bash
curl -N -H "Authorization: Bearer <API_KEY>" "http://localhost:8000/api/dashboard/stream?api_key=<API_KEY>"
# 判定标准 / Pass Criteria: 返回 SSE 事件流（event: ping 或 event: refresh）
# 注意：需启用 dashboard_sse_enabled=true
```

### 9.7 AI Debug Agent 工具注册

- [ ] **[可选]** MCP 工具列表包含 repair_async 和 repair_result

```bash
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1,"params":{}}'
# 判定标准 / Pass Criteria: 返回的 tools 数组包含 repair_async 和 repair_result
# 注意：需启用 Agent 模式（AGENT_MODE，取值 off/single/dag/verify_loop，默认 off；
#       未显式设置时才按 agent_enabled 等历史布尔开关派生）
```

---

## 10. Docker 部署检查 / Docker Deployment

> 仅当使用 Docker Compose 部署时执行本节。

### 10.1 Docker 环境

- [ ] **[必选]** Docker 和 Docker Compose 已安装

```bash
docker --version
docker compose version

# 判定标准 / Pass Criteria: 两个命令均输出版本号
# Docker Engine >= 24.0, Docker Compose >= 2.20 推荐
```

### 10.2 环境变量

- [ ] **[必选]** Docker 必需环境变量已设置

```bash
# 以下变量在 docker-compose.yaml 中标记为必须（?语法）:
# API_KEY=<token>                 ← 服务鉴权令牌（当前唯一使用 ? 必填语法的变量）

# 判定标准 / Pass Criteria: 上述变量已设置且非空
# 异常处理 / Contingency:
#   - 未设置会导致 docker compose up 直接报错
#   - PostgreSQL 服务与 PG_PASSWORD / POSTGRES_PASSWORD 已随后端移除，不再需要
```

### 10.3 启动与验证

- [ ] **[必选]** Docker Compose 启动成功

```bash
docker compose up -d

# 检查所有服务状态
docker compose ps

# 判定标准 / Pass Criteria:
#   - redis: healthy
#   - app: healthy (或 running)
# 异常处理 / Contingency:
#   - docker compose logs app    ← 查看应用日志
#   - 确认 .env 中 API_KEY 已配置
```

---

## 快速检查脚本 / Quick Check Scripts

以下脚本可一键执行多项检查：

### Linux / macOS

```bash
#!/bin/bash
echo "=== Lujo-MCP Pre-flight Check ==="

# Python 版本
echo -n "Python: "
python --version 2>&1

# .env 存在
echo -n ".env file: "
[ -f .env ] && echo "OK" || echo "MISSING - run: cp .env.example .env"

# 核心依赖
echo -n "Dependencies: "
pip check 2>&1 | tail -1

# 端口 8000
echo -n "Port 8000: "
ss -tlnp 2>/dev/null | grep -q :8000 && echo "IN USE" || echo "AVAILABLE"

# Playwright (可选)
echo -n "Playwright: "
python -c "import playwright" 2>/dev/null && echo "INSTALLED" || echo "NOT INSTALLED (optional)"

echo "=== Check Complete ==="
```

### Windows PowerShell

```powershell
Write-Host "=== Lujo-MCP Pre-flight Check ==="

# Python 版本
Write-Host "Python: $(python --version 2>&1)"

# .env 存在
if (Test-Path .env) { Write-Host ".env file: OK" }
else { Write-Host ".env file: MISSING - run: Copy-Item .env.example .env" }

# 核心依赖
Write-Host "Dependencies: $(pip check 2>&1 | Select-Object -Last 1)"

# 端口 8000
$port = netstat -ano 2>$null | Select-String ":8000 "
if ($port) { Write-Host "Port 8000: IN USE" } else { Write-Host "Port 8000: AVAILABLE" }

# Playwright (可选)
try { python -c "import playwright" 2>$null; Write-Host "Playwright: INSTALLED" }
catch { Write-Host "Playwright: NOT INSTALLED (optional)" }

Write-Host "=== Check Complete ==="
```

---

## 异常处理预案 / Contingency Plans

### 启动失败快速恢复

| 错误现象 / Symptom | 可能原因 / Cause | 处理方案 / Resolution |
|---|---|---|
| `Refusing to start: host contains 0.0.0.0 but API_KEY is empty` | 外网监听无鉴权 | 设置 `API_KEY` 或改用 `HOST=127.0.0.1` |
| `Invalid STORAGE_BACKEND` | 配置拼写错误 | 检查 `STORAGE_BACKEND` 值，仅允许 `memory`（唯一合法值；`postgresql` 已移除，会以 `StorageBackendRemovedError` 拒绝） |
| `Address already in use` | 端口被占用 | 更换端口或终止占用进程 |
| `ModuleNotFoundError` | 依赖未安装 | `pip install -r requirements.txt` |
| `StorageBackendRemovedError` | `STORAGE_BACKEND=postgresql` | PostgreSQL 后端已移除；改回 `memory` 或删除该行，迁移指引见 TROUBLESHOOTING.md L 节 |
| `.env` 警告 `Ignored extra .env keys` | .env 含多余键 | 可忽略，不影响运行；或清理多余键（含遗留 `PG_*`） |

### 降级模式说明

服务在以下情况会进入降级模式（degraded），仍可启动但部分功能受限：

| 条件 / Condition | 影响 / Impact | 恢复方式 / Recovery |
|---|---|---|
| `OPENAI_API_KEY` 未设置 | LLM 分析功能不可用 | 配置有效的 API Key |
| Redis 不可达 + STATE_BACKEND=redis | 限流功能异常 | 恢复 Redis 连接或切换为 `STATE_BACKEND=memory` |
| OTel Collector 不可达 | 指标导出失败，不影响主服务 | 恢复 OTel Collector 或关闭 `OTEL_ENABLED` |

> 注：存储层不再有 PostgreSQL 降级路径——后端固定 memory，且 `STORAGE_BACKEND=postgresql` 会在启动时被直接拒绝（fail-fast，不静默回退）。

---

## 相关文档 / Related Documents

- [变更与发布说明 / CHANGELOG & Release Notes](./CHANGELOG.md)
- [异常排查指南 / Troubleshooting Guide](./TROUBLESHOOTING.md)
- [环境配置模板 / Environment Template](../../.env.example)
- [Docker Compose 配置](../../docker-compose.yaml)
