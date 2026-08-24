# Lujo-MCP API 参考手册

> 版本：v0.6.0（2026-08-21）
> 本文档覆盖 Lujo-MCP 对外暴露的两类接口：**REST API** 与 **MCP 工具**。
> 接口清单以代码为准；启动后可用 `GET /mcp`（非 SSE）查看协议元信息，`GET /health` 查看运行状况。

---

## 目录

- [1. 鉴权与角色（RBAC）](#1-鉴权与角色rbac)
- [2. REST API](#2-rest-api)
  - [2.1 调试 `/api/debug`](#21-调试-apidebug)
  - [2.2 数据接入 `/ingest`](#22-数据接入-ingest)
  - [2.3 控制台 `/api/dashboard`](#23-控制台-apidashboard)
  - [2.4 规范 CRUD `/api/spec`](#24-规范-crud-apispec)
  - [2.5 MCP 传输 `/mcp`](#25-mcp-传输-mcp)
  - [2.6 令牌签发 `/auth`](#26-令牌签发-auth)
- [3. MCP 工具（18 个）](#3-mcp-工具18-个)
  - [3.1 查询 / 分析类工具（agent）](#31-查询--分析类工具agent)
  - [3.2 数据采集类工具（sdk）](#32-数据采集类工具sdk)
  - [3.3 实验工具（experimental）](#33-实验工具experimental)
- [4. 常用字段速查](#4-常用字段速查)

---

## 1. 鉴权与角色（RBAC）

Lujo-MCP 采用 **fail-closed（默认拒绝）** 的 API Key 鉴权：

- 请求头：`Authorization: Bearer <key>` 或 `X-API-Key: <key>`（二者等价，`Authorization: Bearer` 优先）。
- 仅当 `sendBeacon` / `EventSource` 等无法自定义 header 的场景，允许 `?token=<beacon短时令牌>` 或 `?api_key=` 查询参数降级（不推荐长期使用）。
- 未配置任何 `API_KEY` 时 = 不鉴权（仅限内网/回环使用；绑定非回环地址会启动校验拒绝或告警）。

三角色分级（`RBAC_ENABLED=true` 时生效）：

| 角色 | 权限 |
|------|------|
| `admin` | 完全控制（含诊断端点 `/api/debug/echo`、`/api/debug/token`） |
| `developer` | 读 + 写（调试、修复、验证、规范 CRUD、数据接入） |
| `viewer` | 只读（Dashboard、上下文查询、trace 查询、修复结果轮询） |

- `RBAC_ENABLED=false`（默认）时，所有 key 视为 `admin`（向后兼容）。
- 未在 `RBAC_ROLE_MAPPING` 中映射的 key，默认 `viewer`（fail-closed，防配置遗漏越权）。

---

## 2. REST API

> 每个端点标注了所需最低角色。请求/响应均为 JSON；`Content-Type: application/json`。

### 2.1 调试 `/api/debug`

| 方法 | 路径 | 角色 | 说明 |
|------|------|------|------|
| POST | `/api/debug/run` | developer | 执行调试流程：记录请求 → 处理 → 构建上下文 |
| POST | `/api/debug/analyze` | developer | 对指定 request_id 做 LLM 根因分析 |
| POST | `/api/debug/analyze/stream` | developer | 流式 LLM 分析（SSE） |
| POST | `/api/debug/analyze/async` | developer | 异步 LLM 分析（削峰队列，返回 job_id） |
| GET | `/api/debug/analyze/result/{job_id}` | viewer | 查询异步分析任务状态/结果 |
| POST | `/api/debug/repair/async` | developer | 异步生成修复方案（AI Debug Agent，需 `AGENT_ENABLED=true`） |
| GET | `/api/debug/repair/result/{job_id}` | viewer | 查询异步修复任务状态/结果 |
| GET | `/api/debug/runtime` | viewer | 获取当前进程运行时快照（CPU/内存/线程） |
| GET | `/api/debug/session` | viewer | 列出活跃调试会话 |
| POST | `/api/debug/verify` | developer | 比对实际结果 vs 期望规范，检测静默失败 |
| POST | `/api/debug/verify/ui` | developer | Playwright 按 UI 规范自动遍历并验证（FR14） |
| GET | `/api/debug/health` | viewer | 调试健康检查（`{"status":"ok","timestamp":...}`） |
| GET | `/api/debug/prompt?request_id={id}` | viewer | 生成纯文本调试提示词（FR12，非 MCP 场景一键复制；模板可经 `PROMPT_TEMPLATE_PATH` 自定义） |
| POST | `/api/debug/sourcemap` | developer | 上传 Source Map（v0.5.1，需 `SOURCEMAP_ENABLED=true`） |
| POST | `/api/debug/echo` | admin | 回显请求体（需 `DEBUG_ENDPOINTS_ENABLED=true`） |
| GET | `/api/debug/token` | admin | 探测响应脱敏的测试端点（需 `DEBUG_ENDPOINTS_ENABLED=true`） |

**`POST /api/debug/run` 示例**：

```json
// 请求
{ "payload": { "user_id": 1 }, "metadata": { "trace_kind": "debug" } }

// 响应（节选）
{
  "request_id": "req-xxx",
  "result": { "echo": { "user_id": 1 }, "status": "success" },
  "trace": [ { "timestamp": ..., "step": "request_start", "data": {...} } ],
  "context": { ... }
}
```

**`POST /api/debug/verify` 示例**：

```json
// 请求：actual 为实际结果，spec 为期望（或传 spec_id）
{
  "actual": { "status_code": 200, "body": {"data": null} },
  "spec": { "kind": "api", "target": "POST /login",
            "expect": { "body": { "data": {"type": "object", "required": true} } } },
  "trace_id": "req-xxx"
}

// 响应
{ "matched": false, "diffs": [{ "field": "body.data", "expected": "object", "actual": null }], "silent_failure": true }
```

**`GET /api/debug/prompt?request_id=req-xxx` 示例**（FR12，v0.5.5）：

```json
// 响应：prompt 为可一键复制的纯文本提示词
{
  "request_id": "req-xxx",
  "prompt": "你是一位资深排障专家。以下是程序运行时的调试上下文，请分析并定位问题根因：\n\n== 调试上下文（request_id: req-xxx）==\n\n请求 ID: req-xxx\n执行流程: request_start → processing → error\n异常详情: {\n  \"type\": \"ValueError\",\n  \"message\": \"bad value\",\n  ...\n}\n\n请基于以上上下文给出分析结论，包括：..."
}
```

> 说明：`prompt` 字段内容 = 完整调试上下文（异常帧/源码片段/运行时/git 归因等）经脱敏 + 截断后的纯文本渲染；可直接粘贴给任意 AI 助手。默认使用内置模板，可用 `PROMPT_TEMPLATE_PATH` 指定自定义模板文件（占位符 `$context` / `$request_id`）。

### 2.2 数据接入 `/ingest`

> 供 Browser SDK / 外部服务 / 非 Python 运行时直接上报原始数据，入库前统一脱敏。

| 方法 | 路径 | 角色 | 说明 |
|------|------|------|------|
| POST | `/ingest/network` | developer | 单条网络请求记录上报 |
| GET | `/ingest/network/{trace_id}` | viewer | 查询某 trace 关联的网络记录 |
| POST | `/ingest/silent-failure` | developer | 上报前端静默失败（含 UI 事件链 + 网络链） |
| POST | `/ingest/error` | developer | 任意语言主动上报异常（不限于 Python） |
| POST | `/ingest/console` | developer | 上报浏览器控制台日志 |
| POST | `/ingest/ui-event` | developer | 上报 UI 交互事件 |
| POST | `/ingest/batch` | developer | 批量上报（事件数组，单次 ≤100 条，支持 gzip） |

**`POST /ingest/batch` 示例**（V5，支持 `Content-Encoding: gzip`）：

```json
{
  "events": [
    { "path": "/ingest/error", "payload": { "exc_type": "TypeError", "message": "...", "frames": [] } },
    { "path": "/ingest/network", "payload": { "record": { "method": "GET", "url": "/api/x" } } }
  ]
}
// 响应
{ "results": [ { "path": "/ingest/error", "ok": true, "result": {...} }, ... ], "count": 2 }
```

### 2.3 控制台 `/api/dashboard`

| 方法 | 路径 | 角色 | 说明 |
|------|------|------|------|
| GET | `/api/dashboard/stats` | viewer | 控制台概览统计（trace 数 / 静默失败数 / 异常数 / 规范数） |
| GET | `/api/dashboard/traces?limit=100` | viewer | 列出最近 traces（limit 1–1000） |
| GET | `/api/dashboard/trace/{trace_id}` | viewer | trace 详情（含 spec_diffs + quality_report） |
| GET | `/api/dashboard/trace/{trace_id}/quality` | viewer | 单独获取 trace 质量报告 |
| GET | `/api/dashboard/specs` | viewer | 列出所有已存规范 |
| GET | `/api/dashboard/stream` | viewer | Dashboard 实时 SSE 推送（需 `DASHBOARD_SSE_ENABLED=true`） |
| GET | `/api/dashboard/errors/aggregated` | viewer | 按指纹聚合错误统计 |
| GET | `/api/dashboard/errors/ranked` | viewer | 按影响程度排序错误 |
| GET | `/api/dashboard/errors/history` | viewer | 查询错误历史（PG 长期历史，PG 不可用返回空） |

### 2.4 规范 CRUD `/api/spec`

| 方法 | 路径 | 角色 | 说明 |
|------|------|------|------|
| POST | `/api/spec` | developer | 创建一条期望规范 |
| GET | `/api/spec?kind=&target=` | viewer | 列出规范（可按 kind/target 过滤） |
| GET | `/api/spec/{spec_id}` | viewer | 取一条规范 |
| PATCH | `/api/spec/{spec_id}` | developer | 部分更新规范（id 不可修改） |
| DELETE | `/api/spec/{spec_id}` | developer | 删除一条规范 |

### 2.5 MCP 传输 `/mcp`

符合 MCP Streamable HTTP 规范：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/mcp` | 客户端 → 服务端消息（`initialize` / `tools/list` / `tools/call` / 通知） |
| GET | `/mcp` | `Accept: text/event-stream` 时返回 SSE 长连接；否则返回服务健康信息 |
| DELETE | `/mcp` | 终止会话（携带 `Mcp-Session-Id`） |

会话经 `Mcp-Session-Id` 响应头维护；`initialize` 总是新建会话（防会话固定）。

### 2.6 令牌签发 `/auth`

| 方法 | 路径 | 角色 | 说明 |
|------|------|------|------|
| POST | `/auth/beacon-token` | viewer | 换取短时 beacon 令牌（供 `sendBeacon`/`EventSource` 场景，避免永久 Key 进 URL） |

---

## 3. MCP 工具（18 个）

> 工具经 HTTP（`POST /mcp` → `tools/call`）或 stdio 传输调用。HTTP 传输下受 RBAC 工具级门控（见每项「角色」）。
> 类别含义：`agent` = 供 AI Agent 调用的查询/分析/验证类；`sdk` = 供 Browser SDK 上报的数据采集类。

### 3.1 查询 / 分析类工具（agent）

| 工具名 | 角色 | 说明 |
|--------|------|------|
| `debug` | developer | 执行完整调试流程，返回结构化调试上下文 |
| `context` | viewer | 按 request_id 获取调试上下文（流程/输入输出/错误） |
| `trace` | viewer | 获取请求完整原始追踪日志 |
| `stacktrace` | viewer | 捕获当前异常或指定请求的堆栈（含源码片段） |
| `get_network_trace` | viewer | 查询某 trace 关联的网络请求记录 |
| `get_blame_for_frame` | viewer | 查询文件/行最后一次的修改 commit（git blame） |
| `get_recent_diff` | viewer | 返回文件最近 N 次 commit 的 diff |
| `get_related_specs` | viewer | 按文件路径返回相关项目规范片段 |
| `verify` | developer | 比对实际结果 vs 期望规范，检测静默失败 |
| `verify_ui` | developer | Playwright 按 UI 规范自动遍历并验证 |

**关键参数 / 返回**：

| 工具 | 入参（必填标 *） | 返回要点 |
|------|-----------------|----------|
| `debug` | `payload`*(object), `metadata`(object) | `request_id`, `result`, `trace`, `context` |
| `context` | `request_id`*(string) | 结构化上下文 + `code_snippets` |
| `trace` | `request_id`*(string) | `trace`(时序列表), `step_count` |
| `stacktrace` | `request_id`(string, 可选) | `exception`, `code_snippets`, `ai_summary` |
| `get_network_trace` | `trace_id`*(string) | `found`, `count`, `records` |
| `get_blame_for_frame` | `file`*(string), `line`*(int) | `found`, `blame` |
| `get_recent_diff` | `file`*(string), `commits_back`(int=3) | `found`, `diff` |
| `get_related_specs` | `file`*(string) | `found`, `count`, `specs` |
| `verify` | `actual`*(object), `spec` 或 `spec_id`(二选一), `trace_id`(可选) | `matched`, `diffs`, `silent_failure` |
| `verify_ui` | `spec` 或 `spec_id`(二选一), `timeout_ms`(int=30000) | `matched`, `diffs`, `silent_failure`, `interactions[]`, `security?` |

### 3.2 数据采集类工具（sdk）

| 工具名 | 角色 | 说明 |
|--------|------|------|
| `ingest_network` | developer | 单条网络请求记录上报 |
| `ingest_silent_failure` | developer | 上报前端静默失败（UI 事件链 + 网络链 + 期望描述） |
| `ingest_error` | developer | 任意语言主动上报异常 |
| `ingest_console` | developer | 上报浏览器控制台日志 |

**关键参数**：

| 工具 | 入参（必填标 *） |
|------|-----------------|
| `ingest_network` | `record`*(object), `trace_id`, `request_id` |
| `ingest_silent_failure` | `message`*(string), `frames`[], `ui_events`[], `network_records`[], `expectation`, `observed`, `observed_events`[], `source` |
| `ingest_error` | `exc_type`*(string), `message`*(string), `frames`[], `source`, `extra` |
| `ingest_console` | `message`*(string), `level`(error/warn/info), `source`, `extra`, `trace_id`, `request_id` |

### 3.3 实验工具（experimental）

> `experimental=true`：接口可能变更，需显式环境/开关启用。

| 工具名 | 角色 | 说明 | 前置条件 |
|--------|------|------|----------|
| `auto_test` | developer | 自动遍历页面可交互元素并捕获控制台错误 + 网络 4xx/5xx | Playwright |
| `repair_async` | developer | 异步生成修复方案（AI Debug Agent） | `AGENT_MODE` 非 off（或旧配置 `AGENT_ENABLED=true`） |
| `repair_result` | viewer | 查询 repair_async 任务状态/结果 | `AGENT_MODE` 非 off（或旧配置 `AGENT_ENABLED=true`） |
| `resolve_stack` | viewer | 用 Source Map 还原 minified 堆栈 | `SOURCEMAP_ENABLED=true` + 已上传 .map |

**关键参数**：

| 工具 | 入参（必填标 *） |
|------|-----------------|
| `auto_test` | `url`*(string), `max_actions`(int=20), `capture_console`(bool=true), `capture_network`(bool=true) |
| `repair_async` | `request_id`*(string) 或 `trace_id`(二选一) |
| `repair_result` | `job_id`*(string) |
| `resolve_stack` | `frames`*(array, 帧含 file/line/column/function), `artifact`(string) |

### 3.4 工具执行控制与错误码

- **背压与并发控制**：同步工具调用通过有界工作线程池执行（`TOOL_EXECUTOR_WORKERS`，默认 8）。排队等待超过 `TOOL_BUSY_QUEUE_TIMEOUT`（默认 1.5s；设为 0 时立即拒绝）将快速返回错误，避免请求在过载时无限堆积。
- **错误码约定**：

| JSON-RPC Code | error_code | 说明 | 处理建议 |
|---------------|------------|------|----------|
| `-32004` | `TOOL_BUSY` | 同步工具执行器达到容量上限且等待超时（或 timeout=0 立即拒绝） | 客户端指数退避重试或调大 `TOOL_EXECUTOR_WORKERS` |
| `-32000` | `TOOL_TIMEOUT` | 工具执行超过 `TOOL_TIMEOUT_SECONDS`（默认 60s） | 检查目标操作或适当调大超时阈值 |
| `-32602` | `INVALID_PARAMS` | 工具入参校验失败（Pydantic 校验不通过） | 检查参数类型与必填项 |
| `-32603` | `INTERNAL_ERROR` | 工具执行内部未捕获异常 | 检查服务日志与堆栈 |

---

## 4. 常用字段速查

| 字段 | 含义 |
|------|------|
| `request_id` | 一次调试流程的唯一标识（`/api/debug/run`、`debug` 工具生成） |
| `trace_id` | SDK 生命周期内的追踪标识（贯穿所有上报），也可作为 request_id 关联 |
| `error_id` | 自动捕获异常的唯一标识（`errors` 表主键） |
| `fingerprint` | 错误指纹（归一化 message + type），用于知识库精确命中去重 |
| `silent_failure` | 静默失败标志：`matched=false` 且无异常、无 4xx/5xx |
| `spec_diffs` | 规范比对差异列表（expected vs actual） |
| `quality_report` | v0.4.0 QualityScorer 9 维度评分报告 |

---

## 相关文档

- [README.md](../../README.md) — 项目总览与快速启动
- [DESIGN.md](./DESIGN.md) — 技术设计
- [KNOWLEDGE_BASE.md](./KNOWLEDGE_BASE.md) — 知识库（RAG 经验积累）
- [SDK_GUIDE.md](./SDK_GUIDE.md) — 浏览器 SDK 使用手册
- [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) — 异常排查