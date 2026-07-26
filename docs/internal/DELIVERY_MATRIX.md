# ai-debug-mcp 真实交付功能矩阵

> 本文档是当前项目功能完成度的**唯一权威口径**。  
> 判定标准仅以仓库中的真实代码、测试与运行前提为依据，不以历史文档表述为准。
>
> 状态定义：
>
> - `已完成`：代码已完整落地，默认链路可正常使用
> - `部分完成`：核心逻辑已存在，但仍有缺失分支、闭环缺口或待补测试
> - `需依赖环境`：代码已完成，但启用依赖特定环境、第三方服务或额外运行时
> - `仅配置预埋`：仅有配置入口或文档占位，缺少可用的功能实现

## 一、调试与协议能力

| 功能项 | 当前状态 | 代码依据 | 说明 |
| --- | --- | --- | --- |
| REST 调试入口 `/api/debug/run` | 已完成 | `app/api/debug.py` | 可接收请求、写 trace、构建上下文并返回调试结果 |
| LLM 分析 `/api/debug/analyze` | 需依赖环境 | `app/api/debug.py` `app/llm/analyzer.py` | 逻辑完整；依赖 OpenAI/智谱/自定义 LLM 服务 |
| 流式分析 `/api/debug/analyze/stream` | 需依赖环境 | `app/api/debug.py` `app/llm/analyzer.py` | SSE 流式输出已实现；依赖可用 LLM 服务 |
| 异步分析 `/api/debug/analyze/async` | 需依赖环境 | `app/api/debug.py` `app/llm/analysis_queue.py` | P3-6 削峰队列：有界 `asyncio.Queue(maxsize=N)` + K 常驻消费协程 + `asyncio.Semaphore(K)` 对齐 LLM RPM/TPM；队列满返回 429；优雅停机 drain；零侵入 analyzer.py |
| AI Debug Agent 修复 `/api/debug/repair/async` | 需依赖环境 | `app/api/debug.py` `app/agent/repair_queue.py` `app/agent/coordinator.py` | FR19 Phase 1：入队修复请求返回 `job_id`，队列满返回 429；`Coordinator` 编排上下文装配 → `RepairAgent` 执行 → trace 收集；`agent_enabled=False` 时路由不挂载；启用依赖外部 LLM 服务 |
| AI Debug Agent 修复结果 `/api/debug/repair/result/{job_id}` | 需依赖环境 | `app/api/debug.py` `app/agent/coordinator.py` | 轮询修复结果（含 `RepairPlan` + `AgentTrace`）；任一上下文子采集器失败静默降级，不阻断主链路 |
| MCP Streamable HTTP `POST /mcp` | 已完成 | `app/api/mcp_routes.py` `app/mcp/protocol/server.py` | initialize、tools/list、tools/call、ping 均已落地 |
| MCP Streamable HTTP `GET /mcp` SSE 长连接 | 已完成 | `app/api/mcp_routes.py` `app/mcp/transports/sse.py` | 已支持会话化订阅与消息推送消费 |
| MCP HTTP server->client notifications | 部分完成 | `app/api/mcp_routes.py` `app/mcp/transports/sse.py` | 会话 ready 与 POST SSE 结果桥接可推送；更丰富的服务端通知类型仍待扩展 |
| MCP stdio 传输 | 已完成 | `app/mcp_server.py` | 可作为 MCP 本地子进程服务运行 |
| MCP 工具注册（HTTP / stdio） | 已完成 | `app/mcp/tools/__init__.py` | 两种传输共用同一注册表，当前各 17 个工具（含 `repair_async` / `repair_result`，FR19） |
| JSON-RPC 错误码规范化 | 已完成 | `app/mcp/protocol/jsonrpc.py` `app/mcp/protocol/server.py` | 已区分 parse / invalid request / method not found / internal error |

## 二、调试引擎与验证能力

| 功能项 | 当前状态 | 代码依据 | 说明 |
| --- | --- | --- | --- |
| 请求追踪 Trace | 已完成 | `app/mcp/core/logs.py` `app/mcp/core/trace_repo.py` | 支持 request_id 维度追踪与持久化 |
| 调试上下文构建 | 已完成 | `app/mcp/builders/context.py` | 聚合 trace、runtime、code、git、spec、network 等信息 |
| 异常堆栈捕获 | 已完成 | `app/mcp/collectors/stacktrace.py` `app/mcp/hooks/exception_hook.py` | sync + asyncio 未捕获异常均可记录 |
| 代码定位与 IDE 跳转 | 已完成 | `app/mcp/collectors/code_locator.py` | 含白名单限制与 IDE scheme |
| 运行时快照 | 已完成 | `app/mcp/collectors/runtime.py` | 支持进程与解释器信息采集 |
| 静默失败检测 | 已完成 | `app/mcp/verifier/assert_engine.py` | `api/ui/rule` 三类断言已实现 |
| 规范 CRUD | 已完成 | `app/api/spec.py` `app/mcp/verifier/spec_store.py` | 创建、查询、更新、删除均已落地 |
| `verify` 自动校验 | 已完成 | `app/mcp/tools/verify_api.py` `app/api/debug.py` | 支持规范驱动结果校验 |
| `verify_ui` UI 校验 | 需依赖环境 | `app/mcp/verifier/ui_runner.py` `app/mcp/tools/verify_ui_api.py` | 代码完整；依赖 Playwright/Chromium 与目标页面环境 |
| `auto_test` 页面自动遍历 | 需依赖环境 | `app/mcp/tools/auto_test_api.py` | 依赖 Playwright/Chromium 与目标页面环境 |
| 指纹知识库命中与自动沉淀 | 已完成 | `app/rag/knowledge_base.py` `app/llm/analyzer.py` | 进程内最小知识库已落地；命中时返回 `knowledge_base_hit`/`analysis_source`，LLM 成功后自动沉淀 |
| 向量检索 RAG（in-process + Qdrant） | 已完成 | `app/rag/vector_store.py` `app/rag/qdrant_vector_store.py` `app/llm/analyzer.py` | `VectorStore` ABC + `InProcessVectorStore`（Jaccard 相似度）+ `QdrantVectorStore`（OpenAI/智谱 Embeddings 语义召回）；精确指纹 miss 后做向量召回 fallback；Qdrant 不可用时静默降级 |
| AI Debug Agent Phase 1（自动修复） | 需依赖环境 | `app/agent/`（7 文件）`app/api/debug.py` `app/mcp/tools/repair_api.py` | `BaseAgent` ABC + `RepairAgent` + `Coordinator` 编排器 + `RepairQueue` 削峰队列 + `RepairContextAssembler`（并发聚合 analyze + retrieve_similar + get_recent_diff，各失败静默降级）；2 REST 端点（`POST /api/debug/repair/async` + `GET /api/debug/repair/result/{job_id}`）+ 2 MCP 工具（`repair_async` + `repair_result`）；9 个 `agent_*` 配置项（`agent_enabled` 默认 False）；Phase 1 单 Agent + 多 Agent 协同框架预留，Phase 2 多 Agent DAG 为后续待办；启用依赖外部 LLM 服务 |

## 三、浏览器 SDK 与采集链路

| 功能项 | 当前状态 | 代码依据 | 说明 |
| --- | --- | --- | --- |
| XHR / fetch 网络拦截 | 已完成 | `browser-sdk/ai-debug.js` | 支持请求与响应信息采集 |
| 错误上报 | 已完成 | `browser-sdk/ai-debug.js` `app/api/ingest.py` | 错误链路真实可用 |
| 静默失败上报 | 已完成 | `browser-sdk/ai-debug.js` `app/api/ingest.py` | SDK 与服务端 ingest 均已接线 |
| UI 事件上报 | 已完成 | `browser-sdk/ai-debug.js` `app/api/ingest.py` | 点击与交互轨迹可入库 |
| 批量上报 / `sendBeacon` 兜底 | 已完成 | `browser-sdk/ai-debug.js` `app/api/ingest.py` | 已支持批量 flush 与 beacon |
| SDK 采样 / 节流 / 自排除 | 已完成 | `browser-sdk/ai-debug.js` | 采样率、节流与自排除均已实现 |
| 网络错误自动标记静默失败 | 已完成 | `browser-sdk/ai-debug.js` | fetch / XHR 失败可自动转为 silent failure，并支持 `reportNetworkError()` 手动上报 |
| SDK trace_id 初始化与请求关联 | 已完成 | `browser-sdk/ai-debug.js` `app/api/ingest.py` | 初始化即生成 trace_id，并通过 header / payload 贯穿 SDK 生命周期内事件 |
| UI 静默失败自动检测 | 已完成 | `browser-sdk/ai-debug.js` `app/web/silent_failure_demo.html` | 点击 / 提交后在观察窗口内无 DOM、路由、网络变化时自动上报 silent failure |

## 四、存储与数据能力

| 功能项 | 当前状态 | 代码依据 | 说明 |
| --- | --- | --- | --- |
| Memory Store | 已完成 | `app/mcp/core/storage/memory_store.py` | 默认可用 |
| PostgreSQL Store | 需依赖环境 | `app/mcp/core/storage/pg_store.py` | 需 PostgreSQL 环境 |
| asyncpg 异步存储 | 需依赖环境 | `app/mcp/core/storage/async_pg_store.py` `app/mcp/core/storage/factory.py` | 需 PostgreSQL + `PG_ASYNC_ENABLED=true` |
| Storage Factory 自动切换 | 已完成 | `app/mcp/core/storage/factory.py` | memory / postgresql / asyncpg 切换逻辑已落地 |
| PG 不可用自动降级 memory | 已完成 | `app/mcp/core/storage/factory.py` | 由 `storage_fallback_to_memory` 控制 |
| errors 表聚合持久化 | 需依赖环境 | `app/mcp/core/errors.py` `app/mcp/core/storage/pg_store.py` | 需 PG 环境才能持久化 |
| specs 独立表持久化 | 需依赖环境 | `app/mcp/verifier/spec_store.py` `app/mcp/core/storage/pg_store.py` | 需 PG 环境才能持久化到独立表 |
| traces 分区 | 需依赖环境 | `app/mcp/core/storage/pg_store.py` `app/mcp/core/storage/async_pg_store.py` | 代码完整；需 PG + `PG_PARTITION_ENABLED=true` |
| traces 归档 | 需依赖环境 | `app/mcp/core/storage/pg_store.py` `app/mcp/core/storage/async_pg_store.py` | 代码完整；需 PG + `PG_ARCHIVE_ENABLED=true` |
| 批量写入 | 已完成 | `app/mcp/core/logs.py` `app/mcp/core/trace_repo.py` | memory 路径默认可用；PG 复用 ABC 默认实现 |

## 五、稳定性、缓存与观测能力

| 功能项 | 当前状态 | 代码依据 | 说明 |
| --- | --- | --- | --- |
| L1 LRU 分析缓存 | 已完成 | `app/llm/analyzer.py` | 默认可用 |
| L2 Redis 分析缓存 | 需依赖环境 | `app/llm/analyzer.py` | 需 Redis 环境 |
| L3 缓存预热（P3-7） | 需依赖环境 | `app/llm/cache_prewarm.py` `app/llm/analyzer.py` | 从 L2 Redis 扫描热门 fingerprint 回填 L1；只写 L1 不刷新 L2 TTL；lifespan 启动/停机钩子；需 Redis 环境 |
| Dashboard 缓存 | 已完成 | `app/api/dashboard.py` | L1 默认可用，L2 可选 |
| Redis 状态后端 / 限流共享计数 | 需依赖环境 | `app/state/store.py` | 多实例部署需 Redis |
| LLM 熔断器 | 需依赖环境 | `app/llm/analyzer.py` | 代码已实现；需启用开关并依赖真实 LLM 服务验证 |
| PG 熔断器 | 需依赖环境 | `app/mcp/core/storage/pg_store.py` | 代码已实现；需启用开关并依赖真实 PG 故障场景验证 |
| OpenTelemetry 指标导出 | 需依赖环境 | `app/observability.py` | 代码已实现；需启用开关并连接 OTLP exporter |
| Prometheus `/metrics` 文本端点 | 已完成 | `app/observability.py` | 默认可用 |

## 六、安全与工程化能力

| 功能项 | 当前状态 | 代码依据 | 说明 |
| --- | --- | --- | --- |
| fail-closed 鉴权 | 已完成 | `app/middleware.py` `app/auth/key_rotation.py` | 支持 Bearer / X-API-Key；多 key 恒定时间比较（`hmac.compare_digest` 遍历不短路）+ 单 key 向后兼容 |
| API_KEY 多 key 轮换 | 已完成 | `app/auth/key_rotation.py` `app/config.py` | `api_keys` 逗号分隔优先，空时回退单 `api_key`；零签名变更（`AuthMiddleware` 公共签名未变） |
| RBAC 角色分级 | 已完成 | `app/auth/rbac.py` `app/middleware.py` | admin > developer > viewer 三级；未启用时全 admin（向后兼容）；`require_role(*roles)` FastAPI 依赖工厂；未命中映射默认 viewer（fail-closed） |
| 请求体大小限制 | 已完成 | `app/middleware.py` | 覆盖 Content-Length 与 chunked 流 |
| IP / 端点级限流 | 已完成 | `app/middleware.py` | memory 默认可用，Redis 可增强 |
| 安全响应头 | 已完成 | `app/middleware.py` | 默认启用 |
| LFI / SSRF / URL 白名单 | 已完成 | `app/mcp/collectors/code_locator.py` `app/mcp/verifier/ui_runner.py` | 相关限制已落地 |
| Docker Compose 本地部署 | 需依赖环境 | `docker-compose.yaml` | 需本机具备 Docker |
| GitHub Actions CI | 已完成 | `.github/workflows/*` | 仓库已存在 CI 配置 |

## 七、当前未纳入“已完成”口径的事项

以下事项不应再在任何文档中直接表述为“已全部完成”：

1. 更丰富的 MCP server->client notifications 事件类型
2. Docker 容器化复现实验（`STAB-007`，受本机 Docker daemon 状态影响）
3. AI Debug Agent Phase 2（多 Agent DAG：Git Agent + Test Agent + Security Agent 编排）；Phase 1 单 Agent `RepairAgent` + `BaseAgent` ABC 框架预留已落地（`AGENT-001`），Phase 2 待办（`AGENT-002`）
4. Browser SDK 压缩 e2e 联调（代码已完成，待 CI 验证）

> 上述事项已同步纳入 [TODO.md](./TODO.md) 与 [STABILITY_REPORT.md](./STABILITY_REPORT.md) 跟踪。
