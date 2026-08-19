# Lujo-MCP 项目摘要

> AI Agent 第一入口文件。任何 AI 进入项目请先读本文件，3 分钟理解项目全貌。

> 项目功能完成度、待开发项、稳定性验证状态以内部文档为准（不对外公开）。

---

## 1. 项目一句话介绍

**Lujo-MCP：面向 AI Agent 的运行时调试上下文基础设施。**

基于 MCP 协议，为 Claude / Trae / Cursor / Qoder 等 AI Agent 提供真实运行时 Bug 现场信息（Runtime Debug Context），解决"无报错但功能不对"的静默失败检测、"多 Agent 协同调试"以及历史结论复用（Debug Experience RAG）三个核心问题。

**核心能力链路**：

```
Runtime 数据采集
    ↓
Debug Context 构建
    ↓
Debug Experience RAG 检索
    ↓
Agent 分析增强
    ↓
Verifier 验证
```

---

## 2. 当前架构

```
客户端层（MCP客户端/REST/浏览器）
    ↓
传输层（stdio / Streamable HTTP）
    ↓
中间件层（CORS → Trace → SecurityHeaders → RateLimit → MaxBodySize → Auth → NetworkCapture）
    ↓
路由/分发层（/api/debug/* + require_role RBAC门控 │ /mcp + TOOL_ROLE_REQUIREMENTS 工具级门控 │ /health /metrics）
    ↓
调试引擎（logs/context builder/stacktrace/code_locator/runtime/llm/exception_hook）
    ↓
存储/状态层（trace_store[memory/pg] │ session │ state[memory/redis] │ sse hub │ specs）
```

**架构分层意义**：每层只干一件事，能单独测、换存储后端只动一层、构建逻辑不污染路由。

---

## 3. 核心模块位置

| 模块 | 关键文件 | 职责 |
|------|----------|------|
| 入口 | [app/main.py](../../app/main.py) | FastAPI 实例、路由注册、lifespan |
| 配置 | [app/config.py](../../app/config.py) | pydantic-settings 全局单例 |
| 中间件 | [app/middleware.py](../../app/middleware.py) | 7 个中间件（CORS、Auth、MaxBodySize、RateLimit、SecurityHeaders、Trace + NetworkCapture，fail-closed 鉴权） |
| 调试 API | [app/api/debug.py](../../app/api/debug.py) | /api/debug/* 路由 |
| Dashboard API | [app/api/dashboard.py](../../app/api/dashboard.py) | 从 PostgreSQL 读取 |
| MCP HTTP | [app/api/mcp_routes.py](../../app/api/mcp_routes.py) | Streamable HTTP 传输 |
| MCP stdio | [app/mcp_server.py](../../app/mcp_server.py) | stdio 子进程传输 |
| 日志核心 | [app/runtime/core/logs.py](../../app/runtime/core/logs.py) | add_log/get_logs/list_request_ids |
| 存储工厂 | [app/runtime/core/storage/factory.py](../../app/runtime/core/storage/factory.py) | memory/pg 一键切换 |
| PG 存储 | [app/runtime/core/storage/pg_store.py](../../app/runtime/core/storage/pg_store.py) | 连接池+自动建表（修改需审批） |
| 上下文构建 | [app/runtime/context/builder.py](../../app/runtime/context/builder.py) | build_debug_context |
| 故障定位 | [app/runtime/context/fault_localizer.py](../../app/runtime/context/fault_localizer.py) | 栈帧启发式评分，生成 `likely_cause_candidate`（候选，非根因） |
| 断言引擎 | [app/runtime/verifier/assert_engine.py](../../app/runtime/verifier/assert_engine.py) | assert_behavior 纯函数 |
| 规范存储 | [app/runtime/verifier/spec_store.py](../../app/runtime/verifier/spec_store.py) | dict+Lock + add_log 持久化 |
| 异常钩子 | [app/runtime/hooks/exception_hook.py](../../app/runtime/hooks/exception_hook.py) | sys.excepthook + asyncio |
| LLM 分析 | [app/llm/analyzer.py](../../app/llm/analyzer.py) | 重试/超时/fallback/流式 |
| 指纹知识库 | [app/rag/knowledge_base.py](../../app/rag/knowledge_base.py) | 按错误指纹复用历史分析结论（精确匹配 + 自动沉淀）；v0.5.3 起 PostgreSQL 写穿持久化（kb_entries 表 + 启动回灌，learned 经验跨重启保留） |
| 知识库存储后端 | [app/runtime/core/storage/factory.py](../../app/runtime/core/storage/factory.py) + [base.py](../../app/runtime/core/storage/base.py) | `get_knowledge_store()` 分发（PG 真实持久化 / NoOp 降级，PG 不可用自动降级纯内存） |
| Debug 经验记录 | [app/rag/experience.py](../../app/rag/experience.py) | `DebugExperienceRecord` 输出 DTO/View（纯 View，不建存储、不替代 DebugCase） |
| 经验检索器 | [app/rag/retriever.py](../../app/rag/retriever.py) | 三层检索：fingerprint 精确 → message normalize → vector（默认关闭） |
| 向量检索抽象 | [app/rag/vector_store.py](../../app/rag/vector_store.py) | `VectorStore` ABC + `InProcessVectorStore`（Jaccard）+ `NullVectorStore` + 工厂/注册表 |
| Qdrant 语义召回 | [app/rag/qdrant_vector_store.py](../../app/rag/qdrant_vector_store.py) | `QdrantVectorStore` Embeddings 语义检索 + uuid5 幂等 upsert |
| 工具注册 | [app/mcp/tools/__init__.py](../../app/mcp/tools/__init__.py) | register_all_tools（18 个工具，含 `repair_async`/`repair_result`/`resolve_stack`） |
| AI Debug Agent | [app/agent/](../../app/agent/) | 自动修复方案生成 + 多 Agent DAG 协同（Phase 1 单 Agent + Phase 2 多 Agent DAG，共 11 文件） |
| 浏览器 SDK | [browser-sdk/ai-debug.js](../../browser-sdk/ai-debug.js) | UMD/CJS/ESM 三格式 |
| npm 分发 | [npm/packages/lujo-mcp](../../npm/packages/lujo-mcp) + [packaging/lujo-mcp-server.spec](../../packaging/lujo-mcp-server.spec) | PyInstaller 单文件打包 + npm 元包 + 3 平台二进制包（win32-x64/linux-x64/osx-arm64），`npm install -g @lujoai/lujo-mcp` 开箱即用 |

---

## 4. 已完成功能

### 核心能力 ✅

- ✅ 请求追踪（trace 完整链路）
- ✅ 调试上下文构建（结构化 AI 可消费数据）
- ✅ 异常堆栈捕获（sync + asyncio）
- ✅ 运行时快照（psutil）
- ✅ LLM 智能分析（openai/zhipu/deepseek/custom）
- ✅ 指纹知识库命中与自动沉淀（命中优先返回 + LLM 成功后自动写入）
- ✅ 向量检索 RAG（Phase 7）：`VectorStore` ABC + `InProcessVectorStore`（Jaccard 零依赖）+ `QdrantVectorStore`（OpenAI/智谱 Embeddings 语义召回，uuid5 幂等 upsert）；精确指纹 miss 后向量召回 fallback；全链路降级容错
- ✅ 规范驱动验证（assert_behavior 纯函数）
- ✅ 静默失败检测（< 1ms 判定）
- ✅ 全局异常自动捕获（exception_hook）

### 存储能力 ✅

- ✅ PostgreSQL 16 集成
- ✅ PGStore 连接池（minconn=2, maxconn=20）
- ✅ **asyncpg 异步存储**（feature flag 灰度切换，`PG_ASYNC_ENABLED`）
- ✅ 自动建表（traces、sessions、errors、specs、network_records、ui_events）
- ✅ 存储工厂模式（memory/pg 一键切换）
- ✅ Dashboard 从 PostgreSQL 读取
- ✅ **errors 表持久化聚合**（指纹去重 + 统计）
- ✅ **spec_store 独立表**（CRUD + 审计追溯）
- ✅ **P3-1 数据分区**（traces 表按月 RANGE 分区，自动预创建 + 惰性检查，默认关闭）
- ✅ **P3-2 归档策略**（>N 天数据自动归档到 traces_archive，cleanup_expired 先归档再删除，默认关闭）
- ✅ **P3-3 批量写入**（save_entries + add_logs_batch，trace_repo META+LINK 批量）
- ✅ **P3-5 优雅降级**（PG 不可用时自动降级到 memory，默认开启）
- ✅ **P3-8 熔断器**（pybreaker，LLM/PG 调用熔断保护）

### 传输能力 ✅

- ✅ Streamable HTTP 传输（`/mcp` 端点）
- ✅ stdio 传输（Claude Desktop 子进程）
- ✅ SSE 广播中心与会话化长连接
- ⚠️ MCP HTTP notifications 已具备基础推送闭环，丰富通知类型仍待补齐
- ✅ **Dashboard 实时 SSE 推送**（`DASH-SSE-001`，2026-07-30）：`DashboardEventBus` 广播总线 + `GET /api/dashboard/stream` SSE 端点 + `invalidate_cache` 广播钩子 + 前端 EventSource（去抖 refresh + 10s 轮询兜底 + 断线重连）；`dashboard_sse_enabled=False` 默认关闭
- ✅ MCP 工具双传输注册（HTTP / stdio 均由统一注册表动态导出，当前各 18 个，含 `repair_async`/`repair_result`/`resolve_stack`）
- ✅ **npm 开箱即用分发**（2026-08-09）：PyInstaller 单文件打包（`packaging/lujo-mcp-server.spec`）+ npm 元包 + 3 平台二进制包（win32-x64/linux-x64/osx-arm64），`npm install -g @lujoai/lujo-mcp` 即可使用；GitHub Actions 矩阵构建自动发布（`.github/workflows/release-npm.yml`）

### 稳定性与观测能力 ✅

- ✅ **L1/L2/L3 多级缓存**：L1 进程 LRU + L2 Redis 分布式 + L3 缓存预热（从 Redis 回填热门 fingerprint 到 L1，只写 L1 不刷新 L2 TTL）
- ✅ **异步分析削峰队列**（P3-6）：有界 `asyncio.Queue(maxsize=N)` + K 常驻消费协程 + `asyncio.Semaphore(K)` 对齐 LLM RPM/TPM；队列满返回 429；优雅停机 drain
- ✅ **LLM/PG 熔断器**（P3-8）：pybreaker 包装 LLM 和 PG 调用，熔断时返回结构化 fallback；`open → half-open → close` 恢复链路已验证
- ✅ **OpenTelemetry 集成**（P3-4）：双模式 OTel SDK + Prometheus `/metrics` 文本端点向后兼容；OTLP gRPC 导出；惰性初始化 + 失败降级
- ✅ **PG 不可用自动降级 memory**（P3-5）：`storage_fallback_to_memory` 控制，默认开启

### 安全能力 ✅

- ✅ fail-closed 鉴权（hmac.compare_digest）
- ✅ 请求体大小限制（防 DoS）
- ✅ IP 限流（Redis 计数）
- ✅ 安全响应头
- ✅ 入库前脱敏（复合键名子串匹配 + 白名单）
- ✅ /metrics 独立鉴权 toggle
- ✅ CORS 可配置来源
- ✅ RBAC 角色分级（AUDIT-2-13）：admin > developer > viewer 三级；`require_role(*roles)` FastAPI 依赖工厂覆盖 **33 条 REST 路由**（debug 14 + ingest 7 + dashboard 7 + spec 5）及 18 个 MCP 工具（`TOOL_ROLE_REQUIREMENTS` 字典门控，v0.5.1 新增 `resolve_stack` 只读三级）；未命中映射默认 viewer（fail-closed）
- ✅ **API_KEY 多 key 恒定时间比较轮换**（AUDIT-2-14）：`verify_api_key` 遍历所有 key 不短路 + `hmac.compare_digest` 防时序侧信道 + 单 key 向后兼容
- ✅ **LFI/SSRF 防护**：路径白名单 + SSRF URL 白名单（IP/Localhost/Metadata 端点拒绝）

### 前端能力 ✅

- ✅ 浏览器 SDK（UMD/CJS/ESM）
- ✅ **SDK V2 批量上报 + sendBeacon 兜底**
- ✅ **SDK V3 网络错误自动标记静默失败**
- ✅ **SDK V4 trace_id 初始化与请求关联**
- ✅ **SDK V5 分类型批量 ingest 接入**
- ✅ **SDK V6 UI 静默失败自动检测**
- ✅ Console 自动采集（console.error/warn 自动上报 + trace_id 关联 + 脱敏）
- ✅ Playwright 自动遍历（auto_test）
- ✅ Web 控制台 Dashboard

### AI Debug Agent ✅（Phase 1，2026-07-26 + Phase 2，2026-07-30）

- ✅ **自动修复方案生成**：从 analyzer 的"给建议"升级为"生成可执行修复方案"（`{patch, affected_files, validation_strategy, risk_assessment, confidence, rationale}`）
- ✅ **多 Agent 协同框架**：`BaseAgent` ABC + `Coordinator` 编排器，Phase 1 仅 `RepairAgent`，Phase 2 GitAgent/TestAgent/SecurityAgent 继承 `BaseAgent` 接入 DAG
- ✅ **零侵入主链路**：新增 `app/agent/` 目录（7→11 文件），复用 `analyzer._get_async_client` / `knowledge_base.retrieve_similar` / `git.get_recent_diff` / `analyzer.analyze_async`，不改 analyzer.py 公共签名
- ✅ **异步削峰队列**：`RepairQueue` 结构对称 `AnalysisQueue`，独立 workers 配额避免抢 LLM RPM
- ✅ **静默降级**：三层兜底（agent 内 / coordinator / queue），任何失败不穿透主链路
- ✅ **feature flag 控制**：`agent_enabled=False` 默认关闭，零行为变更
- ✅ 新增 2 REST 端点 + 2 MCP 工具（`repair_async` + `repair_result`，工具数 15→17）
- ✅ **Phase 2 多 Agent DAG（`AGENT-002`，2026-07-30）**：`RepairAgent`（先行，产出 `repair_plan`）→ `GitAgent` / `TestAgent` / `SecurityAgent`（并行审查，依赖 `repair_plan`）
  - `git_agent.py`：`GitAgent` 纯 git 归因（复用 `get_recent_diff` + 优先复用 `repair_context.git_context`，不调 LLM）；输出 `suspect_commits` / `recent_changes` / `attribution`；无堆栈帧返回 SKIPPED
  - `test_agent.py`：`TestAgent` 基于修复方案生成验证策略（`test_files` / `test_cases` / `regression_risks` / `validation_steps` / `coverage_note`）；复用 `analyzer._get_async_client`，独立重试/fallback；`repair_plan` 缺失返回 SKIPPED
  - `security_agent.py`：`SecurityAgent` 对修复方案做安全审查（10 类风险：LFI/SSRF/SQLi/CmdInjection/PathTraversal/AuthBypass/InfoLeak/Deserialization/HardcodedSecret/Other）；`_validate_security_review` 容错 JSON + severity/category 合法化
  - `dag.py`：多 Agent DAG 拓扑定义 —— `PHASE2_FIRST_NODES=["repair"]`（先行）+ `PHASE2_PARALLEL_NODES=["git","test","security"]`（并行）；`build_phase2_agents()` 构造节点注册表
  - `coordinator.py`：新增 `_run_dag()` + `_run_parallel_agents()`；`agent_multi_agent_enabled=False` 走 Phase 1 单 Agent 串行（向后兼容）；并行节点失败静默降级 + `dag_degraded` 信号（失败数 ≥ `agent_dag_failure_threshold`）
  - 新增 2 配置项：`agent_dag_parallel_timeout=0`（0=继承 `agent_timeout`）/ `agent_dag_failure_threshold=2`
  - 新增 53 单测（`test_git_agent.py` 13 / `test_test_agent.py` 13 / `test_security_agent.py` 15 / `test_dag_coordinator.py` 9 + `test_agent_config.py` 增 2）

### 工程化 ✅

- ✅ Docker Compose 一键启动
- ✅ scripts/ 目录（run_tests.sh / lint.sh / init_db.sh）
- ✅ migrations/ 目录（6 个 SQL 文件）
- ✅ GitHub Actions CI
- ✅ 测试基线：以 `pytest` 实际执行结果为准；当前 **1134 passed / 6 skipped / 0 failed**（含 AI Debug Agent Phase 1 新增 63 项 + Phase 2 新增 53 项 + Dashboard SSE 18 项 + Quality System 86 项 + Verify Loop 38 项 + M3 Fault Localization 2.0 新增 48 项 + Dashboard 质量报告 6 项 + P1 Debug Experience RAG 26 项 + CODE_REVIEW_FIX_PROMPT 回归测试 + stacktrace 工具与存储工厂边界 17 项 + D5 MCP 可观测性 16 项 + D6 Benchmark 框架 19 项 + v0.5.0 DebugContext Schema/Runtime Integration 与 Tool Category Metadata 45 项 + v0.5.1 Source Map 解析 94 项 + deepseek base_url 1 项 + 第 3 轮代码审查 P1/P2/P3 收口 24 项 + v0.5.3 KB 持久化 13 项 + P3-9 重连回归 5 项）

### v0.3.0 Release Audit 收口 ✅

- ✅ C3/C4 trace_repo 键统一 + PG 持久化回读链路
- ✅ H4/H5/H12 进程边界 + 脱敏复核 + 测试卫生
- ✅ H10 SDK 静默失败事件链补齐（network/UI 环形缓冲 + observed_events 入库）
- ✅ M1 storage factory 拼写错误 fail-fast
- ✅ M4 JSON-RPC 错误码规范化（-32700/-32600/-32601/-32602/-32603）
- ✅ M12 依赖拆分（requirements.txt / requirements-dev.txt）
- ✅ N2 LLM 输出 schema 校验 + 结构化 fallback
- ✅ N3 stdio 生命周期资源回收（PG 连接池 / excepthook 卸载 / atexit+signal 兜底）
- ✅ N4 内部错误串全仓复核（已收口 17 类，漏网 3 处登记 follow-up）
- ✅ M9 .env 未知键 fail-fast（ConfigDict extra="ignore" + model_post_init warning）
- ✅ schemas 重复定义统一（删除死代码 debug.py + 重命名冲突类）
- ✅ spec_store 持久化可靠性（list_specs 恢复逻辑补齐）

---

## 5. 当前开发阶段

**当前阶段**：核心能力已成型；"真实完成度收口 + MCP HTTP 流式闭环 + 稳定性落地验证"已完成；Browser SDK V3-V6 + 指纹知识库 + 向量检索版 RAG（in-process + Qdrant 语义召回）+ AI Debug Agent Phase 1（单 Agent `RepairAgent`）+ Phase 2（多 Agent DAG：`GitAgent` + `TestAgent` + `SecurityAgent` 编排）+ **P1 Debug Experience RAG（D1-D4：DebugExperienceRecord + 三层检索 Retriever + Context Assembler 解耦集成，`debug_experience_enabled` 默认 False）** 均已落地；**v0.5.0 已发布（2026-08-13）**：工程质量加固 + Runtime 数据契约对齐（DebugContext 7→20 字段、MCP Tool Category Metadata、Prompt Injection Guard、API Schema Validation、Session 安全加固）；**v0.5.1 已发布（2026-08-15）**：Source Map 解析（纯 Python VLQ 解码 + 上传/磁盘双获取通道 + `resolve_stack` MCP 工具（18/18）+ QualityScorer/Benchmark A/B 实证，`sourcemap_enabled` 默认关闭；Browser SDK column 保留 + release 透传）；**v0.5.2 已发布（2026-08-15）**：品牌统一（ai-debug-mcp → lujo-mcp）。第 3 轮代码审查 P1/P2/P3 收口（2026-08-16 基线 1105/6/0；P3-9 pg_store 重连修复 2026-08-17 后全部清零）。**v0.5.3 已发布（2026-08-18）**：RAG 知识库 PostgreSQL 持久化（kb_entries 表 + 写穿落库 + 启动回灌，learned 经验跨重启保留）+ 数据库改名 `lujo_mcp` + P3-9 pg_store 重连缺陷修复。测试基线 1134/6/0。

**已完成**：
- Phase 0：项目标准化 ✅
- Phase 1：PostgreSQL 集成 ✅
- Phase 1 规范驱动验证 ✅（V1-V5 全部完成）
- v0.3.0 Release Audit 收口 ✅（P0 7/7 ✅，P1 8/8 ✅，2026-07-19）
- Phase 2：PG 异步存储（asyncpg）+ errors 表持久化聚合 ✅
- Phase 3：LLM 异步调用（AsyncOpenAI）+ 多级缓存 ✅
- Phase 4：Browser SDK V2 批量上报 + /ingest/batch ✅
- Browser SDK V3 / V6：网络错误自动标记 + UI 静默失败自动检测 ✅
- 指纹知识库基础能力：命中优先 + 自动沉淀 ✅
- Phase 5：安全加固（SEC-04/07/08/12/LFI/SSRF/auth hardening）✅
- Phase 5-6 数据层优化：P3-1 分区 / P3-2 归档 / P3-3 批量写入 / P3-4 OpenTelemetry / P3-5 优雅降级 / P3-6 异步分析队列 / P3-7 L3 缓存预热 / P3-8 熔断器 ✅
- Phase 7 智能化：智能错误分析引擎 + 向量检索 RAG（in-process + Qdrant 语义召回 + uuid5 幂等 upsert）✅
- AUDIT-2-13/14：RBAC 角色分级 + API_KEY 多 key 轮换 ✅
- AI Debug Agent Phase 1：单 Agent `RepairAgent` + `BaseAgent` ABC 多 Agent 协同框架预留 ✅（2026-07-26）
- AI Debug Agent Phase 2：多 Agent DAG（`GitAgent` + `TestAgent` + `SecurityAgent` 编排，`AGENT-002`）✅（2026-07-30）
- Dashboard 实时 SSE 推送（`DASH-SSE-001`）：`DashboardEventBus` 广播总线 + `GET /api/dashboard/stream` SSE 端点 + `invalidate_cache` 广播钩子 + 前端 EventSource（去抖 refresh + 轮询兜底 + 断线重连）✅（2026-07-30）
- **P1 Debug Experience RAG（2026-08-07）**：
  - D1 Debug Experience 数据链路：`app/rag/experience.py` `DebugExperienceRecord`（纯 View DTO，不建存储、不替代 DebugCase）✅
  - D2 RAG Retriever 能力：`app/rag/retriever.py` 三层检索（fingerprint 精确 → message normalize → vector，失败降级 `[]`）✅
  - D3 Context Assembler 解耦：`_safe_debug_experience_recall()` 集成 + `debug_experience_enabled=False` 默认关闭，关闭零调用零耗时 ✅
  - D4 全量验证：874 passed / 6 skipped / 0 failed；架构隔离 PASS ✅

**v0.4 开发阶段**：

已完成：
- Runtime Context 数据采集 ✅
- MCP 调试工具链 ✅
- Debug Experience Knowledge Base ✅
- RAG Retriever ✅
- Agent Context Assembly 解耦 ✅

暂未实现：
- 自动代码修复（Repair Loop 闭环）
- Patch 生成
- 多 Agent 协作（独立自动修复链路）
- 自动 Repair Loop

**测试提示**：全仓测试基线请以仓库内最新 `pytest` 实际执行结果为准；当前 **1134 passed / 6 skipped / 0 failed**（含 AI Debug Agent Phase 1 新增 63 项 + Phase 2 新增 53 项 + Dashboard SSE 18 项 + Quality System 86 项 + Verify Loop 38 项 + M3 Fault Localization 2.0 新增 48 项 + Dashboard 质量报告 6 项 + P1 Debug Experience RAG 新增 26 项 + CODE_REVIEW_FIX_PROMPT 回归测试 + stacktrace 工具与存储工厂边界 17 项 + D5 MCP 可观测性 16 项 + D6 Benchmark 框架 19 项 + v0.5.0 DebugContext Schema/Runtime Integration 与 Tool Category Metadata 45 项 + v0.5.1 Source Map 解析 94 项 + deepseek base_url 1 项 + 第 3 轮代码审查 P1/P2/P3 收口 24 项 + v0.5.3 KB 持久化 13 项 + P3-9 重连回归 5 项）。

**当前优先级**：

| 优先级 | 任务 | 目标 |
|--------|------|------|
| ~~**P1**~~ ✅ | ~~v0.5.1/v0.5.2 迭代：Source Map 解析 + Browser SDK 增强 + 品牌统一~~ | ✅ 已完成（2026-08-15：Source Map 解析 + resolve_stack 18/18 + deepseek 修复 → npm 0.5.1；品牌统一 ai-debug-mcp → lujo-mcp → npm 0.5.2） |
| ~~**P1**~~ ✅ | ~~v0.5.3 迭代：RAG 知识库 PostgreSQL 持久化 + 数据库改名 + P3-9 重连修复~~ | ✅ 已完成（2026-08-18：kb_entries 表写穿 + 启动回灌 + lujo_mcp + P3-9 → npm 0.5.3） |
| **P1** | 后续版本迭代（Source Map 后续增强 / Browser SDK 压缩 e2e 落地） | 待规划（详见变更记录） |
| **P2** | Browser SDK 压缩 e2e 联调（`SDK-007`） | CI 交错任务，代码已完成仅验证，不占开发轨 |
| ~~P3~~ ✅ | ~~Docker 容器化复现实验（`STAB-007`）~~ | ✅ 已完成（postgres/redis/app 三容器健康，/health、/api/debug/run、连接池均已验证） |
| ~~P4~~ ✅ | ~~SSE 实时 Dashboard~~ | ✅ 已完成（2026-07-30，`DASH-SSE-001`：`DashboardEventBus` 广播总线 + `GET /api/dashboard/stream` SSE 端点 + `invalidate_cache` 广播钩子 + 前端 EventSource；`dashboard_sse_enabled` 默认 False） |

**v0.3.0 收口成果**：
- 测试基线：340 passed / 6 skipped / 0 failed（单元 310 passed + 6 skipped，脱敏集成 18，AsyncPGStore 12）
- P0 全部清零：C3/C4 键统一+PG回读、H4/H5 复核、H10 SDK事件链、H12 进程边界、M9 .env fail-fast、schemas 统一、spec_store 持久化
- P1 全部清零：N2 LLM输出校验、N3 stdio资源回收、M1 storage factory、M4 JSON-RPC错误码、M12 依赖拆分
- Phase 2-5 新增：asyncpg 异步存储、AsyncOpenAI、多级缓存、errors 聚合、spec_store 独立表、SDK V2 批量上报、GitHub Actions CI

---

## 6. 禁止修改模块

### 绝对禁止修改

| 模块 | 文件 | 原因 |
|------|------|------|
| PGStore | [app/runtime/core/storage/pg_store.py](../../app/runtime/core/storage/pg_store.py) | 已验证，如需修改须先输出问题分析+影响范围+测试方案 |
| 存储抽象层 | [app/runtime/core/storage/base.py](../../app/runtime/core/storage/base.py) | 工厂模式基础 |
| 存储工厂 | [app/runtime/core/storage/factory.py](../../app/runtime/core/storage/factory.py) | 一行切换核心 |
| 安全中间件 | [app/middleware.py](../../app/middleware.py) | fail-closed 安全栈 |
| 全局异常处理 | [app/error_handlers.py](../../app/error_handlers.py) | 异常兜底 |
| 可观测性 | [app/observability.py](../../app/observability.py) | 监控指标 |

### 禁止事项

| 禁止 | 说明 |
|------|------|
| ❌ 绕过 Storage 访问数据库 | 必须通过 factory 获取 store |
| ❌ 新建数据库连接 | 必须使用连接池 |
| ❌ 引入 SQLAlchemy | 当前不允许 |
| ❌ 引入 Alembic | 当前不允许 |
| ❌ 大规模重构 | 除非明确要求，否则小步修改 |

---

## 7. AI 阅读顺序

任何 AI 进入项目，请按以下顺序阅读：

| 序号 | 文件 | 作用 |
|------|------|------|
| 1 | PROJECT_SUMMARY.md | 快速理解项目（本文件）|
| 2 | DESIGN.md | 理解技术设计 |
| 3 | PRD.md | 理解产品需求 |

> 开发规则、当前状态、开发计划、长期路线图等内部文档不对外公开，仅供团队与 AI Agent 在本地文件系统中查阅。

---

## 8. 关键设计决策

1. **工厂模式**：存储层（memory/PG）、状态层（memory/Redis）、LLM provider（openai/zhipu/deepseek/custom）都用工厂模式，一行配置切换
2. **规范驱动**：用期望规范作为 ground truth，`assert_behavior()` 纯函数自动比对，偏离即告警，支持 api/ui/rule 三种 kind
3. **双传输**：HTTP 与 stdio 均复用 `register_all_tools()` + `_tool_registry`，避免工具面漂移和漏注册
4. **宿主 AI 推理模式**：服务只交付结构化原始数据，推理交给 Trae/Codex/Claude
5. **安全优先**：fail-closed 鉴权、Content-Length 硬检查、IP 限流、安全响应头、入库前脱敏
6. **幂等性**：异常钩子 `install_global_hook()` 幂等安装，PG 建表 `CREATE TABLE IF NOT EXISTS`
7. **降级策略**：各采集器失败降级不阻断整体，中间件异常降级放行
8. **实时推送**：Dashboard 使用 SSE（Server-Sent Events）而非 WebSocket——Dashboard 只需服务端→客户端单向推送，SSE 原生支持自动重连、更轻量；`DashboardEventBus` 采用进程内广播总线（`asyncio.Queue` + `call_soon_threadsafe`），队列满时丢弃旧消息保最新

---

## 9. 配置速查

**关键环境变量**：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `STORAGE_BACKEND` | `memory` | `memory` / `postgresql` |
| `STATE_BACKEND` | `memory` | `memory` / `redis` |
| `PG_HOST` | `localhost` | PostgreSQL 主机 |
| `PG_PORT` | `5432` | PostgreSQL 端口 |
| `PG_DATABASE` | `lujo_mcp` | 数据库名 |
| `PG_USER` | `postgres` | PostgreSQL 用户名 |
| `PG_PASSWORD` | — | PostgreSQL 权威密码来源 |
| `LLM_PROVIDER` | `openai` | `openai` / `zhipu` / `custom` |
| `OPENAI_API_KEY` | — | LLM API Key |
| `API_KEY` | — | 鉴权密钥（留空不启用，向后兼容单 key 模式） |
| `API_KEYS` | — | 多 key 模式（逗号分隔，如 `key1,key2,key3`），启用 key rotation |
| `API_KEY_ROTATION_ENABLED` | `false` | 启用多 key 恒定时间比较轮换（默认 false，单 key 模式） |
| `RBAC_ENABLED` | `false` | 启用 RBAC 角色分级（默认 false，全 admin 向后兼容） |
| `RBAC_ROLE_MAPPING` | — | key→role 映射（如 `key1:admin,key2:developer,key3:viewer`），未命中映射默认 viewer（fail-closed） |

> 环境固化约定：应用只读取 `PG_*` 变量；`POSTGRES_PASSWORD` 仅供 Docker 初始化数据库；`DATABASE_URL` 仅作兼容项，应用本身不会读取。

**启动命令**：

```bash
# Docker Compose（推荐）
docker compose up -d

# 本地开发（确保 PostgreSQL 已运行）
python -m app.main

# stdio 模式（供 Claude Desktop 等本地客户端）
python -m app.mcp_server
```

---

## 10. 安全审查结论（2026-07-23，AI 阅读须知）

> AI 进入本项目做任何安全相关判断前，请先读本节与 [DESIGN.md](./DESIGN.md) §13。安全审查详情见内部安全审查文档。

**整体健康度：8.5 / 10**（工程质量 8.5 / 安全性 8.0 / 架构可维护性 8.5 / 文档可信度 9.0）。核心数据流架构**合理、无需重写**；安全基线扎实。此结论为 2026-07-23 安全审查快照——其中提及的长期项 C7（source-map）已于 v0.5.1（2026-08-15）完成落地。

**P0（部署前必修）—— ✅ 四项已于 2026-07-22 修复**（下表为原始风险与证据；行为变更见文末）：

| 项 | 一句话 | 证据 |
|----|--------|------|
| SEC-01 LFI | `ingest` 任意 `frames[].file` → dashboard trace 详情 `linecache` 回显文件 | `ingest.py:73`→`code_locator.py:85` |
| SEC-02 SSRF | `verify_ui`/`auto_test` 的 `url` 直连 Playwright `page.goto`，无白名单 | `ui_runner.py:82` |
| SEC-03 免鉴权 | `API_KEY` 默认空即免鉴权；启动防护仅 `__main__`+`0.0.0.0` | `config.py:74`、`main.py:233` |
| SEC-05 无超时 | 工具调用无 `wait_for`，可数分钟阻塞 | `server.py:87` |

**须订正的既有认知**：① “入库前脱敏”对 `exception_hook→errors` 的自动捕获路径不成立（message/traceback 未脱敏，SEC-06）；② “会话隔离”仅 stdio 成立，共享 HTTP 无 `session_id` 维度（SEC-04）；③ 路径白名单默认放行；④ 中间件真实顺序为 `Trace` 最外、`CORS` 内于 `Auth`（非文档所述）。

**已复核为安全**：SQL 全参数化（无注入）、LLM 发送前递归脱敏、assert_engine 纯函数无 `eval`、PG 连接池双检锁正确。**无支付/资金逻辑**；唯一间接财务风险是 LLM 调用无配额（费用失控）。

> **新增安全能力（2026-07-25，AUDIT-2-13/14）**：RBAC 三级角色分级（admin > developer > viewer）覆盖 33 条 REST 路由 + 18 个 MCP 工具；`require_role(*roles)` FastAPI 依赖工厂路由级门控 + `TOOL_ROLE_REQUIREMENTS` MCP 工具级门控；未命中映射默认 viewer（fail-closed）。多 key 恒定时间比较（`verify_api_key` 遍历不短路 + `hmac.compare_digest`）+ 单 key 向后兼容。LFI 路径白名单 + SSRF URL 白名单已上线。

> 整改追踪见内部审计报告。修任一项须回填状态 + `文件:行` 验证。

**P0 修复后的行为变更（AI 须知）**：① 0.0.0.0+空 `API_KEY` 现会拒绝启动（本地免鉴权用 `HOST=127.0.0.1`）；② 代码/Git 定位默认仅限进程 CWD，读 CWD 外源码需配 `WHITELIST_PATH_PREFIX`/`GIT_PATH_WHITELIST`；③ `verify_ui`/`auto_test` 默认拒私网/元数据/`file://`，本地联调设 `UI_URL_ALLOW_PRIVATE=true`；④ 工具调用受 `TOOL_TIMEOUT_SECONDS`（默认 60s）约束。P1（SEC-04/06/07/08/09）与 P2（SEC-13/M7）已修复。
