# ai-debug-mcp TODO 与需求跟踪台账

> 本文档用于承接散落在代码注释、审计结论和历史计划中的待开发项，确保所有后续工作可追溯、可管理。
>
> 状态说明：
>
> - `已录入`：已进入正式台账，待排期
> - `进行中`：当前正在开发或验证
> - `已完成`：代码、测试、文档已闭环

## 一、本轮已正式录入的待开发项

| ID | 来源 | 事项 | 当前状态 | 同步位置 | 备注 |
| --- | --- | --- | --- | --- | --- |
| DOC-001 | 完成度审计 | 建立以代码为准的真实交付矩阵 | 已完成 | `DELIVERY_MATRIX.md` | 作为项目功能口径唯一来源 |
| DOC-002 | 完成度审计 | 统一 README / PROJECT_SUMMARY / DEV_PLAN / AI_HANDOFF 的完成度表述 | 已完成 | 本文档、`DEV_PLAN.md` | 已同步到关键对内对外文档 |
| STREAM-001 | `app/mcp/transports/sse.py` 注释 | 打通 `GET /mcp` SSE 队列与服务端消息生产者 | 已完成 | `DEV_PLAN.md` | 已接入 ready 推送与结果桥接 |
| STREAM-002 | 完成度审计 | 让 `notifications/initialized` 与 `POST Accept: text/event-stream` 具备真实推送闭环 | 已完成 | `DEV_PLAN.md` | 已补齐基础闭环 |
| STREAM-003 | 完成度审计 | 补齐 SSE 关闭与订阅清理语义 | 已完成 | `DEV_PLAN.md` | DELETE 会话已触发订阅关闭 |
| STAB-001 | 完成度审计 | 补 asyncpg 真实端到端集成验证 | 已完成 | `STABILITY_REPORT.md` | 已完成运行时 smoke test，凭据修正后链路可用 |
| STAB-002 | 完成度审计 | 补 Playwright/Chromium 真实 UI verify 集成验证 | 已完成 | `STABILITY_REPORT.md` | 已完成真实浏览器 + 本地 HTTP 页面 + DOM 断言验证 |
| STAB-003 | 完成度审计 | 补 Redis L2 缓存与共享限流集成验证 | 已完成 | `STABILITY_REPORT.md` | 已完成状态后端 smoke test、LLM L2 回填与 Dashboard L2 回读验证 |
| STAB-004 | 完成度审计 | 补 OTel exporter 启用与关闭 smoke test | 已完成 | `STABILITY_REPORT.md` | 已完成 exporter -> 本地 gRPC collector 导出验证 |
| STAB-005 | 完成度审计 | 补真实故障场景下的熔断恢复验证 | 已完成 | `STABILITY_REPORT.md` | 已完成 LLM / PG breaker 的 `open -> half-open -> close` 恢复验证 |
| STAB-006 | 本轮运行时验证 | 排查当前本机 PostgreSQL 连接异常（`psycopg2` 解码错误 / `asyncpg` 中途断开） | 已完成 | `STABILITY_REPORT.md` | 根因已确认：本地 `.env` 中 PG 密码与当前 PostgreSQL 实际密码不一致 |
| STAB-007 | 本轮运行时验证 | 启动 Docker daemon 或提供等价容器环境，以完成 Redis / OTel / PG 容器化验证 | 已录入 | `ENABLEMENT_GUIDE.md` | 当前仅有 Docker CLI，daemon 未启动 |
| SDK-003 | Browser SDK 续作 | 补网络错误自动标记静默失败（V3） | 已完成 | `DELIVERY_MATRIX.md` | `fetch`/`XHR` 失败自动转 silent failure，支持 `reportNetworkError()` |
| SDK-006 | Browser SDK 续作 | 补自动检测 UI 静默失败（V6） | 已完成 | `DELIVERY_MATRIX.md` | 点击/提交后若无 DOM、路由、网络变化，SDK 自动上报 |
| KB-001 | 智能化续作 | 建立指纹知识库基础能力并接入分析链路 | 已完成 | `DELIVERY_MATRIX.md` | 已支持命中优先返回、`knowledge_base_hit` 标记与 LLM 成功后自动沉淀 |
| DOC-003 | 文档同步 | 同步 Browser SDK V3/V6 与知识库基础能力到对内/对外文档 | 已完成 | `README.md`、`PROJECT_SUMMARY.md`、`AI_HANDOFF.md` | 本轮文档口径已按最新代码状态收口 |
| P3-6 | Phase 6 削峰队列 | 异步 LLM 分析队列（有界 asyncio.Queue + Semaphore(K) + K 常驻消费协程 + 优雅 drain） | 已完成 | `DEV_PLAN.md`、`DELIVERY_MATRIX.md` | 新增 `app/llm/analysis_queue.py` + 2 个端点 + lifespan 钩子；零侵入 analyzer.py |
| P3-7 | Phase 6 多级缓存深化 | L3 缓存预热：从 L2 Redis 扫描热门 fingerprint 回填 L1，只写 L1 不刷新 L2 TTL | 已完成 | `DEV_PLAN.md`、`ROADMAP.md` | 新增 `app/llm/cache_prewarm.py`（2026-07-26）；`analyzer.py` 新增 `_set_l1_only`；lifespan 集成启动/停机钩子；11 单测含关键回归 `test_prewarm_does_not_touch_l2_ttl` |
| VRAG-001 | Phase 7 向量检索 RAG | `VectorStore` ABC + InProcessVectorStore（Jaccard）+ QdrantVectorStore（OpenAI/智谱 Embeddings 语义召回）+ 工厂/注册表插槽；analyzer.py KB hook 区集成作为精确指纹 miss 后二级 fallback | 已完成 | `DEV_PLAN.md`、`DELIVERY_MATRIX.md` | Qdrant 适配器已完成（2026-07-26）；静默降级；uuid5 幂等 upsert |
| AUDIT-2-13 | 安全审查 P3 | RBAC 角色分级（admin/developer/viewer）+ `require_role` FastAPI 依赖工厂 | 已完成 | `claude-audit-consolidated.md` | `app/auth/rbac.py`；未启用时全 admin（向后兼容） |
| AUDIT-2-14 | 安全审查 P3 | API_KEY 多 key 轮换 + 恒定时间比较（`hmac.compare_digest` 遍历不短路） | 已完成 | `claude-audit-consolidated.md` | `app/auth/key_rotation.py`；单 key 向后兼容 |
| DOC-004 | 文档同步 | 全量 Markdown 文档同步：清理 Qdrant 留空插槽 / 待实现 / 待引入等陈旧引用 | 已完成 | `handoff.md` | 2026-07-26 完成；修正 PRD/AI_HANDOFF/DESIGN/CODE_REVIEW/ROADMAP/INTERVIEW/PROJECT_SUMMARY 7 份文档；历史修订记录与 RESUME.md 保留 |
| AGENT-001 | Phase 7 智能化续作 | AI Debug Agent Phase 1（单 Agent `RepairAgent` + 多 Agent 协同框架 `BaseAgent` ABC 预留） | 已完成 | `DEV_PLAN.md`、`ROADMAP.md`、`DELIVERY_MATRIX.md` | 2026-07-26 落地：新增 `app/agent/` 模块（7 文件）+ 2 REST 端点 + 2 MCP 工具（工具数 15→17）；6 单测文件 63 用例 + 3 集成测试 8 用例；测试基线 583 passed / 6 skipped / 0 failed；ruff 0 违规；`agent_enabled` 默认 False 向后兼容 |
| AGENT-002 | Phase 2 多 Agent DAG | AI Debug Agent Phase 2（多 Agent DAG：Git Agent + Test Agent + Security Agent 编排） | 已完成 | `DEV_PLAN.md`、`ROADMAP.md`、`DELIVERY_MATRIX.md` | 2026-07-30 落地：新增 `app/agent/git_agent.py` / `test_agent.py` / `security_agent.py` / `dag.py`（模块 7→11 文件）；`Coordinator` 扩展 DAG 调度（RepairAgent 先行 → Git/Test/Security 并行审查）；2 个新配置项 `agent_dag_parallel_timeout` / `agent_dag_failure_threshold`；新增 53 单测（当时 636 passed / 6 skipped / 0 failed；DASH-SSE-001 落地后达 654）；ruff 0 违规；`agent_multi_agent_enabled` 默认 False 向后兼容 |
| DASH-SSE-001 | Dashboard 实时 SSE 推送 | Dashboard 实时推送：`DashboardEventBus` 广播总线 + `GET /api/dashboard/stream` SSE 端点 + `invalidate_cache` 钩子广播 + 前端 EventSource（去抖 refresh + 轮询兜底 + 断线重连） | 已完成 | `DEV_PLAN.md`、`ROADMAP.md`、`DELIVERY_MATRIX.md`、`PRD.md`、`DESIGN.md` | 2026-07-30 落地：新增 `app/api/dashboard_events.py`（无 session 门槛广播总线，跨线程 `call_soon_threadsafe`，队列满丢旧保最新）；`dashboard.py` 新增 `/stream` 端点（15s 心跳 + close 终止）+ `invalidate_cache` 内挂广播钩子；`dashboard.html` EventSource 集成；`dashboard_sse_enabled=False` 默认关闭（向后兼容）；鉴权复用 `?api_key=` query 降级（EventSource 无法设自定义 header）；新增 18 单测（654 passed / 6 skipped / 0 failed）；ruff 0 违规 |
| SDK-007 | Browser SDK 续作 | Browser SDK 压缩 e2e 联调（V5 压缩传输增强验证） | 已录入 | `DEV_PLAN.md` | 代码已完成，仅验证；降级为 CI 任务，不占开发轨 |
| BETA-P0-01 | beta-release 全量审查 | Dashboard 前端鉴权缺失：所有 fetch 不携带 API Key | 已录入 | `claude-audit-consolidated.md` §十一 | 上线阻断 |
| BETA-P0-02 | beta-release 全量审查 | JWT HS256 硬编码降级密钥 | 已录入 | `claude-audit-consolidated.md` §十一 | 上线阻断 |
| BETA-P0-03 | beta-release 全量审查 | CORS 通配符 allow_origins=["*"] | 已录入 | `claude-audit-consolidated.md` §十一 | 上线阻断 |
| BETA-P0-04 | beta-release 全量审查 | TOOL_ROLE_REQUIREMENTS 缺 fallback，新工具跳过 RBAC | 已录入 | `claude-audit-consolidated.md` §十一 | 上线阻断 |
| BETA-P0-05 | beta-release 全量审查 | API Key 通过 URL 查询参数传递（SSE 端点） | 已录入 | `claude-audit-consolidated.md` §十一 | 上线阻断 |
| BETA-P0-06 | beta-release 全量审查 | save_result 文件名未校验 trace_id，路径遍历 | 已录入 | `claude-audit-consolidated.md` §十一 | 上线阻断 |
| BETA-P1-01 | beta-release 全量审查 | Dashboard SSE 队列满时 close 事件丢弃，流永不终止 | 已录入 | `claude-audit-consolidated.md` §十一 | 上线必修 |
| BETA-P1-02 | beta-release 全量审查 | JWT 无 token blacklist，无法主动失效 | 已录入 | `claude-audit-consolidated.md` §十一 | 上线必修 |
| BETA-P1-03 | beta-release 全量审查 | 多 Worker 下 JWT Key 不同步 | 已录入 | `claude-audit-consolidated.md` §十一 | 上线必修 |
| BETA-P1-04 | beta-release 全量审查 | id_token 返回到前端 URL query string | 已录入 | `claude-audit-consolidated.md` §十一 | 上线必修 |
| BETA-P1-05 | beta-release 全量审查 | 全局 ExceptionHandler 非 DEBUG 模式泄露 traceback | 已录入 | `claude-audit-consolidated.md` §十一 | 上线必修 |
| BETA-P1-06 | beta-release 全量审查 | Agent _skipped() 报 "未配置" 常量误导运维 | 已录入 | `claude-audit-consolidated.md` §十一 | 上线必修 |
| BETA-P1-07 | beta-release 全量审查 | RBAC 认证失败返回 403 而非 401 | 已录入 | `claude-audit-consolidated.md` §十一 | 上线必修 |
| BETA-P1-08 | beta-release 全量审查 | Dashboard 无独立鉴权中间件 | 已录入 | `claude-audit-consolidated.md` §十一 | 上线必修 |
| BETA-P1-09 | beta-release 全量审查 | 写操作 ingest/traces 无速率限制 | 已录入 | `claude-audit-consolidated.md` §十一 | 上线必修 |

## 二、已确认的来源与映射

| 来源位置 | 发现内容 | 已同步任务 |
| --- | --- | --- |
| `app/mcp/transports/sse.py` 文件头注释 | server->client notifications 推送尚未接入业务调用方 | `STREAM-001` `STREAM-002` |
| `tests/integration/test_api.py` | SSE 长连接用例被 skip，提示需补稳定验证 | `STREAM-003` |
| `README.md` / `PROJECT_SUMMARY.md` / `AI_HANDOFF.md` | 完成度表述与真实代码状态存在偏乐观问题 | `DOC-001` `DOC-002` |
| `ROADMAP.md` / `DEV_PLAN.md` | OTel 与下一阶段任务表述落后于真实代码 | `DOC-002` `STAB-004` |
| 完成度审计结论 | PG/asyncpg、Playwright、Redis L2、熔断、OTel 已完成真实环境启用验证；Docker 容器化验证仍待环境支持 | `STAB-001` ~ `STAB-007` |
| Browser SDK / analyzer 最新实现 | Browser SDK V3/V6 与指纹知识库能力已落地，需要同步文档口径 | `SDK-003` `SDK-006` `KB-001` `DOC-003` |

## 三、执行约束

1. 所有新增待开发项必须先录入本台账，再进入 `DEV_PLAN.md` 排期。
2. 任何文档若声明“已完成”，必须可在 `DELIVERY_MATRIX.md` 中找到对应条目。
3. 涉及真实环境验证的事项，完成后必须同时更新 `STABILITY_REPORT.md`。

## 四、beta-release 全量审查来源

| 来源位置 | 发现内容 | 已同步任务 |
| --- | --- | --- |
| `claude-audit-consolidated.md` §十一 | beta-release Phase 2 全量审查：P0×6 + P1×9 + P2×12 + 文档×5 | `BETA-P0-01`~`BETA-P1-09` + `BETA-P2-*` + `BETA-DOC-*` |
| `docs/internal/PRD.md` | 路径过期、保留期不匹配 | `BETA-DOC-01` `BETA-DOC-02` |
| `docs/internal/DESIGN.md` | Phase 2 多 Agent DAG 架构缺失 | `BETA-DOC-04` |
