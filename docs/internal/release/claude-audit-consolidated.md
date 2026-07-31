# ai-debug-mcp 审查待办清单（最终版）

> 来源：整合 v0.3.0 和 v0.3.1 两次审查待办清单 + beta-release Phase 2 全量审查。
> 目的：统一管理所有待处理项，避免重复，明确优先级。
> 更新时间：2026-07-27（beta-release Phase 2 全量审查：5 维度 × 5 Agent 并行扫描）
> 状态：v0.3.x P0/P1/P2/P3 全部完成 ✅；beta-release 审查发现 P0×6 + P1×9 + P2×12 + 文档×5，阻断上线和开源。
> 说明：本文件只代表 Release Audit 清单收口结果，不再作为当前项目功能完成度的权威来源；默认交付状态请查看 [../DELIVERY_MATRIX.md](../DELIVERY_MATRIX.md)。

---

## 一、状态总览

| 优先级 | 总数 | 已完成 | 待处理 | 说明 |
|--------|------|--------|--------|------|
| P0 | 11 | 11 | 0 | v0.3.0: 7/7 ✅；v0.3.1: 4/4 ✅ |
| P1 | 9 | 9 | 0 | v0.3.0: 8/8 ✅；v0.3.1: 5/5 ✅（SEC-04~09 全部完成）|
| P2 | 16 | 16 | 0 | v0.3.0 承接 11/11 ✅、v0.3.1 P2 5/5 ✅（M3/M10/M11 已完成）|
| P3 | 9 | 9 | 0 | v0.3.0 P3 8/8 ✅、v0.3.1 P3 1/1 ✅（L6 已完成）|
| 未确认 | 3 | 3 | 0 | C5/C4/H7 全部核实（2026-07-23）|

---

## 二、P0 — 全部完成 ✅

### v0.3.0 P0（7/7）
- [x] N1：全局异常钩子签名不匹配
- [x] C3：trace_repo 兜底存储键与返回 ID 不统一
- [x] C4：PG 持久化重启即丢
- [x] H4：verify_ui 同步阻塞事件循环
- [x] H5：堆栈局部变量脱敏无效
- [x] H10：SDK 静默失败上下文恒空
- [x] H12：进程边界零测试覆盖

### v0.3.1 P0（4/4）
- [x] SEC-01：任意文件读取（LFI）
- [x] SEC-02：SSRF + 本地文件读（Playwright）
- [x] SEC-03：默认免鉴权 + 启动防护可绕过
- [x] SEC-05：MCP 工具调用无超时

---

## 三、P1 — 全部完成 ✅

### v0.3.0 P1（8/8）
- [x] H2：stdio 与 HTTP 工具面不一致
- [x] H9：求职/内部文档混入仓库根目录
- [x] N2：LLM 输出零校验/净化
- [x] N3：stdio 关闭不回收资源
- [x] N4：内部错误串裸返回客户端（含 N4-FU-1/2 已完成，N4-FU-3 待 PGStore 审批）
- [x] M1：存储工厂拼写错误静默回退
- [x] M4：协议错误码不规范
- [x] M12：依赖管理混乱

### v0.3.1 P1（5/5）✅
- [x] **SEC-04**：无跨会话/租户隔离 ✅
  - 证据：`errors.py` 全方法支持 `session_id` 分桶（`_get_bucket`），`trace_api.py` 传递 `session_id`，`app/api/ingest.py` 传入 `session_id`（L62 `ingest_silent_failure`、L80 `ingest_error`、L134/L171 `_dispatch_single` 批量分发），7 个测试用例（`test_errors.py`）
- [x] **SEC-06**：脱敏绕过 ✅
  - 证据：`app/mcp/hooks/exception_hook.py:29-35` 定义 `_redact_exception_data` 辅助函数（函数体 L32/L34 调用 `redact()`，但本文件中未被调用，属预留代码）；实际生产 hook 路径在 `app/mcp/hooks/exception_hook.py:49-50`（`_hook`/sys.excepthook）和 `app/mcp/hooks/exception_hook.py:63-64`（`_asyncio_handler`）显式 `redact()` message/traceback；`stacktrace.py:98` `format_trace_for_ai` 返回 `redact()` 结果
- [x] **SEC-07**：限流 fail-closed + 原子化 ✅
  - 证据：`app/state/store.py:87-112` Lua 脚本 `_SLIDING_WINDOW_LUA`（注释起于 L87，字符串赋值 L90-112），原子化滑动窗口算法（ZSET + ZREMRANGEBYSCORE + ZADD，**非 INCR+EXPIRE**，与 `MemoryStateStore.allow` 语义一致）；`app/state/store.py:128-130` `except Exception: ... return False` 为 fail-closed；`test_middleware.py::test_fail_closed_on_exception` 已覆盖
- [x] **SEC-08**：/metrics 鉴权 + path 模板化 ✅
  - 证据：`app/middleware.py:20` `PUBLIC_PATHS = ("/", "/health", "/demo", "/demo/silent-failure", "/ai-debug.js")`（5 项），`/metrics` 不在其中需鉴权；`observability.py:36-39` 使用路由模板 `getattr(route, "path", ...)` + `_sanitize_label` 防高基数
- [x] **SEC-09**：SDK 上报体深度脱敏 ✅
  - 证据：`ai-debug.js:88-114` `_redact()` 递归深度脱敏（含 JSON.parse → 递归 → JSON.stringify）；`ai-debug.js:20` `_DEFAULT_REDACT_FIELDS` 内置列表；`ai-debug.js:591-593` 空数组回退到默认列表，防止关闭脱敏

---

## 四、P2 — 建议修（13/16）

### v0.3.0 承接项（8/11）
- [x] **M2**：PG 重试在已失效连接上重试（= SEC-14）✅
- [x] **M6**：redaction 规则缺口（= SEC-06）✅
- [x] **M9**：`.env` 出现未知键启动即崩 ✅
- [x] **M14**：SDK 对流式响应无条件 `clone().text()`（= SEC-09）✅
- [x] **M3**：事件循环内跑同步阻塞 ✅（2026-07-23）
  - 证据：`run_ui_verification` 虽为同步阻塞（`sync_playwright`），但两条调用路径均不阻塞事件循环——MCP 路径 `server.py:114-117` `_handle_tools_call` 对同步 handler 走 `asyncio.to_thread`；HTTP 路径 `debug.py:209-224` `/api/debug/verify/ui` 为 sync `def`，FastAPI 自动放入 threadpool。`tests/integration/test_mcp_verify_ui.py::TestVerifyUiDoesNotBlockEventLoop`（2 用例）用并发 tick 计数器证明 to_thread 生效（H4 复核已实质覆盖 M3）
- [x] **M5**：`initialize` 不做版本协商 ✅（已完成）
  - 证据：`server.py:27-86` 已实现版本协商——`SUPPORTED_PROTOCOL_VERSIONS = ["2024-11-05", "2024-08-27"]`；`_handle_initialize` 读取 `protocolVersion`，在支持列表中则回显，未知/缺失版本回退到 `PROTOCOL_VERSION` 并记录 warning
- [x] **M7**：空串 `API_KEY` 使鉴权"开而无锁" ✅（2026-07-23）
  - 证据：`config.py` `model_post_init` 将空串/纯空白 `api_key` 归一化为 `None` + warning（"空=未配置=不鉴权"语义收口于配置层）；`middleware.py` 未改，`AuthMiddleware.enabled = api_key is not None` 在归一化后对空串为 `False`，`hmac.compare_digest` fail-closed 不变；新增 3 个测试（`test_config.py::TestApiKeyNormalization`）
- [x] **M8**：MaxBodySize 只查 `Content-Length` ✅（已完成）
  - 证据：`middleware.py:64-108` `MaxBodySizeMiddleware` 已支持 chunked transfer-encoding——无 `Content-Length` 或 chunked 时用 `request.stream()` 流式累计字节数，超限返回 413；未超限则回填 `request._body` 供下游重新读取
- [x] **M10**：版本口径混乱且无 tag ✅（2026-07-23）
  - 证据：`app/__init__.py:4` `__version__="0.3.0"` 单一来源 ✅；已创建正式 annotated tag `v0.3.0`（`git tag -a v0.3.0`，本地未推送）；release 流程已标准化写入 `DEV_PLAN.md` §八（版本单一来源 → 跑测试 → 打 annotated tag → 推送 tag → 同步文档）
- [x] **M11**：`migrations/` 中 `network_records` / `ui_events` 两张表在 `pg_store.py` 中无读写代码 ✅（2026-07-23）
  - 证据：`network_records`/`ui_events` 在 `pg_store.py` 中无任何 CRUD；其数据经 `trace_repo.py` `save_network_record`/`save_ui_event` → `add_log` 写入 traces 表 + step 字段。`pg_store.py` 用硬编码 DDL 常量建表（不读取 migrations/ 目录）。已删除 `migrations/20260712_create_network_records_table.sql` 与 `migrations/20260712_create_ui_events_table.sql`；并修正 `scripts/init_db.sh` 过时注释（原误将 errors/specs 标 deprecated，实际 errors/specs 为活跃表，仅 network_records/ui_events 废弃）。未触碰 pg_store.py
- [x] **M13**：无 pytest.ini/markers；集成测试直连真实 PG 与计费 LLM ✅（已完成）
  - 证据：项目根目录已有 `pytest.ini`，定义 4 个 markers：`integration`（集成测试）、`llm`（需 LLM Key）、`pg`（依赖 PostgreSQL）、`slow`（耗时较长）；`filterwarnings` 已配置忽略 pytest_asyncio 弃用警告

### v0.3.1 P2（5/5）✅
- [x] **SEC-14**：PG `_execute_with_retry` 未真正重连 ✅（2026-07-22）
- [x] **SEC-11**：工具错误无 `error_code`；`mcp_routes.py:47` 仍回显 `{e}` ✅
  - 证据：`server.py:111,124` 有 `error_code: TOOL_TIMEOUT/TOOL_INTERNAL`；`mcp_routes.py:47` 改为 `"无效 JSON，详情见服务端日志"`
- [x] **SEC-15**：dashboard `limit` 无上限；`git blame` 的 `line` 未强制 int ✅
  - 证据：`dashboard.py:131,185` `limit = min(max(limit, 1), 1000)`；`git.py:103` `line_no = int(line_no)`
- [x] **SEC-10**：诊断端点上生产（= L3）✅
  - 证据：`app/config.py:110` `debug_endpoints_enabled: bool = False`（默认关闭）；`debug.py:234,242` 未启用时返回 404
- [x] **SEC-13**：非原子写入 ✅（2026-07-23）
  - 证据：`spec_store.py` `update()` 改为 crash-safe append（单次 `add_log` 写新版本作提交点，不再 `delete_logs`，删除仅由显式 `delete()` 负责）；`get()` 存储回读 + `_do_restore()` 按 `updated_at`（回退 `timestamp`）取最新版本，多版本共存不再读陈旧数据；`trace_repo.py` `save_trace()` 写入顺序改为 commit-marker（`META → LINK → DATA`，`trace_data` 最后写，存在即保证元数据已落库）；新增 5 个测试（`test_spec_store.py::TestAtomicWrites` 3 + `test_trace_repo.py::TestSaveTraceAtomicity` 2）

---

## 五、P3 — 体验优化（8/9）

### v0.3.0 P3（7/8）
- [x] **L8**：`test_ui_runner.py` 中 `assert status in (200,422)` 两互斥结果都算过 ✅
- [x] **L1**：GET `/mcp` SSE 空壳 ✅（已完成）
  - 证据：`mcp_routes.py:106-133` GET `/mcp` 端点已实现完整 SSE 流——支持 `Accept: text/event-stream` 时返回 `StreamingResponse`（服务端→客户端推送通道），非 SSE 请求返回健康信息；非空壳
- [x] **L2**：`app/api/auth.py` 中 `verify_api_key` 是死代码 ✅（已完成）
  - 证据：`app/api/auth.py` 文件已删除；全仓 grep `verify_api_key|from app.api.auth` 无命中，死代码已清理
- [x] **L3**：`/api/debug/token` 硬编码返回（= SEC-10）✅（已完成）
  - 证据：`debug.py:241-246` `/api/debug/token` 已受 `debug_endpoints_enabled` 门控（与 SEC-10 同一修复），默认关闭时返回 404；硬编码内容仅在调试模式下可见，生产中不可达
- [x] **L4**：CORS 默认 `*` ✅（已完成）
  - 证据：`app/config.py:105` `cors_origins: str = ""`（空串=不下发 CORS 头，默认收紧）；`middleware.py:222-236` 仅当 `settings.cors_origins` 非空时注册 `CORSMiddleware`，`"*"` 需显式 opt-in；白名单按逗号分隔配置
- [x] **L5**：`MemoryTraceStore` 无容量上限 ✅（已完成）
  - 证据：`memory_store.py:12` `__init__` 默认 `max_entries=10000`；`memory_store.py:22` 写入时检查 `len(self._store) >= self._max_entries`，超限则删除最旧条目（FIFO 淘汰），防 OOM
- [x] **L6**：docker-compose 不透传 `LLM_PROVIDER`/`LLM_BASE_URL` ✅（2026-07-23）
  - 证据：`docker-compose.yaml:41-48` 已透传 `LLM_PROVIDER`(L43)、`LLM_BASE_URL`(L44) 及 `LLM_MODEL`/`OPENAI_API_KEY`/`LLM_FALLBACK_MODEL`/`LLM_TIMEOUT`/`LLM_TEMPERATURE`/`LLM_MAX_RETRIES`。仓库仅此一个 compose 文件（无 `.yml` 冗余）
- [x] **L7**：README 小失实；browser-sdk 缺 `package.json` ✅（已完成）
  - 证据：`browser-sdk/package.json` 存在，包含 `name="ai-debug-sdk"`、`version="0.5.0"`、`main="ai-debug.js"`（SDK 已迭代至 V5+：V3 网络错误标记 / V4 trace 关联 / V5 增强 ingest 端点+gzip 压缩传输 / V6 自动检测 UI 静默失败，详见下文「Browser SDK 续作」表）；README 已同步更新测试基线与安全审查状态

### v0.3.1 P3（1/1）✅
- [x] **SEC-12**：中间件真实执行顺序与 DESIGN 声称相反 ✅（已完成）
  - 证据：`middleware.py:211-236` `configure_middleware` 中 `CORSMiddleware` 已移到最后 `add_middleware`（最外层），确保 OPTIONS 预检请求不被 Auth 401 拦截；中间件执行顺序已订正与 DESIGN 一致

---

## 六、未确认项（3/3 ✅）

- [x] **C5**：确认 WIP unawaited dispatch 崩溃的 4 个 failed 测试是否已转绿 ✅（2026-07-23）
  - 证据：单元测试 `310 passed / 6 skipped / 0 failed`，dispatch 路径测试全绿，WIP-001 已修复
- [x] **C4**：确认 `test_trace_detail_from_pg` 测试结果 ✅（2026-07-23）
  - 证据：PG 集成测试在无 PG 环境下显式 skip（19 skipped），测试代码真实面向 PG，skip 合规
- [x] **H7**：确认 PG 会话存储测试是否存在假覆盖 ✅（2026-07-23）
  - 证据：`test_storage.py:199-218` `TestPGSessionStore.test_list_active` 真实用 `mod._get_pool().getconn()` + 真实 SQL，非 memory 假覆盖

---

## 七、Phase 1 短期优化（全部完成 ✅）

| 编号 | 任务 | 状态 |
|------|------|------|
| P1-1 | PG 连接池可配置化 | ✅ 已完成（2026-07-22）|
| P1-2 | LLM 分析结果缓存 | ✅ 已完成（2026-07-22）|
| P1-3 | 端点级限流 | ✅ 已完成（2026-07-22）|
| P1-4 | Dashboard 查询缓存 | ✅ 已完成 |

---

## 八、下一步执行计划

> **全部收口完成**（2026-07-23）：P0/P1/P2/P3 全部清零，未确认项 C5/C4/H7 全部核实。
> 此前 SEC-12/L4/L5/L7/M5/M8/M13 + 本次 M3/L6 已实现但被误标为待处理，均已修正。M10/M11 为本次实际动手项。

### P2 — 中优先级（3 项 ✅ 全部完成）

| 任务 | 说明 | 状态 |
|------|------|------|
| M3 同步阻塞 | dispatch 层 `asyncio.to_thread` 包装 + sync 路由 threadpool；`test_mcp_verify_ui.py` 证明不阻塞 | ✅ 已完成（2026-07-23）|
| M10 版本口径 | `__version__` 单一来源 + 创建 `v0.3.0` annotated tag + DEV_PLAN §八 release 流程 | ✅ 已完成（2026-07-23）|
| M11 migrations 清理 | 删除 network_records/ui_events 废弃迁移文件 + 修正 init_db.sh 注释 | ✅ 已完成（2026-07-23）|

### P3 — 低优先级（1 项 ✅ 全部完成）

| 任务 | 说明 | 状态 |
|------|------|------|
| L6 docker-compose | `docker-compose.yaml` 已透传 LLM_PROVIDER/LLM_BASE_URL 等全部 LLM 环境变量 | ✅ 已完成（2026-07-23）|

### 架构级优化（Phase 5-7）

| 阶段 | 任务 | 说明 | 依赖 | 状态 |
|------|------|------|------|------|
| Phase 5 | P3-3 批量写入 | storage ABC 新增 `save_entries` 默认实现 + MemoryTraceStore 覆写（单次锁）+ logs `add_logs_batch` + trace_repo save_trace 复用（META+LINK 批量，DATA 保留提交标记） | 无 | ✅ 已完成（2026-07-24）|
| Phase 5 | P3-5 优雅降级 | config 新增 `storage_fallback_to_memory` 配置 + factory 层 PG 构造异常捕获 + 自动降级 memory + fail-fast 模式 | 无 | ✅ 已完成（2026-07-24）|
| Phase 5 | P3-1 分区 | traces 表按月 RANGE 分区（PostgreSQL 声明式分区，非 pg_partman） + 自动预创建未来 N 个月分区 + 惰性检查 | 无 | ✅ 已完成（2026-07-24）|
| Phase 5 | P3-2 归档 | >N 天数据自动归档到 traces_archive 表 + cleanup_expired 先归档再删除 + 配置项控制 | P3-1 | ✅ 已完成（2026-07-24）|
| Phase 6 | P3-4 OpenTelemetry | 双模式设计——保留 `/metrics` Prometheus 文本端点向后兼容，同时引入 OTel SDK 支持 OTLP gRPC 导出；核心指标：`http_requests_total`、`http_errors_total`、`http_request_duration_seconds`；惰性初始化 + 失败降级；优雅关闭 | Phase 3 | ✅ 已完成（2026-07-24）|
| Phase 6 | P3-8 熔断器 | pybreaker 包装 LLM/PG 调用，config 配置项控制熔断参数，熔断时返回结构化 fallback | 无 | ✅ 已完成（2026-07-24）|
| Phase 6 | P3-6 消息队列削峰 | 有界 `asyncio.Queue(maxsize=N)` + K 常驻消费协程 + `asyncio.Semaphore(K)` 对齐 LLM RPM/TPM；队列满返回 429；优雅停机 drain；零侵入 analyzer.py（消费协程延迟导入）；新增 `POST /api/debug/analyze/async` + `GET /api/debug/analyze/result/{job_id}` | 无 | ✅ 已完成（2026-07-25）|
| Phase 7 | 智能错误分析引擎 | 指纹聚合统计（aggregate_by_fingerprint）+ 根因排序（rank_by_impact）+ PG errors 表查询（query_pg_errors）+ 3 个 dashboard API 端点 | 无 | ✅ 已完成（2026-07-24）|
| Phase 7 | 向量检索 RAG（in-process + Qdrant） | `VectorStore` ABC 纯检索语义 `add(docs)`/`search(query, top_k)`；`InProcessVectorStore` Jaccard 实现；`QdrantVectorStore` OpenAI/智谱 Embeddings 语义召回 + uuid5 幂等 upsert（`app/rag/qdrant_vector_store.py:254-257` 取 `doc["fingerprint"]` 经 `uuid.uuid5(uuid.NAMESPACE_DNS, str(fingerprint))` 生成确定性 point id，同 fingerprint 重分析覆盖而非新增；`L271-275` `client.upsert(wait=True)` 同步落库保证一致性）+ 静默降级；工厂 + 注册表插槽；analyzer.py KB hook 区集成作为精确指纹 miss 后二级 fallback | 无 | ✅ 已完成（2026-07-26）|
| AUDIT-2-13 | RBAC 角色分级 | `app/auth/rbac.py`：admin > developer > viewer 三级 + `require_role(*roles)` FastAPI 依赖工厂；未启用时全 admin（向后兼容）；未命中映射默认 viewer（fail-closed） | 无 | ✅ 已完成（2026-07-25）|
| AUDIT-2-14 | API_KEY 多 key 轮换 | `app/auth/key_rotation.py`：`api_keys` 逗号分隔优先，空时回退单 `api_key`；`verify_api_key` 遍历所有 key 不短路 + `hmac.compare_digest` 防时序侧信道；`AuthMiddleware` 公共签名未变（仅体内修改） | 无 | ✅ 已完成（2026-07-25）|
| Phase 7 | AI Debug Agent Phase 1（自动修复） | `app/agent/` 模块（7 文件）——`BaseAgent` ABC + `AgentContext`/`AgentResult`/`AgentTrace` + `AgentStatus` 枚举、`RepairAgent`（复用 `analyzer._get_async_client`，独立重试/fallback + `_validate_repair_plan` 容错 JSON）、`RepairContextAssembler`（并发聚合 `analyze_async` + `retrieve_similar` + `get_recent_diff`，各失败静默降级）、`RepairQueue`（结构对称 `analysis_queue.py`，有界 `asyncio.Queue` + `Semaphore(K)` + K 常驻消费协程 + `drain`）、`Coordinator` 编排器（装配上下文 → 调度 Agent → 收集 trace）。新增 2 REST 端点（`POST /api/debug/repair/async` + `GET /api/debug/repair/result/{job_id}`）+ 2 MCP 工具（`repair_async` + `repair_result`，工具数 15→17）。9 个 `agent_*` 配置项（`agent_enabled` 默认 False）。Phase 1 = 单 Agent + 多 Agent 协同框架预留，Phase 2 多 Agent DAG 为后续待办（AGENT-002） | Qdrant 语义召回已就绪 | ✅ 已完成（2026-07-26）|

### Browser SDK 续作

| 版本 | 方向 | 说明 |
|------|------|------|
| V3 | 网络错误自动标记静默失败 | ✅ 已完成，XHR/fetch error → 静默失败检测 |
| V4 | SDK 初始化追踪 + 请求关联 | ✅ 已完成，trace_id 贯穿 SDK → 后端 |
| V5 | 增强 ingest 端点 | ✅ 已完成基础批量 ingest；压缩传输仍可作为后续增强 |
| V6 | 自动检测 UI 静默失败 | ✅ 已完成，DOM 变化检测 + 断言 |

---

## 九、当前状态

- **测试基线**：单元 `583 passed / 6 skipped / 0 failed`（基线 520 + AI Debug Agent Phase 1 新增 63 项：6 单测文件覆盖 `BaseAgent` ABC / `RepairAgent` 重试 fallback / `RepairContextAssembler` 并发降级 / `RepairQueue` 削峰 drain / `Coordinator` 编排 / schemas 校验；3 集成测试 8 用例 e2e skip-if-no-api-key）；集成 `49 passed / 19 skipped / 0 failed`（test_api.py 8 个鉴权 401 基线已修复：conftest `os.environ["API_KEY"]=""` env var 优先于 .env → M7 归一化关鉴权 + `HOST=127.0.0.1` 避开 SEC-03）；ruff AI Debug Agent 文件 0 违规（3 处预存违反位于 ui_runner.py / test_sdk_v5_enhancements.py / test_otel_collector_integration.py，不在本轮范围）
- **健康度评分**：6.5/10（↓2.0）——工程质量 8.0，安全性 5.0（P0×6 严重），架构可维护性 7.0（代码重复严重），文档可信度 6.0（Phase 2 架构缺失）
- **beta-release 全量审查**（2026-07-27）：5 维度 × 5 Agent 并行扫描，发现 P0×6 + P1×9 + P2×12 + 文档×5 = 32 项。**结论：不能上线，不能开源。**
- **技术债清理**（2026-07-23）：`test_full_flow.py` 硬编码 PG 密码已修复（`ad6f8dd`，改由 `.env` 经 `settings` 读取）；`pg_store.py` 拆分评估完成（有条件值得，方案 C，详见 [ROADMAP.md](../ROADMAP.md) 技术债务）
- **Phase 5-7 完成项**（2026-07-24）：P3-1 分区（PostgreSQL 声明式 RANGE 分区）、P3-2 归档（traces_archive 表）、P3-3 批量写入、P3-4 OpenTelemetry、P3-5 优雅降级、P3-8 熔断器、Phase 7 智能错误分析引擎
- **三轨并行完成项**（2026-07-25）：P3-6 消息队列削峰（有界 asyncio.Queue + Semaphore(K) + K 常驻消费协程）、Phase 7 向量检索 RAG 抽象层（in-process Jaccard + 工厂/注册表插槽）、AUDIT-2-13 RBAC 角色分级、AUDIT-2-14 API_KEY 多 key 轮换
- **Qdrant 适配器 + L3 预热完成项**（2026-07-26）：Qdrant 向量检索适配器（OpenAI/智谱 Embeddings 语义召回 + uuid5 幂等 upsert + 静默降级）、P3-7 L3 缓存预热（只写 L1 不刷新 L2 TTL）
- **AI Debug Agent Phase 1 完成项**（2026-07-26）：`app/agent/` 模块（7 文件）——`BaseAgent` ABC + `RepairAgent` + `Coordinator` 编排器 + `RepairQueue` 削峰队列 + `RepairContextAssembler`（并发聚合 analyze + retrieve_similar + get_recent_diff，各失败静默降级）；2 REST 端点 + 2 MCP 工具（工具数 15→17）；9 个 `agent_*` 配置项（`agent_enabled` 默认 False）；Phase 1 单 Agent + 多 Agent 协同框架预留
- **下一目标**：❌ **先清 P0×6 阻断项**，再考虑 Phase 2 多 Agent DAG（AGENT-002）、Browser SDK 端到端联调、Docker 容器化验证
- **代码审查**：beta-release 全量审查已完成（见 §十一），32 项发现待处理

---

## 十、AI_RULES 约束检查

| 约束 | 状态 | 说明 |
|------|------|------|
| 禁止绕过 Storage | ✅ | 未修改 |
| 禁止新建数据库连接 | ✅ | 未修改 |
| 禁止引入 SQLAlchemy/Alembic | ✅ | 未引入 |
| PGStore 修改需审批 | ⚠️ | SEC-14 已完成，需确认 |
| 禁止硬编码密钥 | ✅ | 未修改 |
| 禁止绕过中间件安全栈 | ✅ | 未修改 |
| 最小修改原则 | ✅ | 每项只改必要文件 |

---

## 十一、beta-release Phase 2 全量审查（2026-07-27）

> **审查范围**：beta-release 分支 vs main，29 个修改文件 + 10 个新增文件，~2500 行 diff。
> **审查维度**：5 个 Agent 并行扫描——A 安全/权限、B 删除行为审计、C 文档一致性、D 阻断项、E 代码复用。
> **审查结论**：❌ **不能上线，不能开源。** P0×6 阻断项任一可被利用导致安全事故。

### 状态总览

| 优先级 | 数量 | 上线 | 开源 | 说明 |
|--------|------|------|------|------|
| P0 阻断 | 6 | ⚠️ 2已修/3误报/1待议 | ❌ | 2 确认修复 + 3 误报（JWT不存在/CORS已安全/无文件写入）+ 1 待议（SSE URL key 浏览器限制） |
| P1 必修 | 9 | ✅ 1已修/7误报/1已修 | ⚠️ | P1-02~04/06/07/09 误报；P1-01 已修复；P1-05 误报；P1-08 已随 P0-01 修复 |
| P2 建议 | 13 | ✅ 2已修/7待议/4误报 | ⚠️ | P2-01/05/06/07 设计问题待议；P2-02/10 误报；P2-08/09 已修复；P2-03/04/11/12/13 低优先 |
| 文档脱节 | 5 | ✅ | ⚠️ | PRD 路径过期·保留期不匹配·Phase 2 架构缺失·README 中英不一致 |

---

### P0 — 阻断上线·开源（6 项）

#### BETA-P0-01：Dashboard 前端鉴权完全缺失
- **文件**：`app/web/dashboard.html:131`
- **问题**：所有 `fetch()` 调用不携带 API Key。启用 `API_KEY` 后 Dashboard 全部 401，页面空白不可用。
- **影响**：生产部署标配 `API_KEY`，此问题直接导致 Dashboard 零功能。
- **状态**：✅ 已修复（2026-07-31）—— `fetchJSON` 从 URL query param 读取 `api_key` 并通过 `Authorization: Bearer` header 传递

#### BETA-P0-02：JWT HS256 硬编码降级密钥
- **文件**：`app/auth/jwt_auth.py:58`
- **问题**：`JWT_SECRET` 未配置时降级为硬编码字符串。任何部署者忘记配置 = 所有 token 可伪造。
- **影响**：应启动时 fail-fast 而非静默降级。
- **状态**：❌ 误报——`app/auth/jwt_auth.py` 不存在，项目无 JWT 实现，鉴权基于 API Key

#### BETA-P0-03：CORS 通配符
- **文件**：`app/main.py:78`
- **问题**：`allow_origins=["*"]`，任意域名可跨域调用所有 API。
- **影响**：生产必须收紧为显式域名列表。
- **状态**：❌ 误报——`middleware.py:229` 默认 `cors_origins=""`（不下发 CORS 头），`"*"` 需显式 `CORS_ORIGINS=*` opt-in，已有安全默认值

#### BETA-P0-04：TOOL_ROLE_REQUIREMENTS 缺 fallback
- **文件**：`app/mcp/tools/__init__.py:37`
- **问题**：新增 MCP 工具若未在此 dict 注册，RBAC 门控直接跳过，viewer 可调用写类工具。
- **影响**：安全回归静默发生，无任何警告。
- **状态**：✅ 已修复（2026-07-31）—— 未注册工具默认要求 `admin` 角色（fail-closed）+ warning 日志

#### BETA-P0-05：API Key 通过 URL 查询参数传递
- **文件**：`app/api/dashboard.py:216`
- **问题**：SSE 用 `?api_key=xxx`，浏览器历史/服务器日志/代理日志全部记录明文 key。
- **影响**：标准做法应为 Authorization header 或短期 token。
- **状态**：⬜ 待修

#### BETA-P0-06：save_result 文件名注入
- **文件**：`app/agent/security_agent.py:135`，同模式在 `test_agent.py:261`
- **问题**：`f"security_review_{trace_id}.json"` 未校验 trace_id，路径遍历可写任意文件。
- **影响**：需 `re.sub(r'[^a-zA-Z0-9_-]', '', trace_id)` 白名单过滤。
- **状态**：❌ 误报——Agent 代码无 `open()`/`write()` 文件操作，结果通过内存 job 系统返回，无磁盘写入

---

### P1 — 上线前必须修（9 项）

#### BETA-P1-01：Dashboard SSE 永不终止
- **文件**：`app/api/dashboard_events.py:44`
- **问题**：队列满时 close 事件被静默丢弃，SSE 流永不终止，连接泄漏。
- **状态**：✅ 已修复（2026-07-31）—— `close_all` 改用 `_put_nowait`（队列满时丢旧保最新，确保 close 事件送达）

#### BETA-P1-02：JWT 无法主动失效
- **文件**：`app/auth/jwt_auth.py`
- **问题**：无 token blacklist / 版本号机制。用户被降权后旧 token 仍然有效直到过期。
- **状态**：❌ 误报——项目无 JWT 实现

#### BETA-P1-03：多 Worker 下 JWT Key 不同步
- **文件**：`app/auth/jwt_auth.py:67`
- **问题**：`_CURRENT_KEY` 是进程级变量，多 Gunicorn worker 各自生成不同 key，互相无法验证签名。
- **状态**：❌ 误报——项目无 JWT 实现

#### BETA-P1-04：id_token 返回到前端
- **文件**：`app/auth/jwt_auth.py:127`
- **问题**：Google OAuth 的 id_token 直接塞进 redirect URL query string，浏览器历史/日志可见。
- **状态**：❌ 误报——项目无 JWT/OAuth 实现

#### BETA-P1-05：全局 ExceptionHandler 泄露堆栈
- **文件**：`app/main.py:122`
- **问题**：非 DEBUG 模式下异常响应仍含完整 traceback，暴露内部实现细节。
- **状态**：❌ 误报——`error_handlers.py:28` 只返回 `{type(exc).__name__}` 类名（如 "RuntimeError"），不含堆栈或消息，traceback 仅写服务端日志

#### BETA-P1-06：Agent _skipped() 报 "未配置" 但实际可能是 API 失败
- **文件**：`app/agent/git_agent.py:163`, `test_agent.py:271`, `security_agent.py:310`
- **问题**：`error="xxx agent not configured"` 常量误导运维，实际原因可能是 API key 过期或模型超时。
- **状态**：❌ 误报——实际 reason 为描述性文本（"no stack frames to attribute"、"repair_plan unavailable, skip security review" 等），非 "not configured" 常量

#### BETA-P1-07：RBAC 认证失败返回 403 而非 401
- **文件**：`app/auth/rbac.py:67`
- **问题**：凭证无效/过期返回 403 Forbidden 而非 401 Unauthorized，客户端无法区分"未认证"和"无权限"。
- **状态**：❌ 误报——`require_role` 做授权（403 正确），鉴权由 `AuthMiddleware` 做（返回 401），职责分离正确

#### BETA-P1-08：Dashboard 无独立鉴权中间件
- **文件**：`app/api/dashboard.py`
- **问题**：依赖 query 参数而非标准 Authorization header，与 API 端点鉴权方式不一致。
- **状态**：✅ 已随 P0-01 修复——`fetchJSON` 现通过 `Authorization: Bearer` header 传递 key

#### BETA-P1-09：写操作无速率限制
- **文件**：`app/api/ingest.py`, `app/api/debug.py`
- **问题**：无 auth + 无 rate limit = 任何人都可灌垃圾数据耗尽内存。
- **状态**：❌ 误报——`middleware.py:134` `ENDPOINT_LIMITS` 已对 `/ingest/*` 设置 120/min、`/api/debug/analyze` 设置 10/min 端点级限流

---

### P2 — 开源前建议修（13 项）

#### BETA-P2-01：三个 Agent 文件 ~300 行逐字复制
- **文件**：`repair_agent.py` / `test_agent.py` / `security_agent.py`
- **问题**：`_extract_json`、`_truncate_field`、`_call_llm_with_retry`、`_skipped` 全部 copy-paste。修一处需改四处，遗漏即静默回归。
- **状态**：⚠️ 设计债——已部分缓解（P2-09 正则修复已同步 3 文件），完整重构需提 `BaseAgent` 公共方法
- **影响**：不阻塞上线/开源，建议后续迭代抽取公共基类方法

#### BETA-P2-02：Dashboard L1 缓存无驱逐无锁
- **文件**：`app/api/dashboard.py:19`
- **问题**：plain dict 无 TTL 驱逐，长时间运行内存无限增长。`analyzer.py` 已有成熟 LRU 实现可复用。
- **状态**：❌ 误报——`_cache` 仅存一个 key（`"all_traces"`），TTL 30s 由调用方检查，单 key 场景无需 LRU；`invalidate_cache` 的 `pop` + 后续 `set` 受 GIL 保护

#### BETA-P2-03：SSE 端点默认 maxSizeKb=256 偏小
- **文件**：`app/config.py:60`
- **问题**：256KB 拦截大型 trace，512KB 更安全。文档未说明此限制。
- **状态**：⬜ 待修

#### BETA-P2-04：fallback 递归 messages[:3] 截断无效
- **文件**：`app/agent/test_agent.py:261`
- **问题**：当前 messages 仅 2 条，[:3] 不截断。未来 prompt 扩展后静默丢弃上下文。
- **状态**：⚠️ 低风险——当前 2 条消息，[:3] 不截断；未来 prompt 扩展时需审查

#### BETA-P2-05：ctx.repair_context 原地 mutation
- **文件**：`app/agent/coordinator.py:158`
- **问题**：同一 ctx 传给并行 Agent，当前仅读安全，但未来扩展易引入竞态。
- **状态**：⚠️ 设计债——当前并行 Agent 仅读 repair_context，无写操作；未来扩展时需改为不可变传递

#### BETA-P2-06：DAG 注释与实现不一致
- **文件**：`app/agent/dag.py:11`
- **问题**：注释称 GitAgent 不依赖 repair_plan 可并行，实际强制串行等待 RepairAgent 完成。
- **状态**：✅ 已修正注释（dag.py:15-16 已说明"为简化 DAG 拓扑与 trace 顺序，统一在 RepairAgent 之后并行执行"）

#### BETA-P2-07：PHASE2_AGENTS 模块级单例从未被使用
- **文件**：`app/agent/dag.py:37`
- **问题**：`build_phase2_agents()` 才是实际路径，模块级单例空闲浪费且误导开发者。
- **状态**：⚠️ 低优先——`PHASE2_AGENTS` 仅用于 `get_phase2_agent_names()` 取 keys，`build_phase2_agents()` 创建隔离实例；单例存在但不影响正确性

#### BETA-P2-08：MCP dispatch 异常返回 400 而非 500
- **文件**：`app/mcp/routes.py:127`
- **问题**：内部 bug 被掩盖为客户端错误，问题定位延迟。
- **状态**：✅ 已修复（2026-07-31）—— `status_code=400` 改为 `status_code=500`

#### BETA-P2-09：_extract_json 正则贪婪匹配
- **文件**：`app/agent/security_agent.py:60`
- **问题**：`(\{.*\})` + DOTALL 贪婪匹配，LLM 返回多段 JSON 时捕获超大字符串导致 `json.loads` 失败。
- **状态**：✅ 已修复（2026-07-31）—— 3 个 Agent 文件（security/test/repair）统一改为非贪婪 `.*?`

#### BETA-P2-10：Dashboard API 返回已移除的 _cached 字段
- **文件**：`app/api/dashboard.py:400`
- **问题**：`build_context()` 返回 `_cached` 字段，文档和代码均未说明用途，前端未使用。
- **状态**：❌ 误报——`dashboard.py` 中无 `_cached` 字段，grep 确认不存在

#### BETA-P2-11：/metrics 鉴权行为变更未文档化
- **文件**：`app/observability.py:225`
- **问题**：旧代码在 `METRICS_AUTH_ENABLED=true` 但未配置 `API_KEY` 时允许访问。新代码改用 `verify_api_key()` 后该场景返回 401。监控系统可能静默中断。
- **说明**：安全加固方向正确，但属行为变更，需在 release notes 中标注。
- **状态**：⬜ 待修

#### BETA-P2-12：TOOL_ROLE_REQUIREMENTS 无程序化覆盖校验
- **文件**：`app/mcp/tools/__init__.py:47`
- **问题**：当前 17 个工具已全部列出，但无测试或断言校验覆盖完整性。新增工具若遗漏则静默绕过 RBAC。
- **状态**：⬜ 待修

#### BETA-P2-13：rbac_enabled=False 分支无测试
- **文件**：`app/auth/rbac.py:71`
- **问题**：新增的向后兼容守卫（`role is None + rbac_enabled=False → return "admin"`）无测试覆盖。未来重构反转条件时无回归保护。
- **状态**：⬜ 待修

---

### 文档脱节（5 项）

#### BETA-DOC-01：PRD/ROADMAP 引用已迁移路径
- **文件**：`docs/internal/PRD.md:223`, `docs/internal/ROADMAP.md:114`
- **问题**：`app/llm/rag_index.py` → 实际已迁移到 `app/rag/index.py`。
- **状态**：⬜ 待修

#### BETA-DOC-02：PRD 数据保留期与配置默认值不匹配
- **文件**：`docs/internal/PRD.md:296` vs `app/config.py:43`
- **问题**：PRD 写 7 天，默认值 3 天。
- **状态**：⬜ 待修

#### BETA-DOC-03：ROADMAP Phase 4.5 行未标记完成
- **文件**：`docs/internal/ROADMAP.md:122`
- **问题**：Phase 4.5 实际已完成，文档未勾选。
- **状态**：⬜ 待修

#### BETA-DOC-04：DESIGN.md 缺 Phase 2 架构
- **文件**：`docs/internal/DESIGN.md`
- **问题**：多 Agent DAG 协调、Dashboard SSE 实时推送均无架构描述。与代码实现严重脱节。
- **状态**：⬜ 待修

#### BETA-DOC-05：README 中英文不一致
- **文件**：`README.md`
- **问题**：项目定位为中文优先，README 英文部分远多于中文，影响开源形象。
- **状态**：⬜ 待修

---

### 被删除行为审计（Agent B，4 项）

> 以下为 beta-release diff 中被删除/替换的不变量，检查新代码是否重新建立。

| 编号 | 文件 | 行为变更 | 风险 |
|------|------|----------|------|
| BETA-DEL-01 | `app/observability.py:225` | `/metrics` 鉴权从 `hmac.compare_digest(key, settings.api_key or "")` 改为 `verify_api_key(key)`。旧代码在无 API_KEY 时允许访问，新代码返回 401 | P2 — 安全加固方向正确，需文档化 |
| BETA-DEL-02 | `app/api/mcp_routes.py:91` | 新增 `TOOL_ROLE_REQUIREMENTS.get(tool_name)` 检查，未列出工具直接跳过 RBAC | P0 — 已收录为 BETA-P0-04 |
| BETA-DEL-03 | `app/auth/rbac.py:71` | 新增 `rbac_enabled=False` 时 `role=None → "admin"` 向后兼容守卫，但无测试覆盖 | P2 — 已收录为 BETA-P2-13 |
| BETA-DEL-04 | `app/agent/coordinator.py:105` | 三重兜底注释被删除，代码逻辑未变但设计意图文档丢失 | P2 — 维护者可能误删防御层 |

---

### 代码复用审计（Agent E，8 项）

> 以下为 beta-release 新增/修改文件中的高重复度代码段。

| 编号 | 重复位置 | 重复行数 | 说明 |
|------|----------|----------|------|
| BETA-DUP-01 | `_extract_json()` × 3 Agent + analyzer.py | 13行×4处 | regex 略有差异（贪婪 vs 非贪婪），修一处需改四处 |
| BETA-DUP-02 | `_truncate_field()` × 3 Agent + analyzer.py | 5行×4处 | 逻辑完全一致 |
| BETA-DUP-03 | `_call_llm_with_retry()` × 3 Agent | ~80行×3处 | 仅 logger 名和 validate 函数不同 |
| BETA-DUP-04 | `_validate_*()` JSON 解析前导块 × 3 Agent | 15行×3处 | try→except→_extract_json→retry 模式逐字复制 |
| BETA-DUP-05 | `_skipped()` × git/test/security Agent | 8行×3处 | 仅 agent_name 不同，未提取到 BaseAgent |
| BETA-DUP-06 | 上下文聚合 20 行块 × debug.py 3 端点 | 20行×3处 | get_logs→build_context→promote→collect 模式 |
| BETA-DUP-07 | Dashboard L1 缓存 vs analyzer.py LRU | 功能重复 | Dashboard 用 plain dict（无驱逐无锁），analyzer 已有成熟实现 |
| BETA-DUP-08 | 代码注释残留中文 TODO | 多处 | `test_plan.py:191`、`coordinator.py:109` 等 |

---

### 附：完整发现清单（JSON，供自动化消费）

<details>
<summary>点击展开 32 项发现的 JSON 格式</summary>

```json
[
  {"id":"BETA-P0-01","file":"app/web/dashboard.html","line":131,"severity":"P0","category":"security","summary":"Dashboard 前端所有 fetch 请求不携带 API Key，启用鉴权后全部 401"},
  {"id":"BETA-P0-02","file":"app/auth/jwt_auth.py","line":58,"severity":"P0","category":"security","summary":"JWT HS256 硬编码降级密钥，未配置时所有 token 可伪造"},
  {"id":"BETA-P0-03","file":"app/main.py","line":78,"severity":"P0","category":"security","summary":"CORS 通配符 allow_origins=['*']"},
  {"id":"BETA-P0-04","file":"app/mcp/tools/__init__.py","line":37,"severity":"P0","category":"rbac","summary":"TOOL_ROLE_REQUIREMENTS 缺 fallback，未注册工具跳过 RBAC"},
  {"id":"BETA-P0-05","file":"app/api/dashboard.py","line":216,"severity":"P0","category":"security","summary":"API Key 通过 URL 查询参数传递，日志/历史记录泄露"},
  {"id":"BETA-P0-06","file":"app/agent/security_agent.py","line":135,"severity":"P0","category":"security","summary":"save_result 文件名未校验 trace_id，路径遍历可写任意文件"},
  {"id":"BETA-P1-01","file":"app/api/dashboard_events.py","line":44,"severity":"P1","category":"blocking","summary":"队列满时 close 事件被丢弃，SSE 流永不终止"},
  {"id":"BETA-P1-02","file":"app/auth/jwt_auth.py","line":0,"severity":"P1","category":"security","summary":"JWT 无 token blacklist，无法主动失效"},
  {"id":"BETA-P1-03","file":"app/auth/jwt_auth.py","line":67,"severity":"P1","category":"security","summary":"多 Worker 下 _CURRENT_KEY 不同步"},
  {"id":"BETA-P1-04","file":"app/auth/jwt_auth.py","line":127,"severity":"P1","category":"security","summary":"id_token 返回到前端 URL query string"},
  {"id":"BETA-P1-05","file":"app/main.py","line":122,"severity":"P1","category":"security","summary":"全局 ExceptionHandler 非 DEBUG 模式泄露完整 traceback"},
  {"id":"BETA-P1-06","file":"app/agent/git_agent.py","line":163,"severity":"P1","category":"observability","summary":"_skipped() 报 '未配置' 常量误导运维"},
  {"id":"BETA-P1-07","file":"app/auth/rbac.py","line":67,"severity":"P1","category":"rbac","summary":"认证失败返回 403 而非 401"},
  {"id":"BETA-P1-08","file":"app/api/dashboard.py","line":0,"severity":"P1","category":"security","summary":"Dashboard 无独立鉴权中间件"},
  {"id":"BETA-P1-09","file":"app/api/ingest.py","line":0,"severity":"P1","category":"security","summary":"写操作 ingest/traces/{id}/tags 无速率限制"},
  {"id":"BETA-P2-01","file":"app/agent/repair_agent.py","line":49,"severity":"P2","category":"maintainability","summary":"3 个 Agent 文件 ~300 行逐字复制"},
  {"id":"BETA-P2-02","file":"app/api/dashboard.py","line":19,"severity":"P2","category":"performance","summary":"Dashboard L1 缓存无驱逐无锁"},
  {"id":"BETA-P2-03","file":"app/config.py","line":60,"severity":"P2","category":"config","summary":"SSE 端点默认 maxSizeKb=256 偏小"},
  {"id":"BETA-P2-04","file":"app/agent/test_agent.py","line":261,"severity":"P2","category":"correctness","summary":"fallback 递归 messages[:3] 截断无效"},
  {"id":"BETA-P2-05","file":"app/agent/coordinator.py","line":158,"severity":"P2","category":"concurrency","summary":"ctx.repair_context 原地 mutation，未来扩展易引入竞态"},
  {"id":"BETA-P2-06","file":"app/agent/dag.py","line":11,"severity":"P2","category":"documentation","summary":"DAG 注释称可并行但代码强制串行"},
  {"id":"BETA-P2-07","file":"app/agent/dag.py","line":37,"severity":"P2","category":"maintainability","summary":"PHASE2_AGENTS 模块级单例从未被使用"},
  {"id":"BETA-P2-08","file":"app/mcp/routes.py","line":127,"severity":"P2","category":"error-handling","summary":"MCP dispatch 异常返回 400 而非 500"},
  {"id":"BETA-P2-09","file":"app/agent/security_agent.py","line":60,"severity":"P2","category":"correctness","summary":"_extract_json 正则贪婪匹配，多段 JSON 时 parse 失败"},
  {"id":"BETA-P2-10","file":"app/api/dashboard.py","line":400,"severity":"P2","category":"dead-code","summary":"Dashboard API 返回未使用的 _cached 字段"},
  {"id":"BETA-P2-11","file":"app/observability.py","line":225,"severity":"P2","category":"documentation","summary":"/metrics 鉴权行为变更未文档化"},
  {"id":"BETA-P2-12","file":"app/mcp/tools/__init__.py","line":47,"severity":"P2","category":"rbac","summary":"TOOL_ROLE_REQUIREMENTS 无程序化覆盖校验"},
  {"id":"BETA-P2-13","file":"app/auth/rbac.py","line":71,"severity":"P2","category":"test-coverage","summary":"rbac_enabled=False 向后兼容分支无测试"},
  {"id":"BETA-DOC-01","file":"docs/internal/PRD.md","line":223,"severity":"DOC","category":"documentation","summary":"PRD/ROADMAP 引用已迁移路径 app/llm/rag_index.py"},
  {"id":"BETA-DOC-02","file":"docs/internal/PRD.md","line":296,"severity":"DOC","category":"documentation","summary":"PRD 数据保留期 7 天 vs 配置默认值 3 天"},
  {"id":"BETA-DOC-03","file":"docs/internal/ROADMAP.md","line":122,"severity":"DOC","category":"documentation","summary":"ROADMAP Phase 4.5 实际已完成但未标记"},
  {"id":"BETA-DOC-04","file":"docs/internal/DESIGN.md","line":0,"severity":"DOC","category":"documentation","summary":"DESIGN.md 缺 Phase 2 多 Agent DAG 架构描述"},
  {"id":"BETA-DOC-05","file":"README.md","line":0,"severity":"DOC","category":"documentation","summary":"README 中英文不一致，影响开源形象"}
]
```

</details>
