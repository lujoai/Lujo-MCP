# 任务交接快照（handoff.md）

> 本文件为最近一轮任务完成状态的快速快照，详细交接信息见 [docs/internal/AI_HANDOFF.md](./docs/internal/AI_HANDOFF.md)。
> 最近更新：2026-07-30

## 当前项目状态快照

| 指标 | 状态 |
|------|------|
| 测试基线 | 654 passed / 6 skipped / 0 failed |
| MCP 工具数 | HTTP 17 / stdio 17 |
| 版本 | v0.3.0（已打 annotated tag，未推送远端） |
| P0/P1 阻塞项 | 0（全部清零） |
| Phase 0-8 | 全部完成 ✅ |
| 剩余待办 | SDK-007（Browser SDK 压缩 e2e，CI 任务）、STAB-007（Docker 容器化验证，待环境） |
| 技术债务 | pg_store.py 拆分评估已完成（待审批）、N4-FU-3（pg_store 启动期错误外泄，待评估） |

---

## 本轮完成：Dashboard 实时 SSE 推送开发与文档同步（2026-07-30）

### 任务背景

Dashboard 实时 SSE 推送（`DASH-SSE-001`）已在代码库落地：新增 `app/api/dashboard_events.py`（`DashboardEventBus` 广播总线）+ `dashboard.py` 新增 `GET /api/dashboard/stream` SSE 端点与 `invalidate_cache` 广播钩子 + `dashboard.html` 前端 EventSource 集成 + `dashboard_sse_enabled=False` 默认关闭（测试基线 636→654 passed）。但 TODO/ROADMAP/DEV_PLAN/DELIVERY_MATRIX/PRD/DESIGN/AI_HANDOFF/handoff/README/PROJECT_SUMMARY 10 份文档中 SSE Dashboard 仍标注为"待开发/下一步"，与代码实际状态不符，需同步更新。

### 落地清单

| 文档 | 修正内容 |
|------|----------|
| [TODO.md](file:///c:/Users/ASUS/Dev-Projects-ai-debug-mcp/docs/internal/TODO.md) | 新增 `DASH-SSE-001` 已完成条目（实现清单 + 配置 + 测试基线 654） |
| [DELIVERY_MATRIX.md](file:///c:/Users/ASUS/Dev-Projects-ai-debug-mcp/docs/internal/DELIVERY_MATRIX.md) | §五 稳定性/缓存/观测能力表新增「Dashboard 实时 SSE 推送」行 |
| [PRD.md](file:///c:/Users/ASUS/Dev-Projects-ai-debug-mcp/docs/internal/PRD.md) | 版本 v5.4→v5.5 + 修订记录新增 v5.5 + §3.1 能力表新增 SSE 行 + 架构师结论新增实时运维场景 + §7.4 FR 表新增 FR20 + FR20 详细需求小节 |
| [DESIGN.md](file:///c:/Users/ASUS/Dev-Projects-ai-debug-mcp/docs/internal/DESIGN.md) | 顶部新增 SSE 设计更新说明 + §3.5 存储层表新增 `dashboard event bus` 行 + 新增 §18 Dashboard 实时 SSE 推送完整设计章节（目标/模块/总线/端点/钩子/前端/降级/测试） |
| [ROADMAP.md](file:///c:/Users/ASUS/Dev-Projects-ai-debug-mcp/docs/internal/ROADMAP.md) | 「最近更新」改 SSE 落地 + 新增 Phase 8 实时观测增强 ✅ + 待开发表移除 SSE 行 |
| [DEV_PLAN.md](file:///c:/Users/ASUS/Dev-Projects-ai-debug-mcp/docs/internal/DEV_PLAN.md) | 当前进度加 SSE 已落地 + P2 SSE 行标记已完成 + 下一步重点移除 SSE |
| [AI_HANDOFF.md](file:///c:/Users/ASUS/Dev-Projects-ai-debug-mcp/docs/internal/AI_HANDOFF.md) | 当前阶段/当前 Sprint 加 SSE + 最近完成事项新增 SSE 完成档 |
| [README.md](file:///c:/Users/ASUS/Dev-Projects-ai-debug-mcp/README.md) | 项目状态表测试基线 636→654、当前阶段移除 SSE 待开发、下一步移除 SSE |
| [PROJECT_SUMMARY.md](file:///c:/Users/ASUS/Dev-Projects-ai-debug-mcp/PROJECT_SUMMARY.md) | 测试基线 636→654、当前优先级表 SSE 行标记已完成 |

### 验证结果

- 全量扫描 "SSE 实时 Dashboard" 在活动文档"待开发/下一步"语境中 0 残留（已完成语境保留）
- 测试基线一致性：654 passed 在 TODO/ROADMAP/DEV_PLAN/DELIVERY_MATRIX/PRD/DESIGN/AI_HANDOFF/handoff/README/PROJECT_SUMMARY 全部对齐
- SSE 状态一致性：上述 10 份文档全部标记 `DASH-SSE-001` 已落地（2026-07-30）
- 代码-文档一致性：`dashboard_events.py` / `dashboard.py` / `dashboard.html` / `config.py` 实现与 DESIGN §18 / PRD FR20 描述对齐
- `dashboard_sse_enabled=False` 默认关闭在所有文档对齐（向后兼容零开销）

---

## 上一轮完成：AI Debug Agent Phase 2 多 Agent DAG 文档同步（2026-07-30）

### 任务背景

AI Debug Agent Phase 2（多 Agent DAG：`GitAgent` + `TestAgent` + `SecurityAgent` 编排，`AGENT-002`）已在代码库落地（`app/agent/` 7→11 文件，`Coordinator` 扩展 DAG 调度，测试基线 583→636 passed）。前序会话已同步 `docs/internal/` 下的 TODO/ROADMAP/DEV_PLAN/DELIVERY_MATRIX/PRD/DESIGN/AI_HANDOFF 7 份内部文档，但根目录面向人类与面试的入口文档（README / PROJECT_SUMMARY / INTERVIEW）仍残留"Phase 2 为后续待办""583 passed""7 文件"等陈旧引用，与代码实际状态不符，需同步更新。

### 落地清单

| 文档 | 修正内容 |
|------|----------|
| [README.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/README.md) | 核心功能 AI Debug Agent 条目从"Phase 1"扩展为"Phase 1 + Phase 2"，新增 DAG 拓扑/三 Agent 职责/`dag_degraded` 信号描述；真实交付状态摘要"部分完成能力"行新增 Phase 2 已落地；项目状态表测试基线 583→636、当前阶段改为"Phase 2 已落地、下一步 Browser SDK/Docker/SSE"；项目结构 `app/agent/` 注释改为 11 文件 |
| [PROJECT_SUMMARY.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/PROJECT_SUMMARY.md) | §3 核心模块表 AI Debug Agent 行改为"Phase 1 单 Agent + Phase 2 多 Agent DAG，共 11 文件"；§4 AI Debug Agent 小节标题加 Phase 2，新增 7 条 Phase 2 详条（git/test/security/dag/coordinator + 2 配置项 + 53 单测）；§4 工程化测试基线 583→636；§5 当前阶段、已完成清单、测试提示、当前优先级表（P1 改为 Browser SDK）均同步 Phase 2 已落地 |
| [INTERVIEW.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/INTERVIEW.md) | 头部测试覆盖 583→636、路线图追加 Phase 2 ✅；§二 STAR Result 测试数 583→636 + 新增 Phase 2 成果条目（DAG 拓扑/三 Agent/降级/53 单测/零侵入）；§二 路线图表 Phase 7 改为 ✅ 已完成 + 新增"后续"增量能力行；§四 Q&A "583 个测试"→"636 个测试" + 补 Phase 2 DAG 测试维度；§五 30 秒陈述 583→636；新增 §三 Q6「多 Agent DAG 设计 / 为什么不用 LangGraph」完整面试叙事（五部分 + 一句话标准答）|

### 无需更新（已在前序会话对齐）

- `docs/internal/DESIGN.md`：§17.9 Phase 2 多 Agent DAG 实现已完整记录（DAG 拓扑图/模块结构/配置/降级矩阵/测试）
- `docs/internal/PRD.md`：FR19 已扩展覆盖 Phase 2 模块结构/配置项
- `docs/internal/DELIVERY_MATRIX.md`：已新增 AI Debug Agent Phase 2 行
- `docs/internal/TODO.md`：`AGENT-002` 已标记已完成
- `docs/internal/ROADMAP.md`：Phase 7 已含 Phase 2 完成项
- `docs/internal/DEV_PLAN.md`：当前状态/下一步重点/P4 行已更新
- `docs/internal/AI_HANDOFF.md`：最近完成事项已新增 Phase 2 完成档
- `RESUME.md`：按项目硬约束不修改
- `SECURITY_REVIEW.md`：已归档，重定向到 claude-audit-consolidated.md

### 验证结果

- 全量扫描 "Phase 2 多 Agent DAG 为后续待办""Phase 2 多 Agent DAG 待启动""583 passed""583 个测试""583 个单元测试" 在活动文档中 0 残留（历史快照保留）
- 测试基线一致性：636 passed 在 README/PROJECT_SUMMARY/INTERVIEW/handoff/AI_HANDOFF/TODO/ROADMAP/DEV_PLAN/DELIVERY_MATRIX 全部对齐
- Phase 2 状态一致性：README/PROJECT_SUMMARY/INTERVIEW/handoff/AI_HANDOFF/TODO/ROADMAP/DEV_PLAN/DELIVERY_MATRIX/PRD/DESIGN 全部标记 Phase 2 已落地（2026-07-30，`AGENT-002`）
- MCP 工具数一致性：HTTP 17 / stdio 17 在所有文档对齐（Phase 2 复用 `repair_async`/`repair_result`，工具数不变）

---

## 上一轮完成：RBAC 文档同步（2026-07-28）

### 任务背景

RBAC 三级角色分级（admin/developer/viewer）+ 多 key 恒定时间比较轮换已在代码库中落地（AUDIT-2-13/14），覆盖 33 条 REST 路由 + 17 个 MCP 工具。但 INTERVIEW.md、PROJECT_SUMMARY.md、CODE_REVIEW.md 等文档中仍残留"无 RBAC""无 API_KEY 轮换""无操作级权限控制"等过期描述，面试叙事与 AI 入口文档未体现最新安全能力，需同步更新。

### 落地清单

| 文档 | 修正内容 |
|------|----------|
| [CODE_REVIEW.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/docs/internal/CODE_REVIEW.md) | §5 评估总览 ② 权限控制：已完成 6→10，待改进 4→1，状态 🟡→🟢；§② 已完成表新增 4 项（RBAC 角色分级、多 key 恒定时间比较轮换、操作级权限控制、MCP tools/call 工具级门控）；§② 待改进表删除前 3 项（已落地），仅保留 `/debug/echo` `/debug/token`；§P3 长期清单 13/14 标记 ✅ 已完成；§评估结论"弱项"删除"缺乏 RBAC"，新增调试端点处置项 |
| [INTERVIEW.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/INTERVIEW.md) | 头部版本行追加 RBAC 摘要；§0.5 架构取舍表安全行更新为 fail-closed + 多 key + RBAC 三级；§二 STAR 成果段追加纵深防御描述 + 新增 RBAC 成果条目；§Q4 中间件顺序修正为真实顺序 + 追加 RBAC 路由级门控注记 + Q4 取舍表安全行同步更新；新增 RBAC 追问预案完整小节（角色分级/路由门控/工具门控/安全兜底 + 一句话标准答）|
| [PROJECT_SUMMARY.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/PROJECT_SUMMARY.md) | §2 架构示意中间件顺序修正 + 路由层追加 RBAC/TOOL_ROLE_REQUIREMENTS 注记；§4 安全能力新增 RBAC 角色分级 + 多 key 轮换 + LFI/SSRF 防护 3 条；§9 配置速查新增 API_KEYS/API_KEY_ROTATION_ENABLED/RBAC_ENABLED/RBAC_ROLE_MAPPING 4 项；§10 安全审查结论追加新增安全能力引用块 |

### 无需更新（已在前序会话对齐）

- `docs/internal/DESIGN.md`：§9.1 RBAC 权限矩阵 + §16.4 Track C 已完整记录
- `docs/internal/DELIVERY_MATRIX.md`：§六 RBAC 角色分级已标注 33 条 REST 路由
- `docs/internal/PRD.md`：§10.1 REST API 表已标注"最低权限要求"列
- `docs/internal/DEV_PLAN.md`：AUDIT-2-13/14 已标记完成
- `docs/internal/ROADMAP.md`、`TODO.md`、`AI_HANDOFF.md`：RBAC 条目已完成
- `README.md`：真实交付状态摘要已包含 RBAC + 33 条 REST 路由
- `RESUME.md`：按项目硬约束不修改
- `SECURITY_REVIEW.md`：已归档，重定向到 claude-audit-consolidated.md

### 验证结果

- 全量扫描 "无 RBAC 角色分级"、"无 API_KEY 轮换机制"、"无操作级权限控制" 在活动文档中 0 残留（历史快照保留）
- 文档版本一致性：RBAC 状态在 CODE_REVIEW/INTERVIEW/PROJECT_SUMMARY/DESIGN/DELIVERY_MATRIX/PRD/DEV_PLAN/AI_HANDOFF 全部对齐

---

## 上一轮完成：根目录 Markdown 文档同步（2026-07-26）

### 任务背景

AI Debug Agent Phase 1 落地后（测试基线 520→583、MCP 工具 15→17），根目录面向人类/AI 的入口文档（PROJECT_SUMMARY.md / INTERVIEW.md）仍残留 v0.3.0 时期的"15 工具 / 41 passed / 下一步 AI Debug Agent"等陈旧引用，与代码库实际状态不符，需同步更新。

### 落地清单

| 文档 | 修正内容 |
|------|----------|
| [PROJECT_SUMMARY.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/PROJECT_SUMMARY.md) | §3 核心模块表新增 `app/agent/` 行 + 工具数 15→17；§4 新增「AI Debug Agent ✅（Phase 1）」小节（7 条能力点）；§4 工程化测试基线 41→583 passed；§5 当前阶段重写（含 Phase 5-6/7/AUDIT-2-13/14/Agent Phase 1 已完成清单 + 当前优先级 P1=Agent Phase 2、P2=Docker、P3=SDK e2e）；测试提示 41→583 passed |
| [INTERVIEW.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/INTERVIEW.md) | 头部路线图：`→ AI Debug Agent` → `→ AI Debug Agent Phase 1 ✅ → Phase 2 多 Agent DAG（待启动）`；版本行 MCP 工具 15→17 + FR19 标注；测试覆盖行新增 583 passed；STAR Result 340→583 + 15→17 + 新增 Qdrant/Agent 模块；路线图表 AI Debug Agent 待启动 → Phase 1 已完成；Q&A "340 个测试" → "583 个测试" + 新增 Qdrant/Agent 测试维度；30 秒标准陈述 340→583 |
| [RESUME.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/RESUME.md) | **按项目硬约束不修改**（仅分析：L15/L18 工具数 15→17、L29 41→583、L44 路线图 Agent 已完成 Phase 1，待用户授权后更新） |

### 保留项（历史快照，不修改）

- **PROJECT_SUMMARY.md §5 v0.3.0 收口成果"测试基线：340 passed"**：v0.3.0 时期历史快照，保留以说明当时交付状态
- **PROJECT_SUMMARY.md §10 安全审查结论（2026-07-23）**：历史安全自审快照，保留原样
- **INTERVIEW.md §0.x 问题 1-4 实战 bug 故事**：面试叙事视角，保留原样
- **RESUME.md**：按项目硬约束不修改

### 验证结果

- 全量 rg 扫描根目录 .md 文件中 "15 个工具|15 个 MCP|340 个测试|41 passed|下一步 AI Debug Agent|AI Debug Agent 待启动" 在"当前状态"语境下 0 残留（历史快照语境保留）
- 文档版本一致性：MCP 工具数 17、测试基线 583 passed 在 PROJECT_SUMMARY/INTERVIEW/handoff/AI_HANDOFF/DELIVERY_MATRIX/README/claude-audit-consolidated 全部对齐

---

## 上一轮完成：失实内容修正与文档对齐（2026-07-26）

### 任务背景

`claude-audit-consolidated.md` 等 markdown 文档中多处证据引用与代码库实际状态不符（虚构函数名、版本号陈旧、行号偏差、路径缺失前缀），需逐一核实并修正，确保文档可信度。

### 核实与修正清单

| # | 类型 | 原失实描述 | 修正后（基于代码库实际） | 涉及文档 |
|---|------|------------|--------------------------|----------|
| 1 | 严重失实 | `browser-sdk/package.json version="0.3.0"` | `version="0.5.0"`（SDK 已迭代至 V5+：V3/V4/V5/V6） | claude-audit-consolidated.md L120 |
| 2 | 严重失实 | `store.py:86-101` Lua 脚本 `_atomic_incr_with_expire` 原子化 INCR+EXPIRE | 函数名不存在；实际为 `app/state/store.py:87-112` `_SLIDING_WINDOW_LUA`（ZSET 滑动窗口，**非 INCR+EXPIRE**）；fail-closed 在 `:128-130` | claude-audit-consolidated.md L60 |
| 3 | 行号偏差 | `config.py:82` `debug_endpoints_enabled` | `app/config.py:110`（偏差 +28） | claude-audit-consolidated.md L97 |
| 4 | 行号偏差 | `config.py:87` `cors_origins` | `app/config.py:105`（偏差 +18） | claude-audit-consolidated.md L114 |
| 5 | 行号+内容 | `middleware.py:22` `PUBLIC_PATHS=("/", "/health")` | `app/middleware.py:20`，5 项：`("/", "/health", "/demo", "/demo/silent-failure", "/ai-debug.js")` | claude-audit-consolidated.md L62 |
| 6 | 证据补充 | SEC-04 `ingest.py 传入 session_id`（无路径无行号） | `app/api/ingest.py` L62/L80/L134/L171 | claude-audit-consolidated.md L56 |
| 7 | 证据补充 | SEC-06 `exception_hook.py:29-34 调用 redact()` | 文件在 `app/mcp/hooks/`；L29-35 仅定义 `_redact_exception_data` 辅助函数（文件内未被调用）；实际 redact() 调用在 L49-50/L63-64 | claude-audit-consolidated.md L58 |
| 8 | 证据补充 | Phase 7 `Qdrant 语义召回 + uuid5 幂等 upsert`（无行号） | `app/rag/qdrant_vector_store.py:254-257`（uuid5 确定性 point id）+ `L271-275`（upsert wait=True） | claude-audit-consolidated.md L181 |
| 9 | 行号+内容（扩展发现） | `PUBLIC_PATHS=(/,/health,/metrics)`（误称 /metrics 免鉴权，与 SEC-08 矛盾） | 同 #5 实际内容；`/metrics` 需鉴权 | DESIGN.md L183、CODE_REVIEW.md L783 |
| 10 | 行号偏差（扩展发现） | `middleware.py:22-23` PUBLIC_PATHS | `middleware.py:20` | DESIGN.md L742 |

### 核实方法

逐项读取源文件确认：`browser-sdk/package.json`、`app/state/store.py`、`app/config.py`、`app/middleware.py`、`app/api/ingest.py`、`app/mcp/hooks/exception_hook.py`、`app/rag/qdrant_vector_store.py`，记录精确行号与函数名，严禁杜撰。

### 保留项（不修改）

- **SECURITY_REVIEW.md**：已归档（L3 明确"已归档...本文件不再更新"，重定向至 claude-audit-consolidated.md），按项目硬约束保持归档不动；其中 `config.py:82`（实为 `:130` `redaction_enabled`）、`exception_hook.py:38`（实为 `app/mcp/hooks/exception_hook.py:38` `install_global_hook`）等历史证据属归档快照，权威修正已在 consolidated 文档体现

### 验证结果

- 全量 rg 扫描 `_atomic_incr_with_expire|version="0.3.0"|store.py:86-101|exception_hook.py:29-34|PUBLIC_PATHS=(/,/health,/metrics)|config.py:82|config.py:87|middleware.py:22` 在活动 .md 文件中 0 匹配
- 唯一残留位于已归档 SECURITY_REVIEW.md（按硬约束不动）
- 更新文件：`docs/internal/release/claude-audit-consolidated.md`（9 处）、`docs/internal/DESIGN.md`（2 处）、`docs/internal/CODE_REVIEW.md`（1 处）、`handoff.md`（本节）

---

## 上一轮完成：AI Debug Agent Phase 1 MVP（2026-07-26）

### 设计要点

- **自动修复方案生成**：从 analyzer 的"给建议"（`{root_cause, impact, fix, confidence}`）升级为"生成可执行修复方案"（`{patch, affected_files, validation_strategy, risk_assessment, confidence, rationale}`）
- **零侵入主链路**：新增 `app/agent/` 目录，复用 `analyzer._get_async_client` / `knowledge_base.retrieve_similar` / `git.get_recent_diff` / `analyzer.analyze_async`，不改 analyzer.py 公共签名
- **多 Agent 协同框架**：`BaseAgent` ABC + `Coordinator` 编排器，Phase 1 仅 `RepairAgent` 实现，Phase 2 GitAgent/TestAgent/SecurityAgent 继承 `BaseAgent` 即可接入
- **异步削峰队列**：`RepairQueue` 结构对称 `AnalysisQueue`，独立 workers 配额避免抢 LLM RPM
- **静默降级**：三层兜底（agent 内 / coordinator / queue），任何失败不穿透主链路（与 Qdrant 适配器一致）
- **feature flag 控制**：`agent_enabled=False` 默认关闭，零行为变更

### 落地清单

- 新增 [app/agent/](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/agent/) 目录（7 文件）：
  - [base.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/agent/base.py)：`BaseAgent` ABC + `AgentContext`/`AgentResult`/`AgentTrace` + `AgentStatus` 枚举
  - [schemas.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/agent/schemas.py)：Pydantic 模型（`RepairRequest`/`RepairPlan`/`RepairJob`/`Sources`）
  - [context_assembler.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/agent/context_assembler.py)：`RepairContextAssembler`（并发聚合 analyze_async + retrieve_similar + get_recent_diff，各失败静默降级）
  - [repair_agent.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/agent/repair_agent.py)：`RepairAgent`（复用 `analyzer._get_async_client`，独立重试/fallback + `_validate_repair_plan` 容错 JSON）
  - [repair_queue.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/agent/repair_queue.py)：`RepairQueue` + lifespan helper（结构对称 `analysis_queue.py`）
  - [coordinator.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/agent/coordinator.py)：`Coordinator` 编排器（装配上下文 → 调度 Agent → 收集 trace）
  - [__init__.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/agent/__init__.py)：模块导出
- [config.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/config.py) 新增 9 个 `agent_*` 配置项（`agent_enabled` 默认 False）
- [debug.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/api/debug.py) 新增 2 个 REST 端点：`POST /api/debug/repair/async` + `GET /api/debug/repair/result/{job_id}`
- [repair_api.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/mcp/tools/repair_api.py) 新增 2 个 MCP 工具：`repair_async` + `repair_result`（工具数 15→17）
- [main.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/main.py) lifespan：启动期 `start_repair_queue()`，停机期 `drain_repair_queue(timeout)`
- 测试：6 单元测试文件（63 用例）+ 3 集成测试文件（8 用例，e2e skip-if-no-api-key）

### 关键降级矩阵

| 失败点 | 降级行为 |
|---|---|
| 向量召回 / git / analyze_async 失败 | 对应字段为空/None，继续 |
| RepairAgent LLM 失败 | `repair_plan=None` + `agent_trace[0].status=FAILED` |
| 队列满 | API 返回 429 + `{"error":"queue_full","queue_size":N}` |
| `agent_enabled=False` | 端点返回 501，MCP 工具返回 `{"error":"agent disabled"}` |

---

## 上一轮完成：全量 Markdown 文档同步（2026-07-26）

### 任务背景

Qdrant 向量检索适配器与 P3-7 L3 缓存预热已落地，但全量扫描发现多个 .md 文档仍残留"Qdrant 留空插槽 / 待实现 / 待引入 / 部分完成"等陈旧引用，与代码实际状态不符。

### 修正清单

| 文档 | 修正内容 |
|------|----------|
| [PRD.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/docs/internal/PRD.md) | 文档版本 v5.2→v5.3；最后更新日期 → 2026-07-26；架构师结论版本 → v5.3 并新增 Qdrant 语义召回 + L3 预热条目；§3.1 能力表 Qdrant 插槽 → 已实现；§7.4 FR17 表格 Qdrant 留插槽 → 已实现；§11 配置表 `VECTOR_STORE_BACKEND` 双后端说明、`QDRANT_URL` 等 🔲 待实现 → ✅ 已实现 |
| [AI_HANDOFF.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/docs/internal/AI_HANDOFF.md) | §三 P3 行 L3 预热待 rebase → ✅ 已完成；P4 行 Qdrant 适配器实现 → ✅ 已完成；§一 Track B 插槽措辞优化；§一 新增 DOC-004 文档同步完成条目 |
| [DESIGN.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/docs/internal/DESIGN.md) | §16.7 当前限制 Track B "Qdrant 后端未实现" → 已通过 QdrantVectorStore 提供语义召回；未来扩展 Track B 标记 ✅ 已完成；§16.3.6 设计权衡"禁止静默回退"措辞修正；§3.4.6 多级缓存补充 L3 预热；§14.2 缺失缓存项标记已落地；§14.6.3 多级缓存架构更新当前实现；头部新增 Phase 6-7 更新说明 |
| [CODE_REVIEW.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/docs/internal/CODE_REVIEW.md) | 向量数据库技术栈表三行 🔲 待引入 → ✅ 已引入；远期 Phase 4-6 清单向量检索 RAG 标记 ✅ 已完成；附录数据库技术栈表 Phase 4 Qdrant 🔲 待引入 → ✅ 已实现；§Phase 3 表 P3-6/P3-7 状态更新为 ✅ 已完成（含 L3 预热） |
| [ROADMAP.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/docs/internal/ROADMAP.md) | P3-7 多级缓存深化 部分完成 → ✅ 已完成；最近更新日期 → 2026-07-26 |
| [TODO.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/docs/internal/TODO.md) | 新增 P3-7（已完成）、DOC-004（已完成）、AGENT-001（已录入）、SDK-007（已录入）4 个台账条目 |
| [DEV_PLAN.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/docs/internal/DEV_PLAN.md) | 最近更新日期 → 2026-07-26；当前进度补充 L3 预热 + Qdrant；§Phase 3 表 P3-7 补充 L3 预热描述与 cache_prewarm.py 依据 |
| [DELIVERY_MATRIX.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/docs/internal/DELIVERY_MATRIX.md) | §五 稳定性缓存表新增 L3 缓存预热（P3-7）行 |
| [STABILITY_REPORT.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/docs/internal/STABILITY_REPORT.md) | §五 后续验证任务表 STAB-002~005 标记 ✅ 已完成；新增状态列 |
| [claude-audit-consolidated.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/docs/internal/release/claude-audit-consolidated.md) | §九 当前状态测试基线 485 → 520 passed（含 Qdrant 23 项 + L3 预热 12 项） |
| [INTERVIEW.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/INTERVIEW.md) | 路线图向量检索版 RAG 标记 ✅ 已完成；§二 Phase 7 待启动 → 部分完成（向量检索 ✅，AI Debug Agent 待启动） |
| [PROJECT_SUMMARY.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/PROJECT_SUMMARY.md) | P3 智能化续作描述更新（向量检索版 RAG 已完成） |

### 保留项（历史快照，不修改）

- **PRD.md §1 修订记录 v5.1/v5.2 条目**：历史修订记录，描述当时交付状态，保留原样
- **RESUME.md**：按用户硬约束不修改
- **handoff.md / AI_HANDOFF.md 中"上一轮完成"节**：历史快照，描述当时对比基线（如 497/485/381 passed），保留
- **描述改造前后的叙述性内容**（如"qdrant 分支从 raise NotImplementedError 改为..."）：保留以说明改造过程

### 验证结果

- 全量扫描 `Qdrant 待实现|Qdrant 后端未实现|Qdrant 留插槽|🔲.*Qdrant` 等关键词：仅在历史修订记录与 RESUME.md 中残留（均属保留项）
- 全量扫描 `L3 预热待|P3-7.*待|P3-7.*部分完成`：0 匹配
- L3 缓存预热记录覆盖 11 个 md 文件（README/handoff/TODO/ROADMAP/claude-audit-consolidated/PRD/DEV_PLAN/DESIGN/DELIVERY_MATRIX/CODE_REVIEW/AI_HANDOFF）
- 520 passed 测试基线覆盖 6 个 md 文件（README/handoff/PRD/DESIGN/AI_HANDOFF/claude-audit-consolidated）
- 未完成任务项（AGENT-001 / SDK-007 / STAB-007）在 TODO.md / DEV_PLAN.md / ROADMAP.md / handoff.md / DELIVERY_MATRIX.md 均有对应记录
- 文档版本号一致性：PRD.md v5.3，其余文档通过日期与状态字段同步

---

## 上一轮完成：Qdrant 向量检索适配器（2026-07-26）

### 设计要点

- **真正语义召回**：接入 OpenAI/智谱 Embeddings API，克服 `InProcessVectorStore` Jaccard 相似度无法理解语义相似的局限（如 "database timeout" 与 "DB connection refused" 召回为 0）。是 AI Debug Agent 的前置依赖。
- **零概念 leak**：Qdrant 的 collection/point/vector_id 全部封装在 `QdrantVectorStore` 内部，对外仍只有 `add(docs)/search(query, top_k)` 检索语义。
- **静默降级**：Qdrant 不可用 / embedding 失败 / 维度不匹配 → `add`=no-op + warning，`search` 返回 `[]`，绝不抛异常穿透到 LLM 主链路（与 Redis L2 / vector_store 既有 fail-safe 模式一致）。
- **embedding client 独立**：不复用 `analyzer._get_client`——解耦 + 错误语义不同（analyzer 缺 Key 直接 raise，本模块须静默降级）。
- **维度不匹配不自动重建**：避免丢数据，改为 warning + 降级 + 日志给恢复指引。

### 落地清单

- 新增 [qdrant_vector_store.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/rag/qdrant_vector_store.py)：
  - `QdrantVectorStore(VectorStore)`：`add` 用 `uuid5(fingerprint)` 做确定性 point id 幂等 upsert；`search` 用 Qdrant 原生 `score_threshold` 过滤
  - `_get_qdrant_client()`：惰性初始化 + 双重检查锁 + collection 自动创建 + 维度校验（参照 `analyzer._get_redis_cache` 模式）
  - `_get_embedding_client()`：独立 OpenAI 客户端，模块内复制 `_PROVIDER_BASE_URLS` 避免循环 import
  - `_embed_texts()`：按 2048/批分块调用 embeddings API + 维度校验
- [vector_store.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/rag/vector_store.py) 工厂改造：qdrant 分支从 `raise NotImplementedError` 改为函数内 `import QdrantVectorStore` 实例化（破循环 + 可选依赖隔离）
- [config.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/config.py) 新增：`qdrant_embedding_model`、`qdrant_embedding_dim`、`qdrant_connect_timeout`、`qdrant_request_timeout`
- [requirements.txt](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/requirements.txt) 追加 `qdrant-client>=1.9.0`
- 测试：[test_qdrant_vector_store.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/tests/unit/test_qdrant_vector_store.py)（23 用例）+ [test_qdrant_integration.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/tests/integration/test_qdrant_integration.py)（4 集成测试，skip-if-unavailable）+ 改造 `test_vector_store.py::test_qdrant_backend_returns_qdrant_store`

### 关键降级矩阵

| 失败点 | add 行为 | search 行为 |
|---|---|---|
| qdrant-client 未装 / 连接失败 / 维度不匹配 | no-op + warning | 返回 `[]` |
| OpenAI client 初始化失败（无 API Key） | no-op + warning | 返回 `[]` |
| embedding API 失败 / 维度校验失败 | no-op + warning | 返回 `[]` |
| upsert / search 网络失败 | no-op + warning | 返回 `[]` |

---

## 上一轮完成：P3-7 L3 缓存预热（2026-07-26）

### 设计要点

- **只写 L1 不写 L2**：预热场景下 L2 已有数据，若调 `_set_cache_result` 会 `setex` 刷新 L2 TTL，导致定时预热周期下 L2 永不自然淘汰，违反 TTL 淘汰语义。新增 [analyzer.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/llm/analyzer.py) `_set_l1_only` 让 L2 TTL 自然流逝，该过期的过期，下次 SCAN 时自然不在结果集里。
- **LRU 逻辑一致性**：`_set_l1_only` 与 `_set_cache_result` 的 L1 段完全一致（容量满且新键 `popitem(last=False)`，已存在键 `move_to_end`），共享 `_cache_lock` 避免并发竞态。
- **fail-safe 降级**：Redis 不可用、SCAN 错误、反序列化失败均静默降级，不阻塞服务启动。

### 落地清单

- 新增 [cache_prewarm.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/llm/cache_prewarm.py)：
  - `prewarm_cache(top_n)`：SCAN 取 top_n 个 L2 key → MGET 批量读 → `_set_l1_only` 回填 L1；返回 `{"scanned", "prewarmed", "skipped"}` 统计
  - `prewarm_once_with_timeout(top_n, timeout=10)`：`asyncio.wait_for` 包装同步函数，防启动期 Redis 慢查阻塞 lifespan
  - `start_prewarm_task()` / `stop_prewarm_task()`：`asyncio.create_task` 周期性预热（`interval_seconds=0` 时仅启动一次）；`stop` 用 `cancel + await` 抑制 `CancelledError`
- [analyzer.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/llm/analyzer.py) 新增 `_set_l1_only(fingerprint, result)`，零改动既有缓存区
- [config.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/config.py) 新增：`llm_cache_prewarm_enabled=False`、`llm_cache_prewarm_top_n=20`、`llm_cache_prewarm_interval_seconds=3600`
- [main.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/main.py) lifespan：启动期 `prewarm_once_with_timeout` + `start_prewarm_task`；停机期 `stop_prewarm_task`
- 测试：[test_cache_prewarm.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/tests/unit/test_cache_prewarm.py)（11 用例，含关键回归 `test_prewarm_does_not_touch_l2_ttl` 断言 `setex` 调用次数为 0）

### 关键回归测试

```python
def test_prewarm_does_not_touch_l2_ttl():
    """prewarm 后断言 Redis 客户端的 setex 调用次数为 0。
    若误用 _set_cache_result 会刷新 L2 TTL，导致定时预热周期下 L2 永不淘汰。
    """
    ...
    mock_redis.setex.assert_not_called()
```

---

## 上一轮完成：三轨并行开发（2026-07-25）

### Track A — P3-6 异步分析队列（消息队列削峰）

- 新增 [analysis_queue.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/llm/analysis_queue.py)：`AnalysisQueue` 类
  - 有界 `asyncio.Queue(maxsize=N)`（N=峰容量，满则背压）
  - K 常驻消费协程 + `asyncio.Semaphore(K)` 对齐 LLM RPM/TPM
  - 优雅停机 `drain(timeout)` 取消 worker + `queue.join(timeout)` 排空
  - `QueueFullError` 端点返回 `429 + queue_size`
- 新增端点：`POST /api/debug/analyze/async`、`GET /api/debug/analyze/result/{job_id}`（[debug.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/api/debug.py)）
- [main.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/main.py) lifespan：启动期 `start_analysis_queue()`，停机期 `drain_analysis_queue(timeout)`
- 配置项：`llm_async_analysis_enabled`、`llm_queue_maxsize=100`、`llm_queue_workers=4`、`llm_queue_drain_timeout=30`
- 测试：[test_analysis_queue.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/tests/unit/test_analysis_queue.py)
- 隔离：零侵入 analyzer.py（消费协程延迟导入 `analyze_async`）

### Track B — 向量检索 RAG（Phase 7）

- 新增 [vector_store.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/rag/vector_store.py)：
  - `VectorStore` ABC，纯检索语义 `add(docs)` / `search(query, top_k) -> [(doc, score)]`
  - `InProcessVectorStore`（Jaccard 相似度，零依赖）
  - `NullVectorStore`（feature 关闭时 no-op）
  - 工厂 `get_vector_store()` 单例 + `register_vector_backend(name, cls)` 注册表插槽
  - Qdrant 后端本轮留空插槽（配置 backend=qdrant 显式 `raise NotImplementedError`）→ **已于 2026-07-26 实现**，见本轮完成节
- [analyzer.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/llm/analyzer.py) KB hook 区集成：精确指纹 miss 后做向量召回 fallback；返回结果新增 `knowledge_base_hit` / `analysis_source` 字段
- 配置项：`vector_store_enabled=False`、`vector_store_backend="in_process"`、`vector_store_top_k=3`、`vector_store_min_score=0.3`、`qdrant_url`、`qdrant_collection`、`qdrant_api_key`
- 测试：[test_vector_store.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/tests/unit/test_vector_store.py)

### Track C — RBAC + API_KEY 轮换（AUDIT-2-13/14）

- 新增 [key_rotation.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/auth/key_rotation.py)：
  - 多 key 恒定时间比较：`verify_api_key` 遍历所有 key 不短路 + `hmac.compare_digest` 防时序侧信道
  - 单 key 向后兼容：`api_keys` 为空时回退 `settings.api_key`
  - `auth_enabled()` 判定是否启用鉴权
- 新增 [rbac.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/auth/rbac.py)：
  - 角色分级 admin > developer > viewer
  - `rbac_enabled=False` 时全 admin（向后兼容）
  - 未命中映射默认 viewer（fail-closed）
  - `require_role(*allowed_roles)` FastAPI 依赖工厂
- [middleware.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/middleware.py) `AuthMiddleware` 仅体内修改：
  - 公共签名未变（`__init__(self, app)` + `dispatch(self, request, call_next)`）
  - `__init__` 调 `auth_enabled()`，`dispatch` 调 `verify_api_key()` + 注入 `request.state.role`
- 配置项：`api_keys`、`api_key_rotation_enabled`、`rbac_enabled`、`rbac_role_mapping`
- 测试：[test_key_rotation.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/tests/unit/test_key_rotation.py)、[test_rbac.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/tests/unit/test_rbac.py)
- 零签名变更：`setup_middleware(app)` 签名未变，[ingest.py](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/app/api/ingest.py) 完全无鉴权改动

---

## 验证结果

- **单元测试**：`583 passed / 6 skipped / 0 failed / 0 errors`（相比上一轮 520 增加 63 项 AI Debug Agent 测试）
- **集成测试**：8 passed（disabled + queue_full，e2e skip-if-no-api-key）
- **Ruff**：`app/agent/` + `repair_api.py` + 9 个测试文件 0 违反
- **AI Debug Agent 隔离性**：`app/agent/` 物理隔离，零侵入 analyzer.py；复用 `_get_async_client` / `retrieve_similar` / `get_recent_diff` / `analyze_async`
- **feature flag 隔离**：`agent_enabled=False` 默认关闭，零行为变更；端点返回 501，MCP 工具返回 error
- **三轨物理隔离**（更早一轮）：
  - Track A 在 `app/llm/analysis_queue.py`
  - Track B 在 `app/rag/vector_store.py`
  - Track C 在 `app/auth/`

---

## 后续待办（按优先级排序）

1. **Browser SDK 压缩 e2e 联调** — 降级为穿插/CI 任务：代码已完成，仅验证，不占开发轨，挂 CI 跑一次即可。
2. **Docker 容器化复现实验**（`STAB-007`）— 用户已明确放后面，待本机 Docker daemon 启动。

> ~~3. **SSE 实时 Dashboard**~~ — ✅ 已完成（2026-07-30，`DASH-SSE-001`，见本轮完成节）。

---

## 合并纪律回顾

- 三轨互不踩 `analyzer.py` 同一区域：A=LLM 调用区（不改 analyzer）、B=知识库挂钩区（仅 KB hook 区域）、C=不碰 analyzer
- 各轨合并前 rebase 主干，避免合并地狱
- 共享改动点（`config.py` / `main.py` lifespan）集中提交，避免合并冲突

---

## 文档同步清单（本轮已更新）

| 文档 | 更新内容 |
|------|----------|
| [DEV_PLAN.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/docs/internal/DEV_PLAN.md) | P3-6 / AUDIT-2-13 / AUDIT-2-14 标记完成；P4 行更新；下一步重点更新 |
| [AI_HANDOFF.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/docs/internal/AI_HANDOFF.md) | §一 最近完成事项新增三轨条目；§三 当前开发方向表更新 |
| [ROADMAP.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/docs/internal/ROADMAP.md) | 新增 Phase 6 已完成节；Phase 7 标记向量检索 RAG 抽象层完成；待开发阶段更新 |
| [PRD.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/docs/internal/PRD.md) | 新增三轨功能对应需求条目（由专项 Agent 处理） |
| [DESIGN.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/docs/internal/DESIGN.md) | 新增 §16 章节"三轨并行开发：削峰队列 / 向量检索 / RBAC"（由专项 Agent 处理） |
| [DELIVERY_MATRIX.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/docs/internal/DELIVERY_MATRIX.md) | 新增异步分析、向量检索 RAG、多 key 轮换、RBAC 行；第七节更新 |
| [TODO.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/docs/internal/TODO.md) | 新增 P3-6 / VRAG-001 / AUDIT-2-13 / AUDIT-2-14 已完成条目 |
| [claude-audit-consolidated.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/docs/internal/release/claude-audit-consolidated.md) | 状态总览更新；架构级优化表新增 P3-6 / 向量检索 RAG / AUDIT-2-13 / AUDIT-2-14 行；测试基线 381→485 |
| [README.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/README.md) | 后端调试能力新增异步分析削峰队列 + 向量检索 RAG；项目状态表更新 |
| [handoff.md](file:///c:/Users/ASUS/Dev/Projects/ai-debug-mcp/handoff.md) | 本文件，本轮任务交接快照 |
