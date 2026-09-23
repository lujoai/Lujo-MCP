# 异常排查指南 / Troubleshooting Guide

**适用版本 / Applicable Version**: v0.9.4
**最后更新 / Last Updated**: 2026-09-23

> **发布状态**：默认 `STORAGE_BACKEND=memory`（唯一合法值；PostgreSQL 后端已正式移除，精确值 `postgresql` 会被直接拒绝，见 L 节）；KB 经验默认写穿本地 SQLite「笔记本」（`KB_PERSIST_ENABLED=true`，路径 `KB_PERSIST_PATH`，默认工作目录 `lujo-kb.sqlite3`）。

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
- [L. PostgreSQL 后端移除说明 / PostgreSQL Removal](#l-postgresql-后端移除说明--postgresql-removal)
- [M. 使用误区 / Common Misunderstandings](#m-使用误区--common-misunderstandings)
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

**验证 / Verify**: 服务正常启动，日志输出 `服务启动 | lujo-mcp v0.9.4`

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

> **多项目同机调试提示 / Multi-project note**: 若占用 8000 端口的是另一个 Lujo-MCP 实例（同时调试多个项目），不要终止它——按「端口即隔离」为本项目改用独立端口（MCP 配置追加 `--http-port`，页面 SDK `endpoint` 指向同端口），详见 README「🛠️ 进阶开发与私有化部署」的「多项目同机调试」小节。

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
| `redis` | redis | `pip install redis>=5.0.0` |
| `pybreaker` | pybreaker | `pip install pybreaker>=1.0.0` |
| `opentelemetry.*` | opentelemetry-* | `pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc` |
| `playwright` | playwright | `pip install playwright && playwright install chromium` |

**验证 / Verify**: `pip check` 输出 `No broken requirements found`

---

### A-4. STORAGE_BACKEND 拼写错误或指向已移除的 PostgreSQL

**现象 / Symptom**:
```
ValueError: Invalid STORAGE_BACKEND='postgrsql'. Valid values: ['memory']. ...
# 精确值 postgresql 则是：
StorageBackendRemovedError: STORAGE_BACKEND=postgresql 已被拒绝：PostgreSQL 运行时后端已正式移除...
```

**原因 / Cause**: `.env` 中 `STORAGE_BACKEND` 值拼写错误，或仍保留着旧版的 `postgresql` 配置（该后端已移除）。

**解决方案 / Solution**:
```bash
# .env 中改回唯一合法值
STORAGE_BACKEND=memory
# 或直接删除该行（memory 是默认值）
```

**设计说明**: 这是 fail-fast 设计，防止拼写错误导致静默降级到 memory 存储，造成生产数据丢失。精确值 `postgresql` 不会被静默改写为 memory，而是启动即拒绝（详见 L 节）。

**验证 / Verify**: 服务正常启动，`/internal/health` 返回 `"storage": "memory"`（公开 `/health` 仅返回 status）

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

**验证 / Verify**: 配置后重启不再出现「鉴权关闭」告警；无 key 请求受保护端点返回 401（生产环境）

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

### C-1. 配置了已移除的 PostgreSQL 后端（原「PostgreSQL 连接失败」等三节的统一入口）

> v0.9.0 起 PostgreSQL 运行时后端已正式移除，原 C-1（连接失败）/ C-2（认证失败）/ C-3（数据库不存在）三节描述的问题不再可能出现——当前版本不建立任何 PG 连接。本节保留为升级指引。

**现象 / Symptom**:
```
StorageBackendRemovedError: STORAGE_BACKEND=postgresql 已被拒绝：PostgreSQL 运行时后端已正式移除...
```
stdio/统一模式进程在启动阶段即非零退出（stdout 保持纯 MCP 协议、错误只落 stderr）；HTTP 模式 lifespan 启动失败。

**原因 / Cause**: `.env` 或环境变量中仍保留 `STORAGE_BACKEND=postgresql`（旧版配置）。

**解决方案 / Solution**:
```bash
# .env 中改回唯一合法值（或删除该行，memory 是默认值）
STORAGE_BACKEND=memory
```
旧 PG kb_entries 数据的迁移见 L 节；遗留的 `PG_*` / `POSTGRES_PASSWORD` / `DATABASE_URL` 键不会重新启用 PostgreSQL，也不会导致启动崩溃，可安全删除。

**验证 / Verify**: 服务正常启动，`/internal/health` 返回 `"storage": "memory"`（公开 `/health` 仅返回 status）

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
- 长期建议: 运行现场本就是进程内数据，按需调大上限即可；KB 经验由本地 SQLite 笔记本独立持久化，不受该上限影响

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

`tools/list` 公开 18 个 Agent-facing 工具（v0.9.4；SDK 上报类 `ingest_*` 不进清单但可按名调用，注册总数 22）:
`debug`, `context`, `trace`, `stacktrace`, `diagnose_issue`, `list_recent_traces`, `search_logs`, `ingest_specs`, `get_network_trace`, `get_blame_for_frame`, `get_recent_diff`, `get_related_specs`, `verify`, `verify_ui`, `auto_test`, `repair_async`, `repair_result`, `resolve_stack`

**验证 / Verify**: `tools/list` 返回完整工具列表

---

### E-6. 工具执行器繁忙拒绝（TOOL_BUSY）

**现象 / Symptom**:
同步工具调用在有界并发槽位打满时返回业务级错误（JSON-RPC `result`，非顶层 JSON-RPC `error`）：
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "工具执行队列已满，请稍后重试。"
      }
    ],
    "isError": true,
    "error_code": "TOOL_BUSY",
    "_busy": true
  }
}
```

**原因 / Cause**:
高并发或高负载场景下，同步工具执行器的有界槽位（`tool_executor_workers`，默认 8）全部被占用。请求在等待 `tool_busy_queue_timeout`（默认 1.5s）后仍未获取到可用槽位；若 `tool_busy_queue_timeout=0` 则无可用槽位时立即拒绝（Fast-Fail）。

> **说明**：此错误属于工具调用结果（`result`）内的业务错误标记（`isError: true` + `error_code: "TOOL_BUSY"`），并非服务端协议级的顶层 JSON-RPC `error` 对象。

**解决方案 / Solution**:
1. 调大同步工具执行器并发槽位上限：
   ```env
   TOOL_EXECUTOR_WORKERS=16  # 默认 8
   ```
2. 调整获取槽位的等待超时阈值（秒）：
   ```env
   TOOL_BUSY_QUEUE_TIMEOUT=3.0  # 默认 1.5 秒；0 为立即拒绝
   ```
3. 客户端识别 `error_code: "TOOL_BUSY"` 后实施稍后重试或指数退避重试（Exponential Backoff）。

**验证 / Verify**: 查看服务端日志，确认未再出现 `工具 <tool_name> 执行队列已满（不等待，立即拒绝），已拒绝执行` 或 `工具 <tool_name> 执行队列已满（等待 ...s 后超时），已拒绝执行` 警告，客户端调用顺利完成。

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
```

**验证 / Verify**: 内存使用稳定在合理范围

---

### I-2. 响应延迟增大

**现象 / Symptom**: API 响应时间明显增加。

**原因 / Cause**: 可能原因包括 LLM 超时、大量并发请求、Redis 不可达时的限流退化。

**排查步骤 / Diagnostic Steps**:
1. 检查 `/metrics` 中的平均延迟
2. 查看日志中是否有慢请求

**解决方案 / Solution**:
```bash
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
Error: API_KEY must be set
```

**原因 / Cause**: `docker-compose.yaml` 中使用 `?` 语法要求必须设置的环境变量缺失。

**解决方案 / Solution**:
```bash
# .env 中设置必需变量:
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
# 1. 配置错误（如 .env 仍写着 STORAGE_BACKEND=postgresql）→ 检查 .env 传递
# 2. 依赖缺失 → 检查 Dockerfile 构建过程
# 3. Redis 不可达（STATE_BACKEND=redis 时）→ 检查 redis 容器是否 healthy
```

**验证 / Verify**: `docker compose ps` 所有服务显示 `healthy`

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

**现象 / Symptom**: 集成测试因缺少外部服务或 LLM API Key 而跳过。

**原因 / Cause**: 集成测试需要真实的外部服务或 LLM Key。

**解决方案 / Solution**:
```bash
# 方案 A: 启动 Redis（仅 STATE_BACKEND=redis 相关测试需要）
docker compose up -d redis

# 方案 B: 仅运行不依赖外部服务的测试
pytest tests/unit/ -q

# 方案 C: 跳过需要 LLM API Key 的测试（llm 是 pytest.ini 已注册的 marker）
pytest tests/integration/ -q -m "not llm"
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

## L. PostgreSQL 后端移除说明 / PostgreSQL Removal

> **节状态（Step 3 收口）**：PostgreSQL 运行时后端已**正式移除**。本节取代原「实验性后端」节；历史内容（本地连接修复记录等）已随后端一并移除——旧版本用户可查阅 v0.8.x 的文档存档。

### 最终 STORAGE_BACKEND 语义

- 唯一合法值为 **`memory`**（默认）：运行现场（traces/errors/sessions/specs）存于进程内存，重启即清；
- KB 调试经验由**本地 SQLite「笔记本」**持久化（`KB_PERSIST_ENABLED=true` 默认开启，路径 `KB_PERSIST_PATH`，默认工作目录 `lujo-kb.sqlite3`），进程重启自动回灌——这是**唯一**的 KB 持久化路径；
- 精确值 `postgresql` 会被**启动即拒绝**（`StorageBackendRemovedError`，`RuntimeError` 子类）：stdio/统一模式进程非零退出（stdout 保持纯 MCP 协议、错误只落 stderr），HTTP 模式 lifespan 启动失败；**不会静默回退 memory**；
- 大小写变体（如 `PostgreSQL`）与拼写错误（如 `postgrsql`）仍走通用 `ValueError` 非法配置，消息含 case-sensitive 提示与移除说明；
- 旧部署 `.env` 遗留的 `PG_*` / `POSTGRES_PASSWORD` / `DATABASE_URL` 键**不会重新启用 PostgreSQL**，也**不会让配置构造阶段崩溃**——它们会被直接忽略（启动日志仅打印忽略的键名），可安全删除。

### 旧 PG kb_entries 数据迁移（v0.8.x 及更早版本的用户）

- **已完成迁移**：在 v0.8.x 执行过一次性迁移脚本（`scripts/migrate_pg_kb_to_sqlite.py`，支持 `--dry-run` 核对）的用户不受影响——经验已在 SQLite 笔记本中，升级后继续直接使用；
- **尚未迁移**：当前版本**不再附带**该一次性迁移脚本；仍需迁移旧 PG `kb_entries` 数据的用户，请先回退 v0.8.x 执行迁移后再升级（该脚本在 v0.8.x 交付，此为唯一路径）；
- **无迁移路径**：traces / errors / sessions / specs **不迁移**——运行现场在当前版本回到 memory-only（重启即清），历史错误计数不随升级延续。

### 历史已知问题（B16–B18/U08）与移除的关系

原实验性后端节列出的已知未修问题（KB 延迟初始化失败可能无法降级、错误计数被节流压低、调度失败后的节流记账抑制有效写入、stdio asyncpg 连接池关闭路径未验证）**已随 PG runtime 代码路径的删除而消失——这是「能力移除」，不代表这些缺陷曾被修复**。仍在 v0.8.x 及更早版本上显式配置 `STORAGE_BACKEND=postgresql` 的用户，这些问题依然存在。

---

## M. 使用误区 / Common Misunderstandings

> 这些条目不是服务故障，而是对 Lujo 分工与工具语义的常见误解。先对照本节，再进入具体异常分类排查。

### M-1. MCP 面板显示已连接，但 AI 查不到浏览器运行现场

- **现象**：宿主 IDE 的 MCP 面板里 Lujo 显示「已连接 / 工具可用」，但让 AI 查页面报错时返回 `found=false` 或空结果。
- **原因**：Lujo 有两条独立链路——①宿主智能体 → MCP → Lujo（工具调用）；②被调试网页 → Browser SDK → Lujo HTTP `/ingest` → memory runtime（现场采集）。MCP 连接只证明第①条链路可用；**没有第②条链路时，Lujo 不会自动知道页面里发生了什么**。
- **解决方案**：
  1. 确认 Lujo HTTP 采集端点在监听（npm 统一模式默认 `http://127.0.0.1:8000`；纯 stdio `--no-http` 模式不接收浏览器上报）。
  2. 确认页面已加载 Browser SDK 且 `AiDebug.init({ endpoint: ... })` 的 endpoint 指向**当前项目对应的 Lujo 实例和端口**（多项目并行时各用不同 `--http-port`，见 README「端口即隔离」）。
  3. 按 C-4/F-4 排查内存与 CORS 后，在页面里复现问题，再回宿主会话查询。
- **验证方法**：浏览器 DevTools Network 面板能看到发往 endpoint 的 `/ingest/batch` 请求且返回 200；随后 `diagnose_issue({})` 能返回现场。
- **附带说明**：当前 runtime 默认 memory，现场保存在 Lujo 进程内存中，**Lujo 进程重启后旧现场可能消失**。正确顺序是保持同一 Lujo 进程运行 → 复现问题 → 等待采集完成（秒级）→ 立即查询。

### M-2. `diagnose_issue` 带 query 查不到，但不带 query 能查到

- **现象**：`diagnose_issue({"query": "登录按钮"})` 返回 `found=false`，而 `diagnose_issue({})` 或 `list_recent_traces` 能看到错误记录。
- **原因**：`query` 是对近期错误 **`type` / `message` 字段的关键词过滤**，不是自然语言全字段检索，不保证匹配 selector、trace 元数据或所有上下文字段；错误超出 `since_minutes`（默认 30 分钟）时间窗时也不会命中。**query 未命中不等于 Lujo 没有现场**。
- **解决方案**（推荐回退顺序）：
  1. `diagnose_issue({})` —— 先读最近一次错误；
  2. `list_recent_traces` —— 列出近期全部错误摘要；
  3. 按返回的 `trace_id` / `request_id` 调 `context` / `trace` / `stacktrace` / `get_network_trace` 深挖。
- **验证方法**：按上述顺序第 1 步即能取回现场；若需要关键词检索，改用与错误 type/message 实际文案一致的关键词（如异常类型名、接口路径片段）。

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
       ├── status=degraded → 部分组件异常，用 /internal/health 查看 storage/llm_configured
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

1. 查阅 [变更与发布说明 / CHANGELOG](./CHANGELOG.md) 了解版本演进与最新修复
2. 查阅 [启动前检查与功能启用综合手册 / Pre-flight Checklist](./PREFLIGHT_CHECKLIST.md) 全面检查环境
3. 查阅 [API_REFERENCE.md](./API_REFERENCE.md) 与 [SDK_GUIDE.md](./SDK_GUIDE.md) 核实接口契约
4. 在 GitHub Issues 提交问题反馈与复现现场日志

---

## 异常索引速查表 / Quick Reference Index

| 错误码/现象 | 分类 | 条目 | 快速解决 |
|---|---|---|---|
| `Refusing to start` | A 启动 | A-1 | 设置 API_KEY |
| `Address already in use` | A 启动 | A-2 | 换端口或杀进程 |
| `ModuleNotFoundError` | A 启动 | A-3 | `pip install -r requirements.txt` |
| `Invalid STORAGE_BACKEND` | A 启动 | A-4 | 修正拼写 |
| `.env` 警告 | B 配置 | B-2 | 可忽略 |
| `StorageBackendRemovedError` | C 存储 | C-1 | 改用 `memory` |
| Redis 连接失败 | C 存储 | C-5 | 检查 Redis 服务 |
| LLM 401 | D LLM | D-1 | 检查 API Key |
| LLM 超时 | D LLM | D-2 | 增大 LLM_TIMEOUT |
| 熔断器打开 | D LLM | D-4 | 排查根因后等待恢复 |
| JSON-RPC -32600 | E MCP | E-1 | 修正请求格式 |
| 工具超时 | E MCP | E-2 | 增大 TOOL_TIMEOUT_SECONDS |
| 工具繁忙拒绝 (TOOL_BUSY) | E MCP | E-6 | 增大 TOOL_EXECUTOR_WORKERS / TOOL_BUSY_QUEUE_TIMEOUT |
| 401 Unauthorized | F 安全 | F-1 | 携带 API_KEY |
| 429 Rate Limit | F 安全 | F-2 | 降低频率或调高限流 |
| 413 Too Large | F 安全 | F-3 | 增大 MAX_BODY_SIZE |
| CORS 错误 | F 安全 | F-4 | 配置 CORS_ORIGINS |
| SSRF 拦截 | G UI | G-2 | 配置 UI_URL_ALLOWLIST |
| Playwright 缺失 | G UI | G-1 | `playwright install chromium` |
| OTel 导出失败 | H 可观测 | H-1 | 检查 Collector 或关闭 |
| Docker 变量缺失 | J Docker | J-1 | 设置必需环境变量 |
| 测试失败 | K 测试 | K-1 | 查看错误信息定位 |
