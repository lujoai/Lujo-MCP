# 异常排查指南 / Troubleshooting Guide

**适用版本 / Applicable Version**: v0.6.0  
**最后更新 / Last Updated**: 2026-08-21

---

## 目录 / Table of Contents

- [使用说明 / How to Use](#使用说明--how-to-use)
- [A. 启动异常 / Startup Errors](#a-启动异常--startup-errors)
- [B. 配置异常 / Configuration Errors](#b-配置异常--configuration-errors)
- [C. 存储层异常 / Storage Errors](#c-存储层异常--storage-errors)
- [D. LLM 调用异常 / LLM Errors](#d-llm-调用异常--llm-errors)
- [E. MCP 协议异常 / MCP Protocol Errors](#e-mcp-协议异常--mcp-protocol-errors)
- [F. 安全与鉴权异常 / Security & Auth Errors](#f-安全与鉴权异常--security--auth-errors)
- [G. UI 验证异常 / UI Verification Errors](#g-ui-验证异常--ui-verification-errors)
- [H. 可观测性异常 / Observability Errors](#h-可观测性异常--observability-errors)
- [I. 性能与资源异常 / Performance & Resource Errors](#i-性能与资源异常--performance--resource-errors)
- [J. Docker 部署异常 / Docker Deployment Errors](#j-docker-部署异常--docker-deployment-errors)
- [K. 测试异常 / Test Errors](#k-测试异常--test-errors)
- [通用排查流程 / General Diagnostic Flow](#通用排查流程--general-diagnostic-flow)

---

## 使用说明 / How to Use

本文档按异常类别组织，每个条目包含：**现象** → **原因** → **解决方案** → **验证方法**。

建议排查路径：
1. 根据错误日志或现象，在目录中定位对应分类
2. 找到匹配条目，按解决方案逐步操作
3. 执行验证方法确认问题已修复

如果问题未在本文档中找到，请查看[通用排查流程](#通用排查流程--general-diagnostic-flow)。

This document is organized by error category. Each entry contains: **Symptom** → **Cause** → **Solution** → **Verification**.

---

## A. 启动异常 / Startup Errors

### A-1. 服务拒绝启动：外网监听无鉴权

**现象 / Symptom**:
```
RuntimeError: Refusing to start: host contains 0.0.0.0 but API_KEY is empty.
Set API_KEY before exposing the service.
```

**原因 / Cause**: `HOST=0.0.0.0` 且 `API_KEY` 为空，服务出于安全保护拒绝启动。

**解决方案 / Solution**:
- 方案 A（推荐）: 设置 `API_KEY`
  ```bash
  # .env
  API_KEY=your_secret_token_here
  ```
- 方案 B: 仅本地开发时，改用 `HOST=127.0.0.1`

**验证 / Verify**: 服务正常启动，日志输出 `服务启动 | Lujo-MCP v0.6.0`

---

### A-2. 端口被占用

**现象 / Symptom**:
```
OSError: [Errno 98] Address already in use
# 或
error: [WinError 10048] 通常每个套接字地址(协议/网络地址/端口)只允许使用一次
```

**原因 / Cause**: 端口 8000 已被其他进程占用。

**解决方案 / Solution**:
```bash
# 查找占用进程
# Linux/macOS:
ss -tlnp | grep 8000
# Windows PowerShell:
netstat -ano | findstr :8000

# 方案 A: 终止占用进程
kill -9 <PID>    # Linux/macOS
# Stop-Process -Id <PID>  # Windows

# 方案 B: 更换端口
# .env 中修改:
PORT=8001
```

**验证 / Verify**: 服务启动成功，`curl http://localhost:<PORT>/health` 返回 200

---

### A-3. 依赖缺失

**现象 / Symptom**:
```
ModuleNotFoundError: No module named 'xxx'
```

**原因 / Cause**: Python 依赖包未安装或版本不匹配。

**解决方案 / Solution**:
```bash
# 确保虚拟环境已激活
pip install -r requirements.txt

# 验证依赖完整性
pip check
```

**常见缺失包对照表**:

| 缺失模块 / Missing Module | 对应包 / Package | 安装命令 |
|---|---|---|
| `fastapi` | fastapi | `pip install fastapi>=0.115.0` |
| `uvicorn` | uvicorn | `pip install uvicorn>=0.49.0` |
| `pydantic_settings` | pydantic-settings | `pip install pydantic-settings>=2.0.0` |
| `psycopg2` | psycopg2-binary | `pip install psycopg2-binary>=2.9.0` |
| `asyncpg` | asyncpg | `pip install asyncpg>=0.29.0` |
| `redis` | redis | `pip install redis>=5.0.0` |
| `pybreaker` | pybreaker | `pip install pybreaker>=1.0.0` |
| `opentelemetry.*` | opentelemetry-* | `pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc` |
| `playwright` | playwright | `pip install playwright && playwright install chromium` |

**验证 / Verify**: `pip check` 输出 `No broken requirements found`

---

### A-4. STORAGE_BACKEND 拼写错误

**现象 / Symptom**:
```
ValueError: Invalid STORAGE_BACKEND: 'postgrsql'. Allowed: memory, postgresql
```

**原因 / Cause**: `.env` 中 `STORAGE_BACKEND` 值拼写错误。

**解决方案 / Solution**:
```bash
# .env 中修正
STORAGE_BACKEND=postgresql   # 注意拼写：postgresql，不是 postgrsql
```

**设计说明**: 这是 fail-fast 设计，防止拼写错误导致静默降级到 memory 存储，造成生产数据丢失。

**验证 / Verify**: 服务正常启动，`/health` 返回正确的 storage 状态

---

## B. 配置异常 / Configuration Errors

### B-1. .env 文件不存在

**现象 / Symptom**:
```
Warning: .env file not found, using defaults
# 或所有配置均为默认值
```

**原因 / Cause**: 未创建 `.env` 文件。

**解决方案 / Solution**:
```bash
cp .env.example .env
# 编辑 .env 填入实际配置值
```

**验证 / Verify**: 服务启动日志显示正确的配置信息

---

### B-2. .env 含未知键的警告

**现象 / Symptom**:
```
WARNING: Ignored extra .env keys: ['SOME_OLD_KEY', 'DEPRECATED_VAR']
```

**原因 / Cause**: `.env` 中存在应用不识别的键名（可能是旧版本遗留）。

**解决方案 / Solution**:
- 可安全忽略，不影响服务运行
- 建议清理多余键以保持配置整洁

**验证 / Verify**: 警告消失（清理后）或服务正常运行（忽略时）

---

### B-3. API_KEY 为空但非 null

**现象 / Symptom**:
```
WARNING: API_KEY 为空，已视为未配置，鉴权关闭
```

**原因 / Cause**: `.env` 中 `API_KEY=` 设置为空串。

**解决方案 / Solution**:
- 开发环境: 可忽略，服务以免鉴权模式运行
- 生产环境: 必须设置有效的 API_KEY
  ```bash
  # 生成随机 API Key
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```

**验证 / Verify**: `/health` 返回 `auth: on`（生产环境）

---

### B-4. LLM 参数越界

**现象 / Symptom**:
LLM 调用行为异常（如返回过于随机的结果、频繁超时）。

**原因 / Cause**: LLM 参数配置不合理。

**解决方案 / Solution**:

| 参数 | 合理范围 | 建议值 | 问题表现 |
|---|---|---|---|
| `LLM_TEMPERATURE` | 0.0 ~ 2.0 | 0.3 | >1.5 输出混乱；=0 无创造性 |
| `LLM_TIMEOUT` | 10 ~ 300 秒 | 60 | 过短导致频繁超时 |
| `LLM_MAX_RETRIES` | 0 ~ 10 | 3 | =0 无重试容错 |

**验证 / Verify**: LLM 调用正常返回分析结果

---

## C. 存储层异常 / Storage Errors

### C-1. PostgreSQL 连接失败

**现象 / Symptom**:
```
psycopg2.OperationalError: could not connect to server: Connection refused
# 或
asyncpg.exceptions.CannotConnectNowError: ...
```

**原因 / Cause**: PostgreSQL 服务未启动或配置错误。

**排查步骤 / Diagnostic Steps**:
1. 检查 PG 服务状态
2. 验证连接参数
3. 检查网络/防火墙

**解决方案 / Solution**:
```bash
# 1. 检查 PG 服务
# Linux:
systemctl status postgresql
# Docker:
docker compose ps postgres

# 2. 验证连接参数
psql -h localhost -p 5432 -U postgres -d lujo_mcp -c "SELECT 1"

# 3. 检查 .env 配置
# PG_HOST=localhost
# PG_PORT=5432
# PG_DATABASE=lujo_mcp
# PG_USER=postgres
# PG_PASSWORD=<correct_password>

# 4. 如果 PG 确实不可用，可临时降级
# .env:
STORAGE_BACKEND=memory
# 或保持 postgresql 但确保:
STORAGE_FALLBACK_TO_MEMORY=true
```

**验证 / Verify**: `/health` 返回 `"storage": "postgresql (connected)"`

---

### C-2. PostgreSQL 认证失败

**现象 / Symptom**:
```
psycopg2.OperationalError: FATAL: password authentication failed for user "postgres"
```

**原因 / Cause**: `PG_PASSWORD` 配置错误或用户不存在。

**解决方案 / Solution**:
```bash
# 1. 确认密码正确
# 2. 如果忘记密码，重置 PG 用户密码:
#    以 postgres 系统用户执行:
psql -c "ALTER USER postgres PASSWORD 'new_password';"
# 3. 更新 .env 中 PG_PASSWORD 和 POSTGRES_PASSWORD
```

**验证 / Verify**: `psql` 可正常连接

---

### C-3. 数据库不存在

**现象 / Symptom**:
```
psycopg2.OperationalError: FATAL: database "lujo_mcp" does not exist
```

**原因 / Cause**: 目标数据库尚未创建。

**解决方案 / Solution**:
```sql
-- 连接到 PG 默认数据库
psql -h localhost -U postgres -d postgres

-- 创建目标数据库
CREATE DATABASE lujo_mcp;
```

**验证 / Verify**: `psql -d lujo_mcp -c "SELECT 1"` 成功

---

### C-4. 内存存储容量超限

**现象 / Symptom**:
日志中出现:
```
WARNING: Memory store reached max entries (10000), evicting oldest
```

**原因 / Cause**: 内存存储达到 `MEMORY_STORE_MAX_ENTRIES` 上限，旧数据被 FIFO 淘汰。

**解决方案 / Solution**:
- 这是正常行为，不是错误
- 如需增大容量:
  ```bash
  # .env
  MEMORY_STORE_MAX_ENTRIES=20000
  ```
- 长期建议: 切换到 PostgreSQL 存储

**验证 / Verify**: 服务正常运行，旧数据按预期淘汰

---

### C-5. Redis 连接失败

**现象 / Symptom**:
```
redis.exceptions.ConnectionError: Error connecting to localhost:6379
```

**原因 / Cause**: Redis 服务未启动或 `REDIS_URL` 配置错误。

**解决方案 / Solution**:
```bash
# 1. 检查 Redis 服务
redis-cli ping
# 期望返回: PONG

# 2. 如果未启动
# Linux:
systemctl start redis
# Docker:
docker compose up -d redis

# 3. 检查 REDIS_URL 格式
# redis://localhost:6379/0
# redis://:password@host:6379/0  (有密码时)

# 4. 如果不需要 Redis，切换为内存后端
# .env:
STATE_BACKEND=memory
```

**验证 / Verify**: `redis-cli ping` 返回 `PONG`

---

## D. LLM 调用异常 / LLM Errors

### D-1. API Key 无效

**现象 / Symptom**:
```
openai.AuthenticationError: Error code: 401 - Incorrect API key provided
```

**原因 / Cause**: `OPENAI_API_KEY` 无效或已过期。

**解决方案 / Solution**:
1. 验证 API Key 格式正确（`sk-` 开头）
2. 到 OpenAI 控制台确认 Key 状态
3. 如使用自定义 `LLM_BASE_URL`，确认端点地址和认证方式

**验证 / Verify**: 发送调试请求，LLM 正常返回分析结果

---

### D-2. LLM 调用超时

**现象 / Symptom**:
```
openai.APITimeoutError: Request timed out
```

**原因 / Cause**: LLM 响应时间超过 `LLM_TIMEOUT` 设置。

**解决方案 / Solution**:
```bash
# .env 中增大超时时间
LLM_TIMEOUT=120    # 从默认 30 秒增加到 120 秒
```

**验证 / Verify**: LLM 调用在超时时间内返回

---

### D-3. LLM 模型不可用

**现象 / Symptom**:
```
openai.NotFoundError: Error code: 404 - The model 'xxx' does not exist
```

**原因 / Cause**: `LLM_MODEL` 指定的模型不存在或账户无权访问。

**解决方案 / Solution**:
```bash
# .env 中更换为可用模型
LLM_MODEL=gpt-4o           # 或 gpt-4o-mini, gpt-4-turbo 等
LLM_FALLBACK_MODEL=gpt-4o-mini
```

**验证 / Verify**: 服务启动后 LLM 调用正常

---

### D-4. 熔断器触发

**现象 / Symptom**:
日志中出现:
```
WARNING: LLM circuit breaker OPEN after 5 failures in 60s window
```

**原因 / Cause**: LLM 调用在滑动窗口内连续失败超过阈值，熔断器打开。

**解决方案 / Solution**:
1. 先排查根本原因（网络？API Key？模型不可用？）
2. 等待 `CB_LLM_RESET_TIMEOUT`（默认 30 秒）后半开状态自动试探
3. 如需临时关闭熔断器:
   ```bash
   CIRCUIT_BREAKER_ENABLED=false
   ```

**验证 / Verify**: 熔断器恢复关闭状态，LLM 调用成功

---

### D-5. 自定义 LLM Provider 配置

**现象 / Symptom**: LLM 调用返回 404 或连接错误。

**原因 / Cause**: 使用 `LLM_PROVIDER=custom` 或 `zhipu` 时 base_url 配置不正确。

**解决方案 / Solution**:

| Provider | 自动设置的 base_url | 推荐模型 |
|---|---|---|
| `openai` | `https://api.openai.com/v1` | gpt-4o |
| `zhipu` | `https://open.bigmodel.cn/api/paas/v4/` | glm-4-flash |
| `deepseek` | `https://api.deepseek.com` | deepseek-chat |
| `custom` | 需手动设置 `LLM_BASE_URL` | 自定义 |

```bash
# 自定义 provider 示例
LLM_PROVIDER=custom
LLM_BASE_URL=https://your-api-endpoint.com/v1
LLM_MODEL=your-model-name
OPENAI_API_KEY=your-key-for-this-endpoint
```

**验证 / Verify**: LLM 调用正常返回

---

## E. MCP 协议异常 / MCP Protocol Errors

### E-1. MCP 初始化失败

**现象 / Symptom**:
```json
{"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid Request"}, "id": 1}
```

**原因 / Cause**: JSON-RPC 请求格式不正确。

**解决方案 / Solution**:
确保请求符合 JSON-RPC 2.0 规范:
```json
{
  "jsonrpc": "2.0",
  "method": "initialize",
  "id": 1,
  "params": {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "your-client", "version": "1.0"}
  }
}
```

**常见错误**:
- 缺少 `jsonrpc` 字段 → Parse Error (-32700)
- `method` 字段缺失 → Invalid Request (-32600)
- 调用不存在的方法 → Method Not Found (-32601)

**验证 / Verify**: 返回包含 `serverInfo` 的正确响应

---

### E-2. MCP 工具调用超时

**现象 / Symptom**:
```json
{"isError": true, "content": "_timed_out"}
```

**原因 / Cause**: 工具执行时间超过 `TOOL_TIMEOUT_SECONDS`。

**解决方案 / Solution**:
```bash
# .env 中增大工具超时
TOOL_TIMEOUT_SECONDS=120    # 默认 60 秒
# UI 验证类工具建议:
TOOL_TIMEOUT_SECONDS=300    # verify_ui / auto_test 可能需要更长时间
```

**验证 / Verify**: 工具在超时时间内完成执行

---

### E-3. SSE 连接中断

**现象 / Symptom**: MCP SSE 长连接意外断开。

**原因 / Cause**: 网络不稳定或代理/负载均衡器超时。

**解决方案 / Solution**:
1. 检查网络稳定性
2. 如使用反向代理，确保 SSE 不被缓冲:
   ```nginx
   # Nginx 示例
   location /mcp {
     proxy_buffering off;
     proxy_cache off;
     proxy_read_timeout 3600s;
   }
   ```
3. 客户端实现重连逻辑

**验证 / Verify**: SSE 连接稳定，`GET /mcp` 保持长连接

---

### E-4. 工具未注册

**现象 / Symptom**:
```json
{"jsonrpc": "2.0", "error": {"code": -32601, "message": "Method not found: tools/call xxx"}, "id": 1}
```

**原因 / Cause**: 请求的 MCP 工具未注册。

**解决方案 / Solution**:
查看已注册工具列表:
```bash
# 发送 tools/list 请求
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1,"params":{}}'
```

已注册的 18 个工具:
`debug`, `context`, `trace`, `stacktrace`, `ingest_network`, `get_network_trace`, `get_blame_for_frame`, `get_recent_diff`, `ingest_silent_failure`, `ingest_error`, `ingest_console`, `get_related_specs`, `verify`, `verify_ui`, `auto_test`, `repair_async`, `repair_result`, `resolve_stack`

**验证 / Verify**: `tools/list` 返回完整工具列表

---

## F. 安全与鉴权异常 / Security & Auth Errors

### F-1. 401 Unauthorized

**现象 / Symptom**: HTTP 401 响应。

**原因 / Cause**: 请求未携带有效的鉴权信息。

**解决方案 / Solution**:
```bash
# 方式 1: Bearer Token
curl -H "Authorization: Bearer <API_KEY>" http://localhost:8000/health

# 方式 2: X-API-Key Header
curl -H "X-API-Key: <API_KEY>" http://localhost:8000/health

# 方式 3: Query Parameter
curl "http://localhost:8000/health?api_key=<API_KEY>"
```

**注意**: `/` 和 `/health` 端点不需要鉴权。

**验证 / Verify**: 携带正确 API_KEY 的请求返回 200

---

### F-2. 429 Too Many Requests

**现象 / Symptom**: HTTP 429 响应。

**原因 / Cause**: 请求频率超过限流阈值。

**解决方案 / Solution**:
1. 降低请求频率
2. 如需调高限流:
   ```bash
   # .env
   RATE_LIMIT_PER_MINUTE=120    # 默认 60
   ```

**端点级限流默认值**:

| 端点 / Endpoint | 限制 / Limit |
|---|---|
| `/ingest/` | 120 次 / 60 秒 |
| `/api/debug/analyze` | 10 次 / 60 秒 |
| `/api/debug/verify/ui` | 5 次 / 60 秒 |
| 其他端点 | `RATE_LIMIT_PER_MINUTE` / 60 秒 |

**验证 / Verify**: 等待限流窗口过期后请求成功

---

### F-3. 413 Payload Too Large

**现象 / Symptom**: HTTP 413 响应。

**原因 / Cause**: 请求体超过 `MAX_BODY_SIZE` 限制。

**解决方案 / Solution**:
```bash
# .env 中增大限制
MAX_BODY_SIZE=2097152    # 默认 1MB，改为 2MB
```

**验证 / Verify**: 请求体在限制范围内的请求成功

---

### F-4. CORS 跨域问题

**现象 / Symptom**: 浏览器控制台报 CORS 错误。

**原因 / Cause**: 未配置 `CORS_ORIGINS` 或配置不匹配。

**解决方案 / Solution**:
```bash
# .env

# 方式 1: 允许所有来源（不带凭证）
CORS_ORIGINS=*

# 方式 2: 指定白名单（带凭证）
CORS_ORIGINS=https://example.com,https://app.example.com
```

**注意**: 默认不配置 `CORS_ORIGINS` 时不下发 CORS 头，跨域请求会被浏览器拦截。

**验证 / Verify**: 浏览器跨域请求成功，响应包含 `Access-Control-Allow-Origin` 头

---

## G. UI 验证异常 / UI Verification Errors

### G-1. Playwright 浏览器未安装

**现象 / Symptom**:
```
Executable doesn't exist at ...
```

**原因 / Cause**: Playwright Chromium 浏览器未安装。

**解决方案 / Solution**:
```bash
playwright install chromium

# Linux 可能需要系统依赖:
playwright install-deps chromium
```

**验证 / Verify**: `playwright install --dry-run` 无报错

---

### G-2. SSRF 拦截：URL 被拒绝

**现象 / Symptom**:
```
ValueError: URL blocked by SSRF protection: http://localhost:3000
```

**原因 / Cause**: 目标 URL 为内网地址，而 `UI_URL_ALLOW_PRIVATE=false`。

**解决方案 / Solution**:
```bash
# .env

# 方案 A: 允许所有私网地址（开发环境）
UI_URL_ALLOW_PRIVATE=true

# 方案 B: 仅白名单特定主机
UI_URL_ALLOWLIST=localhost,127.0.0.1,192.168.1.100
```

**安全提醒**: 生产环境必须保持 `UI_URL_ALLOW_PRIVATE=false`。

**验证 / Verify**: UI 验证请求可正常访问目标 URL

---

### G-3. 页面元素定位失败

**现象 / Symptom**: UI 验证断言失败，报告元素未找到。

**原因 / Cause**: 页面结构与断言预期不匹配。

**解决方案 / Solution**:
1. 确认页面已完全加载（可能需要等待异步渲染）
2. 检查 CSS 选择器是否正确
3. 使用 Dashboard (`/dashboard`) 查看验证详情

**常见断言类型与选择器要求**:

| 断言类型 / Assertion | 选择器要求 / Selector |
|---|---|
| `text_content` | 任意元素 |
| `element_exists` | 任意元素 |
| `form` | input/textarea/select 的 name 或 label |
| `data_table` | table 元素 |
| `numeric_range` | 包含数值的文本元素 |

**验证 / Verify**: 修正选择器后断言通过

---

### G-4. 表单断言 (form) 失败

**现象 / Symptom**: `form` 类型断言返回值与预期不匹配。

**原因 / Cause**: 表单字段名称或值不匹配。

**解决方案 / Solution**:
1. 确认 `expected_values` 中的键名与表单字段的 `name` 属性或关联 `<label>` 文本一致
2. checkbox/radio 类型使用 `true`/`false` 作为期望值
3. select 类型使用选项的 `value` 属性值

**验证 / Verify**: 修正期望值后断言通过

---

## H. 可观测性异常 / Observability Errors

### H-1. OTel 导出失败

**现象 / Symptom**: 日志中出现 OTLP gRPC 导出错误。

**原因 / Cause**: OTel Collector 不可达或配置错误。

**解决方案 / Solution**:
```bash
# 1. 检查 OTel Collector 状态
# 2. 验证端点配置
# OTEL_EXPORTER_ENDPOINT=http://localhost:4317

# 3. 如不需要 OTel，关闭
# OTEL_ENABLED=false

# 注意: OTel 导出失败不影响主服务运行
```

**验证 / Verify**: OTel 指标正常导出，或关闭后服务正常运行

---

### H-2. /metrics 端点返回空数据

**现象 / Symptom**: `/metrics` 返回但无指标数据。

**原因 / Cause**: 服务刚启动，尚无请求产生指标。

**解决方案 / Solution**: 发送几个请求后再次访问 `/metrics`。

**验证 / Verify**: 有请求后 `/metrics` 返回 Prometheus 格式指标

---

### H-3. /metrics 鉴权失败

**现象 / Symptom**: 访问 `/metrics` 返回 401。

**原因 / Cause**: `METRICS_AUTH_ENABLED=true` 但未携带 API_KEY。

**解决方案 / Solution**:
```bash
curl -H "X-API-Key: <API_KEY>" http://localhost:8000/metrics
```

**验证 / Verify**: 携带 API_KEY 后返回指标数据

---

## I. 性能与资源异常 / Performance & Resource Errors

### I-1. 内存使用过高

**现象 / Symptom**: 进程内存占用持续增长。

**原因 / Cause**: 内存存储积累了大量数据。

**解决方案 / Solution**:
```bash
# 1. 降低内存存储上限
# MEMORY_STORE_MAX_ENTRIES=5000

# 2. 缩短 TTL
# TRACE_TTL_SECONDS=1800
# SESSION_TTL_SECONDS=1800

# 3. 切换到 PostgreSQL 存储
# STORAGE_BACKEND=postgresql
```

**验证 / Verify**: 内存使用稳定在合理范围

---

### I-2. 响应延迟增大

**现象 / Symptom**: API 响应时间明显增加。

**原因 / Cause**: 可能原因包括 PG 连接池耗尽、LLM 超时、大量并发请求。

**排查步骤 / Diagnostic Steps**:
1. 检查 `/metrics` 中的平均延迟
2. 查看日志中是否有慢请求
3. 检查 PG 连接池状态

**解决方案 / Solution**:
```bash
# 调整 PG 连接池
# PG_ASYNC_MAX=30    # 增大异步连接池

# 调整 LLM 超时
# LLM_TIMEOUT=120

# 启用熔断器防止级联故障
# CIRCUIT_BREAKER_ENABLED=true
```

**验证 / Verify**: 响应延迟恢复到正常水平

---

## J. Docker 部署异常 / Docker Deployment Errors

### J-1. docker compose up 报错：变量未设置

**现象 / Symptom**:
```
Error: POSTGRES_PASSWORD must be set
# 或
Error: PG_PASSWORD must be set
# 或
Error: API_KEY must be set
```

**原因 / Cause**: `docker-compose.yaml` 中使用 `?` 语法要求必须设置的环境变量缺失。

**解决方案 / Solution**:
```bash
# .env 中设置以下必需变量:
POSTGRES_PASSWORD=your_pg_password
PG_PASSWORD=your_pg_password        # 建议与 POSTGRES_PASSWORD 一致
API_KEY=your_api_key
```

**验证 / Verify**: `docker compose up -d` 成功启动所有服务

---

### J-2. 容器健康检查失败

**现象 / Symptom**: `docker compose ps` 显示 app 为 `unhealthy`。

**原因 / Cause**: 应用启动失败或 `/health` 端点返回异常。

**解决方案 / Solution**:
```bash
# 查看应用日志
docker compose logs app

# 常见原因:
# 1. PG 连接失败 → 检查 PG 容器是否 healthy
# 2. 配置错误 → 检查 .env 传递
# 3. 依赖缺失 → 检查 Dockerfile 构建过程
```

**验证 / Verify**: `docker compose ps` 所有服务显示 `healthy`

---

### J-3. PG 密码不一致

**现象 / Symptom**: 应用日志显示 PG 认证失败，但 postgres 容器正常。

**原因 / Cause**: `POSTGRES_PASSWORD`（PG 初始化用）与 `PG_PASSWORD`（应用连接用）不一致。

**解决方案 / Solution**:
```bash
# .env 中确保两者一致:
POSTGRES_PASSWORD=same_password
PG_PASSWORD=same_password
```

**注意**: 如果修改了已运行的 PG 密码，需要删除 volume 重新初始化:
```bash
docker compose down -v    # 警告: 会删除所有数据！
docker compose up -d
```

**验证 / Verify**: `/health` 返回 `"storage": "postgresql (connected)"`

---

## K. 测试异常 / Test Errors

### K-1. 单元测试失败

**现象 / Symptom**:
```
pytest tests/unit/ → FAILED
```

**原因 / Cause**: 代码变更导致测试不通过，或依赖版本不兼容。

**解决方案 / Solution**:
```bash
# 1. 查看具体失败信息
pytest tests/unit/ -v --tb=long

# 2. 运行单个失败测试以聚焦排查
pytest tests/unit/test_xxx.py::test_name -v

# 3. 确认依赖版本
pip check

# 4. 清除缓存重新运行
pytest tests/unit/ --cache-clear -q
```

**验证 / Verify**: 所有单元测试通过

---

### K-2. 集成测试需要外部服务

**现象 / Symptom**: 集成测试因 PG/Redis 不可用而跳过或失败。

**原因 / Cause**: 集成测试需要真实的外部服务。

**解决方案 / Solution**:
```bash
# 方案 A: 启动 Docker 服务
docker compose up -d postgres redis

# 方案 B: 仅运行不依赖外部服务的测试
pytest tests/unit/ -q

# 方案 C: 跳过需要外部服务的测试
pytest tests/integration/ -q -m "not requires_pg and not requires_redis"
```

**验证 / Verify**: 测试按预期通过或跳过

---

### K-3. Playwright 测试超时

**现象 / Symptom**: UI 相关测试超时失败。

**原因 / Cause**: Chromium 启动慢或测试页面加载超时。

**解决方案 / Solution**:
```bash
# 1. 确保 Chromium 已安装
playwright install chromium

# 2. 增加测试超时
pytest tests/ --timeout=120

# 3. CI 环境可能需要 headed 模式调试
# 设置环境变量:
# PLAYWRIGHT_HEADLESS=false
```

**验证 / Verify**: UI 测试在超时时间内完成

---

## 通用排查流程 / General Diagnostic Flow

当遇到的问题不在上述分类中时，按以下流程排查:

```
1. 查看服务日志
   ├── LOG_FORMAT=json → 解析 JSON 日志，关注 level=ERROR
   └── LOG_FORMAT=text → 搜索 ERROR / Exception / Traceback

2. 检查健康状态
   └── curl http://localhost:8000/health
       ├── status=ok → 服务正常，问题在特定功能
       ├── status=degraded → 部分组件异常，查看 storage/llm_configured
       └── status=unhealthy → 核心组件异常

3. 检查配置
   ├── .env 是否存在且格式正确
   ├── 关键变量是否设置（API_KEY, OPENAI_API_KEY）
   └── 参考 PREFLIGHT_CHECKLIST.md 逐项检查

4. 检查依赖
   ├── pip check → 依赖完整性
   └── pip list → 版本确认

5. 运行测试
   └── pytest tests/unit/ -q → 代码层面验证

6. 查看指标
   └── curl http://localhost:8000/metrics → 请求统计/延迟/错误率
```

### 日志级别调整

临时调高日志级别以获取更多信息:
```bash
# .env
LOG_LEVEL=DEBUG
DEBUG=true    # 仅开发环境！
```

### 获取帮助

1. 查阅 [发布说明 / Release Notes](./RELEASE_NOTES.md) 了解版本变更
2. 查阅 [启动前检查清单 / Pre-flight Checklist](./PREFLIGHT_CHECKLIST.md) 全面检查环境
3. 查阅内部文档了解功能详情与已知问题

---

## 异常索引速查表 / Quick Reference Index

| 错误码/现象 | 分类 | 条目 | 快速解决 |
|---|---|---|---|
| `Refusing to start` | A 启动 | A-1 | 设置 API_KEY |
| `Address already in use` | A 启动 | A-2 | 换端口或杀进程 |
| `ModuleNotFoundError` | A 启动 | A-3 | `pip install -r requirements.txt` |
| `Invalid STORAGE_BACKEND` | A 启动 | A-4 | 修正拼写 |
| `.env` 警告 | B 配置 | B-2 | 可忽略 |
| PG 连接失败 | C 存储 | C-1 | 检查 PG 服务与配置 |
| PG 认证失败 | C 存储 | C-2 | 修正密码 |
| Redis 连接失败 | C 存储 | C-5 | 检查 Redis 服务 |
| LLM 401 | D LLM | D-1 | 检查 API Key |
| LLM 超时 | D LLM | D-2 | 增大 LLM_TIMEOUT |
| 熔断器打开 | D LLM | D-4 | 排查根因后等待恢复 |
| JSON-RPC -32600 | E MCP | E-1 | 修正请求格式 |
| 工具超时 | E MCP | E-2 | 增大 TOOL_TIMEOUT_SECONDS |
| 401 Unauthorized | F 安全 | F-1 | 携带 API_KEY |
| 429 Rate Limit | F 安全 | F-2 | 降低频率或调高限流 |
| 413 Too Large | F 安全 | F-3 | 增大 MAX_BODY_SIZE |
| CORS 错误 | F 安全 | F-4 | 配置 CORS_ORIGINS |
| SSRF 拦截 | G UI | G-2 | 配置 UI_URL_ALLOWLIST |
| Playwright 缺失 | G UI | G-1 | `playwright install chromium` |
| OTel 导出失败 | H 可观测 | H-1 | 检查 Collector 或关闭 |
| Docker 变量缺失 | J Docker | J-1 | 设置必需环境变量 |
| 测试失败 | K 测试 | K-1 | 查看错误信息定位 |
