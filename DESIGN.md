# ai-debug-mcp 技术设计文档（DESIGN）

> 本文档描述 ai-debug-mcp 的**实现设计**：系统架构、模块职责、关键流程、数据模型、接口契约、设计决策与待设计项。
> 配套文档：产品需求文档 `PRD.md`（回答"做什么/为什么"），本文档回答"怎么做"。
> 版本：v0.3.0｜设计状态：✅ 已落地 / ⚠️ 已写待补完 / 🔲 设计草案（待实现）
> 审阅视角：高级工程师 / 高级架构师
>
> **v0.3.1 更新**：PostgreSQL 集成完成、Dashboard 读取 PG、MCP Tools 读取 PG、LLM 分析端到端验证、集成测试补充（13 用例）

---

## 1. 设计目标与原则

| 目标 | 设计落点 |
| --- | --- |
| 把运行时数据转为 AI 可消费的结构化上下文 | Trace Log → Context Builder → LLM/宿主 AI |
| 零手工整理（不手写提示词） | **宿主 AI 推理模式**：服务只交付结构化原始数据，推理交给 Trae/Codex/Claude |
| 不漏掉未处理异常 | `exception_hook` 全局捕获（sync + asyncio） |
| 安全可部署 | fail-closed 鉴权 + 限流 + 流式请求体限制 + 安全头 |
| 双形态接入 | Streamable HTTP（远程）+ stdio（本地子进程），共用同一份业务逻辑 |
| 可降级 | 快照采集/LLM 故障不阻断主流程 |

---

## 2. 系统架构

### 2.1 分层架构

```
┌─────────────────────────────────────────────────────────────┐
│  客户端层                                                     │
│  MCP 客户端 (Trae/Codex/Claude Desktop) │ REST 调用方 │ 浏览器 │
└───────────────┬───────────────────────────┬─────────────────┘
                │ JSON-RPC 2.0               │ HTTP/JSON
        ┌───────▼────────┐          ┌─────────▼──────────┐
        │ 传输层          │          │ 传输层              │
        │ stdio 子进程    │          │ Streamable HTTP     │
        │ (mcp_server.py) │          │ POST/GET(SSE)/DELETE│
        └───────┬────────┘          │ (/mcp, mcp_routes)  │
                │                   └─────────┬──────────┘
                └───────────────┬──────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────┐
│ 中间件层 (middleware.py)                                      │
│ Auth → MaxBodySize → RateLimit → SecurityHeaders → Trace      │
└───────────────────────────────┬─────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────┐
│ 路由/分发层                                                   │
│ /api/debug/* (debug.py) │ /mcp (mcp_routes.py) │ /health /metrics │
│ JSON-RPC 分发 (protocol/server.py → dispatch_raw)            │
└───────────────────────────────┬─────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────┐
│ 调试引擎 (Engine)                                             │
│ logs(trace) │ context builder │ stacktrace │ code_locator ✅  │
│ runtime snapshot │ llm analyzer │ exception_hook            │
└───────────────────────────────┬─────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────┐
│ 存储/状态 (Storage)                                           │
│ trace_store(memory/pg) │ session registry │ state store(memory/redis) │
│ sse hub (广播) │ specs ✅(FR15, spec_store)                   │
└───────────────────────────────┬─────────────────────────────┘
                                ▼
                          OpenAI API
```

### 2.2 组件关系图（Mermaid）

```mermaid
flowchart TB
    Client["MCP 客户端 / REST 调用方"]
    subgraph T["传输层"]
        STDIO["stdio (mcp_server.py)"]
        HTTP["Streamable HTTP (mcp_routes.py)"]
    end
    MW["中间件链 (middleware.py)"]
    subgraph R["路由/分发"]
        DR["/api/debug (debug.py)"]
        MR["/mcp (mcp_routes.py)"]
        DISP["JSON-RPC dispatch (protocol/server.py)"]
        TOOLS["MCP 工具集"]
    end
    subgraph E["调试引擎"]
        LOGS["logs: create/add/get"]
        BUILD["context builder"]
        ST["stacktrace collector"]
        LOC["code_locator ✅"]
        RT["runtime snapshot"]
        AN["llm analyzer"]
        HOOK["exception_hook ✅"]
    end
    subgraph S["存储/状态"]
        TS["trace_store"]
        REG["session registry"]
        SS["state store"]
        HUB["sse hub"]
    end
    LLM["OpenAI API"]

    Client --> T --> MW --> R
    MR --> DISP --> TOOLS
    TOOLS --> LOGS --> BUILD
    TOOLS --> ST --> LOC
    TOOLS --> RT
    DR --> BUILD --> AN
    BUILD --> AN
    HOOK --> LOGS
    LOGS --> TS
    REG --> MW
    SS --> MW
    DISP --> HUB
    AN --> LLM
```

---

## 3. 模块设计

### 3.1 传输层

#### 3.1.1 Streamable HTTP（`app/api/mcp_routes.py`）✅

实现 MCP Streamable HTTP 规范，单路由 `/mcp` 支持三种方法：

| 方法 | 行为 | 关键逻辑 |
| --- | --- | --- |
| `POST /mcp` | 收 JSON-RPC（initialize/tools/list/tools/call/通知） | 预解析 JSON 取 `method`/`id`/`Mcp-Session-Id`；`initialize` 时 `registry.create()` 下发 `Mcp-Session-Id`；非 initialize 无有效会话 → 400；分发 `dispatch_raw(raw)`；按 `Accept` 决定返回 JSON 或 SSE |
| `GET /mcp` | SSE 推送通道 / 健康检查 | `Accept: text/event-stream` 且会话有效 → 订阅 `hub.subscribe(session_id)` 长连推送；否则返回 `_health_payload()` |
| `DELETE /mcp` | 终止会话 | `registry.delete(session_id)` → 204 |

**会话模型**：`Mcp-Session-Id` 头贯穿；`registry.mark_initialized()` 标记初始化完成；未初始化访问 `tools/*` 被拒（400）。定时清理 TTL=1800s（`main.py` lifespan）。

**SSE 广播**：`app/mcp/transports/sse.py` 的 `hub` 提供 `subscribe/format_event/unsubscribe`，用于服务端→客户端主动推送。

#### 3.1.2 stdio（`app/mcp_server.py` + `app/mcp/transports/stdio.py`）✅

- `mcp_server.py`：标准 MCP Server，用 `mcp` SDK 的 `stdio_server` 通信，`list_tools()` 维护 **14 个工具**清单（handler 层复用工具模块的业务函数），`call_tool()` 分发。
- 注册方式：客户端配置 `{"command":"python","args":["-m","app.mcp_server"],"cwd":"<abs>"}`。
- `--stdio` 入口：`python -m app.main --stdio`（`main.py` `__main__`）。

**MCP 工具总览（HTTP 15 个 / stdio 14 个，业务实现共用）**：

| 工具 | 说明 | 实现 |
| --- | --- | --- |
| `debug` | 调试入口 | `debug_api.py` |
| `get_debug_context` | **核心**：trace+runtime+源码片段 | `context_api.py` |
| `search_logs` | 按关键字+时间窗搜历史 trace | `trace_api.py` |
| `list_recent_traces` | 最近错误摘要列表 | `trace_api.py` |
| `get_stacktrace` | 最近/指定异常堆栈（文件/行/函数） | `stacktrace_api.py` |
| `get_runtime_snapshot` | CPU/内存/线程/Python 版本 | `runtime.py` |
| `analyze_with_llm` | 可选：内置 LLM 分析 | `analyzer.py` |
| `ingest_network` / `get_network_trace` | 网络请求采集 | `network_api.py` |
| `git_blame` / `git_recent_diff` | Git 代码追溯 | `git_api.py` |
| `silent_failure` | 静默失败检测 | `silent_failure_api.py` |
| `ingest_error` | 错误上报 | `ingest_api.py` |
| `related_specs` | 相关规范查询 | `spec_api.py` |
| `verify` | 规范断言验证 | `verify_api.py` |
| `verify_ui` | 前端 UI 验证 | `verify_ui_api.py` |
| `auto_test` | 页面自动遍历 | `auto_test_api.py` |

#### 3.1.3 双传输一致性

HTTP 传输经 `register_all_tools()`（`app/mcp/tools/__init__.py`）注册 **15 个工具**；stdio 传输（`mcp_server.py`）当前维护**独立的 14 工具清单**，工具名与 HTTP 侧存在差异（如 `context` vs `get_debug_context`、`stacktrace` vs `get_stacktrace`），但 handler 层复用同一批工具模块业务函数，业务逻辑不重复。两侧注册表完全统一列为待办。

### 3.2 中间件层（`app/middleware.py`）✅

注册顺序（外→内）：`CORS → Auth → MaxBodySize → RateLimit → SecurityHeaders → Trace`。

| 中间件 | 机制 | 设计要点 |
| --- | --- | --- |
| `AuthMiddleware` | Bearer / X-API-Key；`hmac.compare_digest` 恒定时间比较；`PUBLIC_PATHS=(/,/health,/metrics)` | **fail-closed**：无 Key 且 `api_key` 已设 → 401；未设 `api_key` 则整体禁用（启动告警） |
| `MaxBodySizeMiddleware` | 先查 `Content-Length` 硬拒；POST/PUT/PATCH **流式分块读取**，超 `max_body_size` 立即 413，避免整 body 进内存 | 防 DoS/OOM |
| `RateLimitMiddleware` | `state_store.allow("ratelimit:{ip}", per_minute, 60)`；异常降级放行 | 按客户端 IP |
| `SecurityHeadersMiddleware` | 补 `X-Content-Type-Options`/`X-Frame-Options`/`Referrer-Policy`/`X-XSS-Protection` | — |
| `TraceMiddleware` | 注入 `trace_id` → `X-Trace-Id`/`X-Response-Time`；异常记录 `trace_id` | 请求级可观测 |

### 3.3 路由/分发层

#### 3.3.1 REST 调试 API（`app/api/debug.py`）✅

| 端点 | 逻辑 |
| --- | --- |
| `POST /api/debug/run` | `create_request_id` → `add_log(request_start/processing/response_ready)` → `build_context` → 出错则 `capture_exception` 挂 `context["exception"]` → `DebugResponse` |
| `POST /api/debug/analyze` | `get_logs`→404 校验→`build_context`→`collect_runtime_snapshot`(失败降级)→`analyze()`；LLM 失败 502 |
| `POST /api/debug/analyze/stream` | 同上，SSE `data:{chunk}` / `[DONE]` |
| `GET /api/debug/runtime` | `collect_runtime_snapshot()` |
| `GET /api/debug/session` | `session_manager.list_active()` 含 idle 时长 |

#### 3.3.2 JSON-RPC 分发（`app/mcp/protocol/server.py` + `jsonrpc.py`）✅

`dispatch_raw(raw)` 解析 JSON-RPC 2.0，按 `method` 路由到工具 `handler`；`make_error(id, code, msg)` 统一错误；`PROTOCOL_VERSION`/`CAPABILITIES` 声明能力。错误不回显内部堆栈（仅"见服务端日志"）。

### 3.4 调试引擎

#### 3.4.1 Trace Log 管理（`app/mcp/core/logs.py`）✅

- `create_request_id()` → 唯一 ID。
- `add_log(request_id, step, data)` → 时序追加，步骤含 `request_start`/`processing`/`response_ready`/`error` 及 MCP 专用 `mcp_*`。
- `get_logs(request_id)` → 按序取回。
- 持久化由 `trace_store` 后端（`memory`/`postgresql`）承担；TTL 清理（`main.py` `periodic_cleanup`，300s 周期）。

#### 3.4.2 Context Builder（`app/mcp/builders/context.py`）✅

`build_context(request_id, logs)` → `{request_id, flow, input, output, errors}`。单条格式异常 `try/except` 标记为 `<malformed>` 并跳过，**不阻断整体**。

> ⚠️ 注意：此构建器现已含 `code_snippets`（FR11 已接线）。详见 §6.1。

#### 3.4.3 Stacktrace Collector（`app/mcp/collectors/stacktrace.py`）✅

`capture_exception(exc)` → `{type, message, traceback, frames[], frame_count}`；每帧 `{file, line, function, code, locals}`。`format_trace_for_ai()` 生成精简文本（含局部变量前 N 个）。

#### 3.4.4 Code Locator（`app/mcp/collectors/code_locator.py`）✅ 已接线

- `get_code_snippet(file, line, context_lines)`：用 `linecache` 读取 `line±context_lines` 行，报错行以 `>>> N: ` 标注；文件读不到返回 `found=False`。
- `get_snippets_for_frames(frames)`：批量处理堆栈帧。
- **已修复**：`config.py` 已增加 `code_context_lines`、`source_path_map`、`ide_scheme`、`whitelist_path_prefix`。

#### 3.4.5 Runtime Snapshot（`app/mcp/collectors/runtime.py`）✅

`collect_runtime_snapshot()`（psutil）→ `RuntimeSnapshot{pid, cpu_percent, memory_mb, thread_count, open_files, python_version, env_hint}`。失败降级（不抛未捕获异常）。

#### 3.4.6 LLM Analyzer（`app/llm/analyzer.py`）✅

- `SYSTEM_PROMPT`：要求模型输出 JSON `{root_cause, impact, fix, confidence}`。
- `build_analysis_prompt(context)`：把 context 拼为文本（调试/展示用）。
- `truncate_context(context, max_tokens)`：运行时快照/异常帧/整体按字符数（`max_tokens*3`）截断，超长标记 `_truncated`。
- `_retry_call(...)`：重试（`llm_max_retries`）+ 指数退避 + 限流/超时处理；耗尽切换到 `llm_fallback_model`（缩短 prompt 重试 1 次）；仍失败抛 `RuntimeError`。
- `analyze(context)` / `analyze_stream(context)`：非流式/流式；流式用 SSE 逐块 yield。

#### 3.4.7 全局异常钩子（`app/mcp/hooks/exception_hook.py`）✅

`install_global_hook()`（幂等）：
- 覆盖 `sys.excepthook` → 未捕获同步异常自动 `capture_exception(exc, source="global_hook")`。
- 覆盖 asyncio loop `set_exception_handler` → 未 await 的协程异常同样捕获。
- FastAPI 请求内异常由 `middleware`/路由层单独捕获（Starlette 会吞部分异常）。
- 捕获逻辑自身 `try/except` 包裹，绝不掩盖原始报错。

> 价值：直接消解用户"手动查日志"负担——任何未处理异常自动入库，宿主 AI 用 `list_recent_traces` 即可见。

### 3.5 存储/状态层

| 组件 | 职责 | 实现 |
| --- | --- | --- |
| `trace_store` | trace/session 持久化 | `memory` / `postgresql`（工厂 `storage/factory.py`） |
| `session registry` | MCP `Mcp-Session-Id` 会话生命周期 | `transports/session.py`，TTL 1800s |
| `state store` | 限流/计数 | `memory` / `redis` |
| `sse hub` | 服务端→客户端广播 | `transports/sse.py` |
| `specs` ✅ | 规范存储（FR15） | `app/mcp/verifier/spec_store.py`，dict+Lock 主存 + add_log 持久化，预留 PG 工厂模式待迁移 |

#### 3.5.1 PostgreSQL 存储实现（`app/mcp/core/storage/pg_store.py`）✅

**连接池**：`psycopg2.pool.ThreadedConnectionPool`（minconn=2, maxconn=10），线程安全，全局单例。

**自动建表**：`_ensure_init()` 启动时执行 `CREATE TABLE IF NOT EXISTS`：
- `traces`（id BIGSERIAL, request_id TEXT, timestamp DOUBLE PRECISION, step TEXT, data JSONB）
- `sessions`（session_id TEXT PRIMARY KEY, created_at DOUBLE PRECISION, last_active DOUBLE PRECISION, metadata JSONB）

**数据序列化**：`save_entry` 统一用 `json.dumps(data, ensure_ascii=False, default=str)` 序列化，支持 dict/list/str/int/float/bool/None；`get_entries` 用 `_parse_data` 安全反序列化（JSON 字符串→对象，非 JSON 字符串→原样返回）。

**重试机制**：`_execute_with_retry` 包装 SQL 执行，捕获 `OperationalError` 自动重连重试。

**查询接口**：`list_request_ids(limit)` 返回最近写入的 request_id 列表（按 timestamp 倒序），供 Dashboard 和 MCP Tools 使用。

#### 3.5.2 Dashboard API（`app/api/dashboard.py`）✅

| 端点 | 功能 | 数据源 |
| --- | --- | --- |
| `GET /api/dashboard/stats` | 概览统计（total/silent/exceptions/spec_count） | errors 缓冲 + PG traces |
| `GET /api/dashboard/traces?limit=N` | 列出最近 traces 摘要 | errors 缓冲 + PG traces |
| `GET /api/dashboard/trace/{trace_id}` | trace 详情（含 spec_diffs） | PG traces + errors 缓冲 |
| `GET /api/dashboard/specs` | 列出已存规范 | spec_store |

`_collect_all_traces` 合并两个数据源：
1. `errors.list_recent()` — 内存异常缓冲（save_trace 默认存这里）
2. `logs.list_request_ids()` — PostgreSQL 持久化数据

`_extract_trace_summary` 安全处理 data 字段（支持 str/dict/None），`_safe_int` 处理 status 类型转换。

### 3.6 可观测性（`app/observability.py`）✅

`/metrics`（Prometheus 文本）、`/health`（校验 LLM 配置 + 存储层连通性，状态 `ok/degraded/unhealthy`）、启动日志打印关键配置。

### 3.7 配置（`app/config.py`，pydantic-settings）✅

单例 `settings`，读项目根 `.env`（基于 `__file__` 锚定绝对路径，任意 CWD 启动行为一致；修复 ENV-001）。`code_context_lines` 等 FR11 配置键已补全。

---

## 4. 关键流程时序

### 4.1 全局异常自动捕获（✅ 已落地）

```mermaid
sequenceDiagram
    participant Code as 用户代码
    participant Hook as sys.excepthook/asyncio handler
    participant Logs as logs core
    participant Store as trace_store

    Code->>Hook: 未捕获异常
    Hook->>Logs: capture_exception(exc, source=...)
    Hook->>Store: add_log(error)
    Hook-->>Code: 原样抛给原 hook（不掩盖）
    Note over Code,Store: 宿主 AI 之后 list_recent_traces 即可见
```

### 4.2 调试流程 + LLM 分析（✅）

```mermaid
sequenceDiagram
    participant C as 调用方
    participant API as /api/debug/run
    participant E as 引擎
    participant LLM as OpenAI

    C->>API: POST /run {payload}
    API->>E: create_request_id + add_log*
    E-->>API: trace
    API->>E: build_context
    API-->>C: DebugResponse{trace,context}
    C->>API: POST /analyze {request_id}
    API->>E: build_context + collect_runtime_snapshot
    API->>E: truncate_context
    E->>LLM: chat.completions(SYSTEM+context)
    LLM-->>E: {root_cause,impact,fix,confidence}
    E-->>C: {context, analysis}
```

### 4.3 MCP Streamable HTTP 握手（✅）

```mermaid
sequenceDiagram
    participant C as 客户端
    participant S as /mcp

    C->>S: POST initialize
    S-->>C: 200 + Mcp-Session-Id
    C->>S: POST notifications/initialized
    S-->>C: 202
    C->>S: POST tools/call
    S-->>C: 结果 (JSON 或 SSE)
    C->>S: DELETE /mcp
    S-->>C: 204
```

---

## 5. 数据模型（`app/schemas`）

```python
# TraceEntry: {step, data, ts}
# DebugContext: {trace, runtime?, code_snippets:[CodeSnippet], note}
# CodeSnippet: {file, error_line, snippet, found}
# RuntimeSnapshot: {pid, cpu_percent, memory_mb, thread_count, open_files, python_version, env_hint}
# Session: {session_id, created_at, last_active, metadata}
```

LLM 输出契约：`{root_cause:str, impact:str, fix:str, confidence:"high|medium|low"}`。

---

## 6. 已实现的关键能力

> 以下能力已按本架构实现并通过测试，详细实现细节参见各模块源码。

| 能力 | 组件 | 说明 |
| --- | --- | --- |
| 脱敏 | [app/mcp/core/redaction.py](./app/mcp/core/redaction.py) | 存储边界统一脱敏，默认开启 |
| 统一存取 | [app/mcp/core/trace_repo.py](./app/mcp/core/trace_repo.py) | 在 TraceStorage + errors 之上实现 save_trace/get_trace/save_network_record/save_ui_event |
| 网络采集 | `app/mcp/collectors/network.py` + `tools/network_api.py` | 解析/截断 + ingest_network/get_network_trace |
| UI 采集 | `app/mcp/collectors/ui_event.py` | 解析/截断 |
| Git 归因 | [app/mcp/core/git.py](./app/mcp/core/git.py) + `tools/git_api.py` | blame/diff，带超时+路径白名单 |
| 静默失败 | `app/mcp/tools/silent_failure_api.py` + [api/ingest.py](./app/api/ingest.py) | 编排 ui/network + trace_kind |
| 跨语言上报 | `app/mcp/tools/ingest_api.py` + `api/ingest.py` | ingest_error |
| inbound 采集 | [app/middleware_network.py](./app/middleware_network.py) | 独立中间件，默认关闭，安全栈内层 |
| 完整上下文 | [app/mcp/builders/context.py](./app/mcp/builders/context.py)::build_debug_context | 注入 code/git/network/ui/runtime/related_specs |
| 规范驱动采集 | `app/mcp/collectors/spec.py` + `tools/spec_api.py` | 扫描/标签匹配/缓存/脱敏 + get_related_specs |
| 指纹去重聚合 | [app/mcp/core/errors.py](./app/mcp/core/errors.py) | compute_fingerprint + occurrence_count，避免重复刷屏 |
| 双传输注册 | [app/mcp/tools/__init__.py](./app/mcp/tools/__init__.py) + [app/mcp_server.py](./app/mcp_server.py) | HTTP 15 / stdio 14，handler 层复用业务函数 |
| 代码定位 | [app/mcp/collectors/code_locator.py](./app/mcp/collectors/code_locator.py) | 源码片段 + vscode:// 链接，路径白名单防穿越 |
| 静默失败检测 | [app/mcp/verifier/assert_engine.py](./app/mcp/verifier/assert_engine.py) | assert_behavior 纯函数，<1ms 判定 |
| 前端自动化 | `app/verifier/ui_runner.py` + `tools/auto_test_api.py` | Playwright headless 遍历，可选依赖 |
| 规范驱动闭环 | [app/mcp/verifier/spec_store.py](./app/mcp/verifier/spec_store.py) | spec CRUD + verify 工具 + spec_diffs 注入 |

---

## 7. 关键设计决策（ADR 摘要）

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 宿主 AI 推理 vs 内置分析 | **默认宿主 AI 推理**，`analyze_with_llm` 仅可选 | 避免重复推理与花费；服务专注"采集+结构化" |
| 协议 | MCP（JSON-RPC 2.0）+ Streamable HTTP/stdio | 标准、被主流客户端原生支持 |
| 上下文截断 | 字符估算（`max_tokens*3`）+ 帧/局部变量上限 | 控成本与延迟，防超长 |
| 安全默认 | fail-closed + 恒定时间比较 | 防未授权与时序攻击 |
| 降级 | 快照/LLM 失败不阻断主流程 | 提高可用性 |
| 存储 | 工厂模式 memory/pg + 状态 memory/redis | 本地轻量 / 生产持久 |

---

## 8. 部署与配置

- 启动：`uvicorn app.main:app --host 0.0.0.0 --port 8000`（或 `python -m app.main --stdio`）。
- 依赖：`requirements.txt`（fastapi、uvicorn、openai、psutil、psycopg2、redis、mcp、pydantic-settings）。
- 关键配置：见 `PRD.md` §11.3；**务必生产设 `API_KEY` 与 `CORS_ORIGINS`**；`code_context_lines` 待补（§6.1）。
- 容器化：`Dockerfile` + `docker-compose.yaml` 已提供。

---

## 9. 安全设计

- 传输：HTTPS 由前置代理提供；CORS `*` 时强制 `allow_credentials=False`。
- 鉴权：API Key，fail-closed，恒定时间比较，公钥路径免鉴权。
- 防 DoS：流式请求体限制（内存恒定 ≤ `max_body_size`）、按 IP 限流。
- 信息脱敏：内部异常/DB 错误不回显；`/health` 仅状态不泄露细节。
- 密钥：`.env` 不入库，提供 `.env.example` + `.gitignore`。
- 路径安全（待补）：`file://`/`vscode://` 仅限 `WHITELIST_PATH_PREFIX`，防目录穿越。

---

## 10. 风险与开放问题

| 项 | 说明 | 处置 |
| --- | --- | --- |
| ~~代码定位未接线~~ | ~~`get_debug_context` 不含片段~~ | §6.1 ✅ 已修复 |
| ~~静默失败/前端自动化~~ | ~~FR13/FR14/FR15 待建~~ | ✅ 已实现 |
| 厂商锁定 | 仅 OpenAI | 多 LLM provider 已支持（openai/zhipu/custom）|
| memory 后端 | 重启即丢 | 生产用 postgresql |
| ~~PGStore API 误用~~ | ~~`conn.execute()` 不存在~~ | ✅ 已改用 `cur = conn.cursor(); cur.execute()` |
| ~~data 字段非 dict 时崩溃~~ | ~~`json.loads` 失败 / `.get()` 报错~~ | ✅ `_parse_data` 安全解析 + 类型检查 |
| SSE 长连接测试 | TestClient 中会阻塞 | 标记 `@pytest.mark.skip`，需手动验证 |

---

## 11. 测试策略

### 11.1 测试分层

| 层级 | 目录 | 用例数 | 说明 |
| --- | --- | --- | --- |
| 单元测试 | `tests/unit/` | 169 | redaction、fingerprint、storage、dashboard、verify_api 等 |
| 集成测试 | `tests/integration/` | 13 | API 端点、debug flow、PostgreSQL 集成 |
| PG 集成测试 | `tests/integration/test_pg_integration.py` | 13 | PGStore 连接、Dashboard 读取、MCP Tools 读取、LLM 分析 |

### 11.2 测试执行

```bash
# 全部测试（需要 PostgreSQL 运行中）
python -m pytest tests/ --tb=short -q

# 仅单元测试（不依赖 PostgreSQL）
python -m pytest tests/unit/ --tb=short -q

# 仅 PG 集成测试
python -m pytest tests/integration/test_pg_integration.py --tb=short -q
```

### 11.3 PG 集成测试覆盖

- **PGStoreConnection**：连接池、表自动创建、save/get 往返、字符串 data 存储、list_request_ids、_parse_data 辅助函数
- **DashboardIntegration**：stats 结构、traces 列表从 PG 读取、trace 详情从 PG 读取
- **MCPToolIntegration**：list_recent_traces 包含 PG 数据、search_logs 搜索 PG 数据、get_logs 返回 PG 数据
- **LLMIntegration**：analyze_with_llm 端到端（LLM 未配置时自动跳过）

---

## 12. 项目架构图（Mermaid）

### 12.1 系统架构总览

```mermaid
flowchart TB
    subgraph Clients["客户端层"]
        MC["MCP 客户端<br/>Trae/Codex/Claude Desktop"]
        REST["REST 调用方<br/>curl/Postman"]
        Browser["浏览器<br/>Dashboard"]
    end

    subgraph Transport["传输层"]
        STDIO["stdio<br/>app/mcp_server.py"]
        HTTP["Streamable HTTP<br/>app/api/mcp_routes.py"]
        DASH["Dashboard API<br/>app/api/dashboard.py"]
    end

    subgraph MW["中间件层<br/>app/middleware.py"]
        AUTH["AuthMiddleware<br/>fail-closed"]
        BODY["MaxBodySizeMiddleware"]
        RATE["RateLimitMiddleware"]
        SEC["SecurityHeadersMiddleware"]
        TRACE["TraceMiddleware"]
    end

    subgraph Engine["调试引擎"]
        LOGS["logs core<br/>add_log/get_logs/list_request_ids"]
        BUILD["context builder<br/>build_debug_context"]
        COLLECT["collectors<br/>stacktrace/runtime/code_locator/git/network"]
        VERIFY["verifier<br/>assert_behavior + spec_store"]
        HOOK["exception_hook<br/>全局异常捕获"]
        LLM["LLM analyzer<br/>智谱/OpenAI"]
    end

    subgraph Storage["存储层"]
        FACTORY["Storage Factory<br/>factory.py"]
        PG["PGStore<br/>pg_store.py<br/>连接池+自动建表"]
        MEM["MemoryStore<br/>memory_store.py"]
        ERRORS["errors 缓冲<br/>errors.py<br/>内存 deque"]
    end

    DB[("PostgreSQL<br/>ai_debug_mcp<br/>traces + sessions")]

    MC --> STDIO
    REST --> HTTP
    Browser --> DASH

    STDIO --> Engine
    HTTP --> MW --> Engine
    DASH --> MW

    Engine --> Storage
    LOGS --> FACTORY
    FACTORY --> PG
    FACTORY --> MEM
    PG --> DB
    BUILD --> ERRORS

    LLM --> |"analyze()"| Engine
```

### 12.2 数据流（POST /debug 端到端）

```mermaid
sequenceDiagram
    participant C as 客户端
    participant API as /debug API
    participant Logs as logs core
    participant Factory as Storage Factory
    participant PG as PGStore
    participant DB as PostgreSQL

    C->>API: POST /debug {payload}
    API->>Logs: create_request_id()
    API->>Logs: add_log(rid, "request_start", data)
    Logs->>Factory: get_trace_store()
    Factory->>PG: save_entry(rid, entry)
    PG->>DB: INSERT INTO traces ...
    API->>Logs: add_log(rid, "processing", {...})
    PG->>DB: INSERT INTO traces ...
    API->>Logs: add_log(rid, "response_ready", {...})
    PG->>DB: INSERT INTO traces ...
    API-->>C: {request_id, result, trace, context}
```

### 12.3 Dashboard 读取流程

```mermaid
sequenceDiagram
    participant Browser as 浏览器
    participant DASH as Dashboard API
    participant Collect as _collect_all_traces
    participant Errors as errors 缓冲
    participant Logs as logs core
    participant PG as PGStore
    participant DB as PostgreSQL

    Browser->>DASH: GET /api/dashboard/traces
    DASH->>Collect: _collect_all_traces(limit)
    Collect->>Errors: list_recent(limit)
    Errors-->>Collect: [err1, err2, ...]
    Collect->>Logs: list_request_ids(limit)
    Logs->>PG: list_request_ids(limit)
    PG->>DB: SELECT DISTINCT request_id ...
    DB-->>PG: [rid1, rid2, ...]
    PG-->>Logs: [rid1, rid2, ...]
    Logs-->>Collect: [rid1, rid2, ...]
    Collect->>Logs: get_logs(rid) per id
    Logs->>PG: get_entries(rid)
    PG->>DB: SELECT ... WHERE request_id = ...
    DB-->>PG: rows
    PG-->>Logs: entries
    Logs-->>Collect: entries
    Collect-->>DASH: merged summaries
    DASH-->>Browser: {traces, total}
```

---

## 附录：模块 ↔ 文件 速查

| 模块 | 文件 |
| --- | --- |
| 入口/生命周期 | `app/main.py` |
| 中间件 | `app/middleware.py` |
| REST 调试 | `app/api/debug.py` |
| HTTP MCP | `app/api/mcp_routes.py` |
| stdio MCP | `app/mcp_server.py` |
| JSON-RPC | `app/mcp/protocol/{server,jsonrpc}.py` |
| Trace | `app/mcp/core/logs.py` |
| Context | `app/mcp/builders/context.py` |
| Stacktrace | `app/mcp/collectors/stacktrace.py` |
| Code Locator | `app/mcp/collectors/code_locator.py` ✅ |
| Runtime | `app/mcp/collectors/runtime.py` |
| LLM | `app/llm/analyzer.py` |
| 异常钩子 | `app/mcp/hooks/exception_hook.py` ✅ |
| 存储 | `app/mcp/core/storage/*` |
| 会话/SSE | `app/mcp/transports/{session,sse}.py` |
| 可观测 | `app/observability.py` |
| 配置 | `app/config.py` |
