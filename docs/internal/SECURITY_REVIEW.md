# ai-debug-mcp 安全审查报告

> ⚠️ **已归档（2026-07-23）**：本报告为原始安全审查文档，包含 C1~C10 合规分析与 SEC-01~15 风险证据链。**审计追踪与待办状态以 [claude-audit-consolidated.md](./release/claude-audit-consolidated.md) 为准**，本文件不再更新。

> 定位：基于项目《AI Debug Assistant (Production)》角色规范的安全合规审查，逐条核验硬约束与边界场景在代码中的实现情况。
> 注意：本报告为静态代码取证，未做动态运行时验证；未修改任何代码。
> 来源：2026-07-21 安全审查（数据保护域 + 运行时协议域双路并行取证交叉验证）
> 审查范围：`app/`、`browser-sdk/` 全量相关模块
> 核心原则：**脱敏为默认行为、会话隔离不可交叉、用户数据不外发**

---

## 合规总览

| # | 约束 | 类型 | 判定 | 风险等级 |
|---|------|------|------|----------|
| C1 | MCP 工具调用超时 ≤ 30s | 硬约束 | ❌ 不符合 | 高 |
| C2 | 错误响应含 error_code+message | 硬约束 | 🔲 部分符合 | 中 |
| C3 | 并发会话隔离 | 硬约束 | 🔲 部分符合 | 高 |
| C4 | 用户数据本地驻留 | 硬约束 | ✅ 符合 | 低 |
| C5 | 脱敏默认且不可关闭 | 硬约束 | 🔲 部分符合 | 中 |
| C6 | 用户取消会话立即清理 | 边界场景 | 🔲 部分符合 | 中 |
| C7 | 堆栈混淆/压缩 source-map 还原 | 边界场景 | ⚪ 未实现 | 低 |
| C8 | 网络体 >1MB 截取前 4KB | 边界场景 | 🔲 部分符合 | 低 |
| C9 | LLM 不可用降级规则引擎 | 边界场景 | 🔲 部分符合 | 低 |
| C10 | 上下文 token 预算控制 | — | 🔲 部分符合 | 中 |

---

## 高风险项（需优先关注）

### C3 并发会话隔离 — 跨会话信息泄露

> 目标：不同 session_id 的数据严禁交叉污染。

**证据链**

- [errors.py](../../app/mcp/core/errors.py) 使用全局 `deque(_recent)`（L21）收集所有异常，不以 session_id 为 key，`list_recent`/`search`/`get_by_id` 均无 session_id 过滤。
- [trace_api.py](../../app/mcp/tools/trace_api.py#L105-L134) `list_recent_traces` 调用 `errors.list_recent(limit)` + `list_request_ids(limit)`，返回全进程所有会话的异常摘要，无 session_id 入参。
- `search_logs`（L137-177）同理跨会话暴露。
- 全仓 grep 确认 `session_id` 从未传入 `logs.add_log`/`errors.record`/`trace_repo.save_trace`；trace 存储 key 是 `request_id`/`error_id`（独立 UUID），与会话无绑定。

**根因**：trace/error 数据流未建立 session_id 维度，工具层无法做会话级鉴权。会话 A 的用户调用 `list_recent_traces` 可看到会话 B 捕获的异常（type/message/source/top_frame）；传入会话 B 的 request_id 可读取其完整 trace。

---

### C1 MCP 工具调用无超时 — 单工具卡死可阻塞整个会话

> 目标：单次 MCP 工具调用超时 ≤ 30s，超时必须返回 partial result + 超时标记。

**证据链**

- [server.py](../../app/mcp/protocol/server.py#L85-L99) `_handle_tools_call` 直接 `await handler(arguments)`，无 `asyncio.wait_for`/`asyncio.timeout`。
- [mcp_server.py](../../app/mcp_server.py#L125-L128) `_run_registered_tool` 同样无超时包裹。
- 传输层 [stdio.py](../../app/mcp/transports/stdio.py#L84) `await dispatch(req)` 与 [mcp_routes.py](../../app/api/mcp_routes.py#L81) `await dispatch_raw(raw)` 均无超时。
- 现有超时均为子操作级（Playwright 30s、LLM 30s、git 10s），无法防止单个工具 handler 整体卡死。
- 全仓 grep `partial result`/`超时标记`/`timed_out` 0 命中，"超时返回 partial result + 超时标记"逻辑完全缺失。

**根因**：协议分发层与传输层均未实现工具调用级超时与降级返回路径。

---

### C5 脱敏可被关闭 + exception_hook 路径绕过脱敏

> 目标：脱敏为默认行为，非可选项；未配置规则时采用内置正则兜底。

**证据链（可关闭）**

- [config.py](../../app/config.py#L82) `redaction_enabled: bool = True`，可经 `REDACTION_ENABLED=false` 关闭。
- [redaction.py](../../app/mcp/core/redaction.py#L90-L91) `if not settings.redaction_enabled: return text` 直接放行，无强制拦截或告警。
- [ai-debug.js](../../browser-sdk/ai-debug.js#L553-L560) `init(opts)` 允许 `redactFields: []` 清空脱敏；SDK 仅匹配 4 个精确键名，覆盖面窄于服务端 6 条正则。

**证据链（绕过路径）**

- [exception_hook.py](../../app/mcp/hooks/exception_hook.py#L38) 直接调用 `capture_exception` 后传入 `errors.record`，未经 `trace_repo.save_trace`。
- [stacktrace.py](../../app/mcp/collectors/stacktrace.py#L67-L70) 返回的 `message=str(exc)` 与 `traceback=traceback.format_exception(...)` 均未调 `redact()`，原样存入 `errors._recent`。
- [stacktrace_api.py](../../app/mcp/tools/stacktrace_api.py#L92) `get_stacktrace()` 暴露原始 traceback；若异常消息含 `password="xxx"` 等敏感模式，直接泄露给 MCP 工具调用方。

**根因**：脱敏仅在 `trace_repo` 存储边界和 LLM 发送前实施，`exception_hook`→`errors` 缓冲路径绕过该边界；且 `redaction_enabled` 开关缺乏生产环境强制锁定。

---

## 中/低风险项

### C2 错误响应格式 — 部分符合

> 目标：错误响应必须包含 error_code（机器可读）+ message（人类可读）。

- JSON-RPC 协议层合规：[jsonrpc.py](../../app/mcp/protocol/jsonrpc.py#L30-L54) `make_error` 含 `code`+`message`，遵循 2.0 规范。
- HTTP 全局异常不合规：[error_handlers.py](../../app/error_handlers.py#L26-L32) 仅返回 `detail`+`trace_id`，无 error_code。
- 工具执行错误不合规：[server.py](../../app/mcp/protocol/server.py#L102-L110) 与 [mcp_server.py](../../app/mcp_server.py#L153) 用 `isError:True`+固定文案"工具执行失败"，无机器可读 error_code，客户端无法区分超时/参数错/内部错。

---

### C6 用户取消会话清理 — 部分符合

> 目标：用户取消会话 → 立即停止采集，清理临时数据。

- 会话注册表删除符合：[mcp_routes.py](../../app/api/mcp_routes.py#L136-L143) `DELETE /mcp` 调 `registry.delete`。
- 不清理临时数据：不调用 `trace_store.delete`、不清理 `errors._recent`、不清理 network/ui_event。
- 无主动停止采集机制：无 `session/cancel` 或 `tools/cancel` MCP 方法；SSE 断开仅 `hub.unsubscribe`。
- 清理依赖被动 TTL：[main.py](../../app/main.py#L49-L58) `periodic_cleanup` 每 300s 跑，`trace_ttl=3600`，用户取消后数据最长残留 1 小时，不符合"立即"。

---

### C10 上下文 token 预算控制 — 部分符合

> 目标：单次调试会话内上下文 token 预算控制（默认 8K，可配置）；超预算时按相关性排序截断，保留头尾。

- 配置存在：[config.py](../../app/config.py#L39) `max_context_tokens: int = 8000`。
- 构建器不控制预算：[context.py](../../app/mcp/builders/context.py) `build_debug_context` 全程不引用 `max_context_tokens`，无 token 估算/截断；仅对 `related_specs` 做字符级硬上限（L158 `>6000`）。
- 预算仅在 LLM 路径生效：[analyzer.py](../../app/llm/analyzer.py#L125-L180) `truncate_context` 用字符数近似 token（`max_tokens*3`），非真实 token 计数。
- 未按相关性排序截断、未显式保留头尾：采用固定字段丢弃 + 前 N 帧/前 N 变量，无相关性评分。

---

### C8 网络体截断 — 部分符合

> 目标：网络请求体 > 1MB → 截取前 4KB + 标注截断。

- 服务端 [network.py](../../app/mcp/collectors/network.py#L11-L20) `_MAX_BODY_CHARS=10*1024`（10KB），标注 `"...（已截断）"`。
- 实际阈值 10KB（服务端）/2000 字符（SDK 响应体）/ 无截断（SDK 请求体），非约束规格的 1MB 触发 4KB 截取。
- [config.py](../../app/config.py#L78) `max_body_size=1_048_576` 是 HTTP 请求体拒绝阈值（DoS 防护），非截断触发器。

---

### C9 LLM 降级 — 部分符合

> 目标：LLM 服务不可用 → 降级为规则引擎分析，不阻塞主流程。

- 不阻塞主流程：[debug.py](../../app/api/debug.py#L107-L119) 与 [debug_api.py](../../app/mcp/tools/debug_api.py#L72-L76) 均捕获 `RuntimeError` 返回结构化错误，trace 流程不受影响。
- 具备重试 + fallback 模型：[analyzer.py](../../app/llm/analyzer.py#L262-L325) 指数退避 + fallback 模型。
- 无规则引擎降级：全仓 grep `rule.engine|rule_based|heuristic` 无 LLM 相关命中；最终失败抛 `RuntimeError`，用户无任何分析输出。

---

### C7 堆栈 source-map 还原 — 未实现

> 目标：堆栈被混淆/压缩 → 尝试 source-map 还原，失败则标注"无法解析"。

- [stacktrace.py](../../app/mcp/collectors/stacktrace.py) 为 Python 服务端堆栈采集，无混淆检测、无 source-map 还原、无"无法解析"标注。
- 全仓 grep `source.?map|sourcemap|minified|混淆|压缩|obfuscat` 0 命中。
- 注：`ingest_error` 工具明确支持外部（Node/Go/Java）上报，前端 JS 场景的 source-map 缺失是真实缺口；Python 服务端场景需求较弱。

---

## 符合项

### C4 数据本地驻留 — 符合

> 目标：所有用户数据不得离开本地存储，除非用户显式授权上传。

- 存储默认内存，可选本地 PG/Redis（docker-compose 部署），无云端/远程存储选项。
- 唯一外发点为 LLM：[analyzer.py](../../app/llm/analyzer.py#L42-L59) 创建 OpenAI 客户端，发送前经 `_prepare_context_for_llm` 双重脱敏。
- LLM 仅经 `/analyze` 端点或 `debug_api` 工具显式触发，不在 trace 流程中自动发起。
- 无遥测/匿名统计：[observability.py](../../app/observability.py) 为纯本地 Prometheus 计数器，无 push gateway 外发。
- 轻微不足：缺少独立的"LLM 数据外发授权"开关，依赖 API Key 统一鉴权。

---

## 建议修复方向（未执行修改）

按优先级排序，供决策参考：

1. **C3 会话隔离**：为 `errors._recent` 增加 session_id 维度；`list_recent_traces`/`search_logs`/`get_stacktrace` 工具增加 session_id 入参并强制过滤；在 `errors.record`/`trace_repo.save_trace` 入口绑定 session_id。
2. **C1 工具超时**：在 `_handle_tools_call`/`_run_registered_tool` 包裹 `asyncio.wait_for(handler, timeout=30)`，超时返回 `{"isError":True,"_timed_out":True,"partial":...}`。
3. **C5 脱敏锁定+绕过修补**：生产环境强制 `redaction_enabled=True` 不可覆盖，或关闭时拒绝启动并告警；在 `capture_exception` 对 `message`/`traceback` 调 `redact()`，或在 `errors.record` 入口统一脱敏。
4. **C2 错误码**：HTTP 全局异常与工具执行错误响应补 `error_code` 字段（如 `TOOL_TIMEOUT`/`TOOL_INTERNAL`/`TOOL_PARAMS`）。
5. **C6 取消清理**：`DELETE /mcp` 串联 `trace_store.delete(session_id)`、清理 `errors._recent` 该会话条目；新增 `tools/cancel` MCP 方法。
6. **C10 预算控制**：`build_debug_context` 引入 token 估算与截断，实现相关性排序 + 头尾保留。
7. **C9 规则引擎**：实现基于堆栈模式匹配/已知错误库的 fallback 分析。
8. **C7 source-map**：在 `ingest_api` 识别压缩/混淆堆栈并标注"无法解析"，有 map 文件时尝试还原。
9. **C8 截断阈值**：统一为 1MB 触发 4KB 截取，或修订约束规格与代码对齐。

---

## 审查局限

- 本次为静态代码取证，未做动态运行时验证（如构造双会话实际触发泄露）。
- 约束 C7（source-map）在 Python 服务端场景需求较弱，判定"未实现"对后端核心功能影响有限，但对前端 JS 上报场景是真实缺口。
- 未覆盖：认证鉴权强度（API Key 实现）、SQL 注入面、依赖漏洞扫描、Dockerfile 安全基线——如需扩展可另起专项。

---

## 附录：合规总览表

| 约束域 | 符合 | 部分符合 | 不符合 | 未实现 |
|--------|------|----------|--------|--------|
| 硬约束（5 项） | 1（C4） | 3（C2/C3/C5） | 1（C1） | 0 |
| 边界场景（4 项） | 0 | 3（C6/C8/C9） | 0 | 1（C7） |
| 其他（1 项） | 0 | 1（C10） | 0 | 0 |
| **合计** | **1** | **7** | **1** | **1** |

### 当前差距

- 硬约束达标率 1/5，核心安全基线未达发布门槛。
- 高风险项 3 个（C1/C3/C5），其中 C3 构成跨会话信息泄露，C1 可致单工具卡死阻塞会话，C5 存在脱敏绕过路径与可关闭开关。
- 建议在下一迭代优先闭环 C1/C3/C5，再处理 C2/C6/C10 中风险项。

---

# 附加：SEC-01~15 风险清单（2026-07-22 数据流+安全联合复核）

> 本章为在 C1~C10 之上，按「数据流通 / 代码逻辑 / 权限与框架」三维做的补充取证，重点补齐 **LFI、SSRF** 两个此前未登记的高危项。所有结论附 `文件:行`，均为静态取证、未改代码。
> 配套数据流复核见 [DESIGN.md](./DESIGN.md) §13。整改追踪见 [release/claude-audit-consolidated.md](./release/claude-audit-consolidated.md)。

> ✅ **进展（2026-07-22）**：P0 四项（SEC-01 LFI / SEC-02 SSRF / SEC-03 默认鉴权 / SEC-05 工具超时）**已修复**；P1 五项（SEC-04 会话隔离 / SEC-06 脱敏绕过 / SEC-07 限流 fail-closed / SEC-08 /metrics 鉴权 / SEC-09 SDK 脱敏）**已修复**；P2 大部分完成。逐项状态见 [release/claude-audit-consolidated.md](./release/claude-audit-consolidated.md)。下表为原始风险与证据链。

## 风险总表

| ID | 严重度 | 风险 | 证据链 | 关联 |
| --- | --- | --- | --- | --- |
| **SEC-01** | 🔴 高 | **任意文件读取（LFI）**：`POST /ingest/error` 接受任意 `frames[].file`（`ingest.py:73`，无路径校验）→ `GET /api/dashboard/trace/{id}`（`dashboard.py:181`）触发 `build_debug_context`→`code_locator.get_code_snippet` `linecache` 读该路径并回显 `code_snippets`。白名单 `whitelist_path_prefix` 默认空 → `_is_allowed` 返回 True（`code_locator.py:35-37`）。同理经 `git blame/diff` 泄漏任意仓库历史。 | `ingest.py:66`→`context.py:110`→`code_locator.py:85` | 新增 |
| **SEC-02** | 🔴 高 | **SSRF + 本地文件读**：`POST /api/debug/verify/ui`（`debug.py:206`）与 `auto_test` 工具把调用方 `target/url` 直接交给 Playwright `page.goto`（`ui_runner.py:82,179`；`auto_test_api.py:58`），**无 scheme/host 白名单**。可访问 `169.254.169.254`（云元数据）、内网、`file:///`。 | `ui_runner.py:59,82` | 新增 |
| **SEC-03** | 🔴 高 | **默认免鉴权 + 启动防护可绕过**：`api_key` 默认 `None`（`config.py:74`）→ `AuthMiddleware.enabled=False`（`middleware.py:27`）。`validate_startup_configuration` 仅拦 `0.0.0.0`+无 key 且**只在 `__main__` 调用**（`main.py:37,233`）；绑定 `127.0.0.1` 或用 `uvicorn app.main:app` 直启即绕过。CORS 默认 `*`。 | `config.py:74`、`main.py:233` | 深化 C(auth) |
| **SEC-04** | 🔴 高* | **无跨会话/租户隔离**→ ✅ **已修复（Phase 5）**：共享 HTTP 场景已加固 session_id 维度隔离。 | `errors.py:78,102`、`dashboard.py:124-143` | = C3 |
| **SEC-05** | 🟠 中高 | **MCP 工具调用无超时**：`server.py:87`/`mcp_server.py:125` 直接 `await handler`，无 `wait_for`；`auto_test`/`verify_ui`/`analyze` 可运行数分钟阻塞会话。 | `server.py:75-90` | = C1 |
| **SEC-06** | 🟠 中 | **脱敏绕过（自动捕获路径）+ 可全局关闭**：`exception_hook.py:38→errors.record` 存入的 `message`（`stacktrace.py:67`）与完整 `traceback`（`:70`）**未脱敏**，`stacktrace` 工具原样返回；`redaction_enabled` 可关（`config.py:82`、`redaction.py:98`）。 | `stacktrace.py:67-70` | = C5 |
| **SEC-07** | 🟠 中 | **限流 fail-open**→ ✅ **已修复（Phase 5）**：Redis 异常时返回 429（fail-closed）；已改用 Redis ZSET 滑动窗口；内存后端加淘汰机制。 | `state/store.py:87-95` | 新增 |
| **SEC-08** | 🟠 中 | **公开 `/metrics` 泄露 + 高基数 + label 注入**→ ✅ **已修复（Phase 5）**：新增 `METRICS_AUTH_ENABLED` 独立鉴权 toggle；`path` 已模板化防注入。 | `observability.py:30,57-81` | 深化 |
| **SEC-09** | 🟠 中 | **SDK 上报体未脱敏、可关闭**：`ai-debug.js` 仅按精确顶层键名脱敏，不解析 body 字符串（`:316,378`）；`init({redactFields:[]})` 可整体关闭（`:557`）。 | `ai-debug.js:77-92` | 深化 C5 |
| **SEC-10** | 🟡 低-中 | **诊断端点上生产**：`/api/debug/echo`（回显）、`/api/debug/token`（`debug.py:236` 返回硬编码 `{"token":"abc123",...}`）、`/runtime`、`/session`。 | `debug.py:224-239` | = L3 |
| **SEC-11** | 🟡 低 | 工具错误无机器可读 `error_code`；`mcp_routes.py:47` 仍回显 `f"无效 JSON: {e}"`（CODE_REVIEW 称已移除，实际未改）。 | `server.py:102`、`mcp_routes.py:47` | = C2 |
| **SEC-12** | 🟡 低 | ✅ **已修复（Phase 5，SEC-12）**：CORS 改为最后 `add_middleware`（最外层），`OPTIONS` 预检请求放行，不再被 `Auth` 401 拦截；中间件真实执行顺序已订正。 | `middleware.py:157-168` | 新增 |
| **SEC-13** | 🟡 低 | **已修复（2026-07-23）**：`spec_store.update` 改为 crash-safe append（单次 `add_log` 提交点，不再 `delete_logs`）+ 读取取最新版本；`save_trace` 写入顺序改为 commit-marker（META→LINK→DATA）。 | `spec_store.py`、`trace_repo.py` | 深化 |
| **SEC-14** | 🟡 低 | **已修复（2026-07-22）**：`_execute_with_retry` 返回 `(conn, rowcount)` 元组，OperationalError 时关闭坏连接并获取新连接，调用方 finally 归还最新连接。 | `pg_store.py:122-172` | = M2 |
| **SEC-15** | 🟡 低 | dashboard `limit` 无上限（`dashboard.py:170`）；`git blame` 的 `line` 未强制 int（`git_api.py`→`git.py:104`，非 shell 无注入，仅类型缺口）。 | — | 新增 |

\* **SEC-04 严重度依部署形态**：共享 HTTP 多客户端下为高危；**stdio 每客户端独立进程**模型下全局 deque 天然按进程隔离，风险显著降低。RESUME/INTERVIEW 中“Agent 间会话隔离互不污染”的表述仅对 stdio 成立，对共享 HTTP **不成立**，已在对应文档订正。

## 已核实为「安全 / 无风险」的项（避免误伤）

- **SQL 全参数化**：`pg_store.py` 所有语句 `%s`+参数元组，含 `LIMIT %s`，**无 SQL 注入**。
- **LLM 发送前递归脱敏**：`analyzer.py:107-110` 截断+`_redact_value_for_llm`，敏感数据不外发 LLM。
- **assert_engine 纯函数**：无 `eval/exec`、无动态属性访问，恶意 spec 无法触发代码执行。
- **PG 连接池**：双重检查锁正确，`conn` 均 `finally` 归还，无泄漏路径。
- **NetworkCapture 不读 body**：默认关闭、跳过 `/health /metrics`，不破坏下游 body 读取。

## 关于「资金 / 支付安全」

本项目为调试工具，**不含任何支付/资金逻辑，无此类直接风险**。唯一间接财务风险：`analyze_with_llm` 无调用配额 + 无工具超时 + 重试退避，可致**外部 LLM API 费用失控**；LLM API Key 泄漏即产生账单责任。建议对 LLM 调用加频率/配额限制。

## 修复优先级（并入 v0.3.1 收口）

- **P0（部署前必修）**：✅ **已修复（2026-07-22）**——SEC-03 启动校验移入 lifespan、SEC-01 路径白名单默认收敛 CWD、SEC-02 Playwright URL 校验、SEC-05 工具 `wait_for` 超时。详见 [release/claude-audit-consolidated.md](./release/claude-audit-consolidated.md)。
- **P1（发布前应修）**：✅ **已全部修复（Phase 5）**——SEC-04 会话隔离（共享 HTTP）✅、SEC-06 脱敏绕过+锁定 ✅、SEC-07 限流 fail-closed ✅、SEC-08 /metrics 鉴权+path 模板化 ✅、SEC-09 SDK 体脱敏 ✅。
- **P2/P3**：SEC-10~15，并入既有 M/L 清单。

> 结论：核心数据流架构合理、无需重写；**安全短板集中在“边界”**（鉴权/路径/URL/超时/隔离/脱敏）。定向加固（P0+P1）约 2 人周即可将其从“优秀作品项目”提升到“可生产部署”。
