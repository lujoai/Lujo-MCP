# ai-debug-mcp 项目摘要

> AI Agent 第一入口文件。任何 AI 进入项目请先读本文件，3 分钟理解项目全貌。

---

## 1. 项目一句话介绍

基于 MCP 协议的 AI 智能调试服务，解决"无报错但功能不对"的静默失败检测和"多 Agent 协同调试"两个核心问题。

---

## 2. 当前架构

```
客户端层（MCP客户端/REST/浏览器）
    ↓
传输层（stdio / Streamable HTTP）
    ↓
中间件层（Auth → MaxBodySize → RateLimit → SecurityHeaders → Trace）
    ↓
路由/分发层（/api/debug/* │ /mcp │ /health /metrics）
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
| 入口 | [app/main.py](./app/main.py) | FastAPI 实例、路由注册、lifespan |
| 配置 | [app/config.py](./app/config.py) | pydantic-settings 全局单例 |
| 中间件 | [app/middleware.py](./app/middleware.py) | 7 个中间件（CORS、Auth、MaxBodySize、RateLimit、SecurityHeaders、Trace + NetworkCapture，fail-closed 鉴权） |
| 调试 API | [app/api/debug.py](./app/api/debug.py) | /api/debug/* 路由 |
| Dashboard API | [app/api/dashboard.py](./app/api/dashboard.py) | 从 PostgreSQL 读取 |
| MCP HTTP | [app/api/mcp_routes.py](./app/api/mcp_routes.py) | Streamable HTTP 传输 |
| MCP stdio | [app/mcp_server.py](./app/mcp_server.py) | stdio 子进程传输 |
| 日志核心 | [app/mcp/core/logs.py](./app/mcp/core/logs.py) | add_log/get_logs/list_request_ids |
| 存储工厂 | [app/mcp/core/storage/factory.py](./app/mcp/core/storage/factory.py) | memory/pg 一键切换 |
| PG 存储 | [app/mcp/core/storage/pg_store.py](./app/mcp/core/storage/pg_store.py) | 连接池+自动建表（修改需审批） |
| 上下文构建 | [app/mcp/builders/context.py](./app/mcp/builders/context.py) | build_debug_context |
| 断言引擎 | [app/mcp/verifier/assert_engine.py](./app/mcp/verifier/assert_engine.py) | assert_behavior 纯函数 |
| 规范存储 | [app/mcp/verifier/spec_store.py](./app/mcp/verifier/spec_store.py) | dict+Lock + add_log 持久化 |
| 异常钩子 | [app/mcp/hooks/exception_hook.py](./app/mcp/hooks/exception_hook.py) | sys.excepthook + asyncio |
| LLM 分析 | [app/llm/analyzer.py](./app/llm/analyzer.py) | 重试/超时/fallback/流式 |
| 工具注册 | [app/mcp/tools/__init__.py](./app/mcp/tools/__init__.py) | register_all_tools（15 个工具） |
| 浏览器 SDK | [browser-sdk/ai-debug.js](./browser-sdk/ai-debug.js) | UMD/CJS/ESM 三格式 |

---

## 4. 已完成功能

### 核心能力 ✅

- ✅ 请求追踪（trace 完整链路）
- ✅ 调试上下文构建（结构化 AI 可消费数据）
- ✅ 异常堆栈捕获（sync + asyncio）
- ✅ 运行时快照（psutil）
- ✅ LLM 智能分析（openai/zhipu/custom）
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

### 传输能力 ✅

- ✅ Streamable HTTP 传输（/mcp 端点）
- ✅ stdio 传输（Claude Desktop 子进程）
- ✅ SSE 广播中心
- ✅ MCP 工具双传输注册（HTTP / stdio 均由统一注册表动态导出，当前各 15 个）

### 安全能力 ✅

- ✅ fail-closed 鉴权（hmac.compare_digest）
- ✅ 请求体大小限制（防 DoS）
- ✅ IP 限流（Redis 计数）
- ✅ 安全响应头
- ✅ 入库前脱敏（复合键名子串匹配 + 白名单）
- ✅ /metrics 独立鉴权 toggle
- ✅ CORS 可配置来源

### 前端能力 ✅

- ✅ 浏览器 SDK（UMD/CJS/ESM）
- ✅ **SDK V2 批量上报 + sendBeacon 兜底**
- ✅ Console 自动采集（console.error/warn 自动上报 + trace_id 关联 + 脱敏）
- ✅ Playwright 自动遍历（auto_test）
- ✅ Web 控制台 Dashboard

### 工程化 ✅

- ✅ Docker Compose 一键启动
- ✅ scripts/ 目录（run_tests.sh / lint.sh / init_db.sh）
- ✅ migrations/ 目录（6 个 SQL 文件）
- ✅ GitHub Actions CI
- ✅ 测试基线：**340 passed / 6 skipped / 0 failed**（单元 310 passed + 6 skipped，脱敏集成 18，AsyncPGStore 12）

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

**当前阶段**：v0.3.0 Phase 0-5 全部完成 ✅

**已完成**：
- Phase 0：项目标准化 ✅
- Phase 1：PostgreSQL 集成 ✅
- Phase 1 规范驱动验证 ✅（V1-V5 全部完成）
- v0.3.0 Release Audit 收口 ✅（P0 7/7 ✅，P1 8/8 ✅，2026-07-19）
- Phase 2：PG 异步存储（asyncpg）+ errors 表持久化聚合 ✅
- Phase 3：LLM 异步调用（AsyncOpenAI）+ 多级缓存 ✅
- Phase 4：Browser SDK V2 批量上报 + /ingest/batch ✅
- Phase 5：安全加固（SEC-04/07/08/12/LFI/SSRF/auth hardening）✅

**测试基线：340 passed / 6 skipped / 0 failed**（单元 310 passed + 6 skipped，脱敏集成 18，AsyncPGStore 12）

**后续优先级**（详见 [ROADMAP.md](./docs/internal/ROADMAP.md)）：

| 优先级 | 任务 | 目标 |
|--------|------|------|
| **P1** | Browser SDK V3-V6 | 网络错误自动标记、SDK 初始化追踪、增强 ingest、UI 静默失败检测 |
| **P2** | 数据层长期优化 | traces 表分区、归档策略、批量写入、优雅降级 |
| **P3** | 可观测性与可靠性 | OpenTelemetry 集成、消息队列削峰、熔断器 |
| **P4** | 智能化 | 智能错误分析引擎、RAG 知识库、AI Debug Agent |

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
| PGStore | [app/mcp/core/storage/pg_store.py](./app/mcp/core/storage/pg_store.py) | 已验证，如需修改须先输出问题分析+影响范围+测试方案 |
| 存储抽象层 | [app/mcp/core/storage/base.py](./app/mcp/core/storage/base.py) | 工厂模式基础 |
| 存储工厂 | [app/mcp/core/storage/factory.py](./app/mcp/core/storage/factory.py) | 一行切换核心 |
| 安全中间件 | [app/middleware.py](./app/middleware.py) | fail-closed 安全栈 |
| 全局异常处理 | [app/error_handlers.py](./app/error_handlers.py) | 异常兜底 |
| 可观测性 | [app/observability.py](./app/observability.py) | 监控指标 |

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
| 2 | docs/internal/AI_RULES.md | 了解开发规则 |
| 3 | docs/internal/AI_HANDOFF.md | 了解当前状态，避免重复开发 |
| 4 | docs/internal/DESIGN.md | 理解技术设计 |
| 5 | docs/internal/DEV_PLAN.md | 了解当前任务 |
| 6 | docs/internal/CODE_REVIEW.md | 理解长期方向 |
| 7 | docs/internal/PRD.md | 理解产品需求（最后阅读）|

---

## 8. 关键设计决策

1. **工厂模式**：存储层（memory/PG）、状态层（memory/Redis）、LLM provider（openai/zhipu/custom）都用工厂模式，一行配置切换
2. **规范驱动**：用期望规范作为 ground truth，`assert_behavior()` 纯函数自动比对，偏离即告警，支持 api/ui/rule 三种 kind
3. **双传输**：HTTP 与 stdio 均复用 `register_all_tools()` + `_tool_registry`，避免工具面漂移和漏注册
4. **宿主 AI 推理模式**：服务只交付结构化原始数据，推理交给 Trae/Codex/Claude
5. **安全优先**：fail-closed 鉴权、Content-Length 硬检查、IP 限流、安全响应头、入库前脱敏
6. **幂等性**：异常钩子 `install_global_hook()` 幂等安装，PG 建表 `CREATE TABLE IF NOT EXISTS`
7. **降级策略**：各采集器失败降级不阻断整体，中间件异常降级放行

---

## 9. 配置速查

**关键环境变量**：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `STORAGE_BACKEND` | `memory` | `memory` / `postgresql` |
| `STATE_BACKEND` | `memory` | `memory` / `redis` |
| `PG_HOST` | `localhost` | PostgreSQL 主机 |
| `PG_PORT` | `5432` | PostgreSQL 端口 |
| `PG_DATABASE` | `ai_debug_mcp` | 数据库名 |
| `LLM_PROVIDER` | `openai` | `openai` / `zhipu` / `custom` |
| `OPENAI_API_KEY` | — | LLM API Key |
| `API_KEY` | — | 鉴权密钥（留空不启用） |

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

> AI 进入本项目做任何安全相关判断前，请先读本节与 [docs/internal/SECURITY_REVIEW.md](./docs/internal/SECURITY_REVIEW.md) SEC-01~15、[DESIGN.md](./docs/internal/DESIGN.md) §13。

**整体健康度：8.5 / 10**（工程质量 8.5 / 安全性 8.0 / 架构可维护性 8.5 / 文档可信度 9.0）。核心数据流架构**合理、无需重写**；安全基线扎实，部分长期项（如 C7 source-map）未完成。

**P0（部署前必修）—— ✅ 四项已于 2026-07-22 修复**（下表为原始风险与证据；行为变更见文末）：

| 项 | 一句话 | 证据 |
|----|--------|------|
| SEC-01 LFI | `ingest` 任意 `frames[].file` → dashboard trace 详情 `linecache` 回显文件 | `ingest.py:73`→`code_locator.py:85` |
| SEC-02 SSRF | `verify_ui`/`auto_test` 的 `url` 直连 Playwright `page.goto`，无白名单 | `ui_runner.py:82` |
| SEC-03 免鉴权 | `API_KEY` 默认空即免鉴权；启动防护仅 `__main__`+`0.0.0.0` | `config.py:74`、`main.py:233` |
| SEC-05 无超时 | 工具调用无 `wait_for`，可数分钟阻塞 | `server.py:87` |

**须订正的既有认知**：① “入库前脱敏”对 `exception_hook→errors` 的自动捕获路径不成立（message/traceback 未脱敏，SEC-06）；② “会话隔离”仅 stdio 成立，共享 HTTP 无 `session_id` 维度（SEC-04）；③ 路径白名单默认放行；④ 中间件真实顺序为 `Trace` 最外、`CORS` 内于 `Auth`（非文档所述）。

**已复核为安全**：SQL 全参数化（无注入）、LLM 发送前递归脱敏、assert_engine 纯函数无 `eval`、PG 连接池双检锁正确。**无支付/资金逻辑**；唯一间接财务风险是 LLM 调用无配额（费用失控）。

> 整改追踪见 [release/claude-audit-consolidated.md](./docs/internal/release/claude-audit-consolidated.md)。修任一项须回填状态 + `文件:行` 验证。

**P0 修复后的行为变更（AI 须知）**：① 0.0.0.0+空 `API_KEY` 现会拒绝启动（本地免鉴权用 `HOST=127.0.0.1`）；② 代码/Git 定位默认仅限进程 CWD，读 CWD 外源码需配 `WHITELIST_PATH_PREFIX`/`GIT_PATH_WHITELIST`；③ `verify_ui`/`auto_test` 默认拒私网/元数据/`file://`，本地联调设 `UI_URL_ALLOW_PRIVATE=true`；④ 工具调用受 `TOOL_TIMEOUT_SECONDS`（默认 60s）约束。P1（SEC-04/06/07/08/09）与 P2（SEC-13/M7）已修复。
