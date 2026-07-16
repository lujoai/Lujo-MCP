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
| 中间件 | [app/middleware.py](./app/middleware.py) | 6 个中间件（fail-closed 鉴权） |
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
- ✅ PGStore 连接池（minconn=2, maxconn=10）
- ✅ 自动建表（traces、sessions）
- ✅ 存储工厂模式（memory/pg 一键切换）
- ✅ Dashboard 从 PostgreSQL 读取

### 传输能力 ✅

- ✅ Streamable HTTP 传输（/mcp 端点）
- ✅ stdio 传输（Claude Desktop 子进程）
- ✅ SSE 广播中心
- ✅ MCP 工具双传输注册（HTTP 15 个：`register_all_tools`；stdio 14 个：`mcp_server.py` 独立清单，handler 复用同一批业务函数）

### 安全能力 ✅

- ✅ fail-closed 鉴权（hmac.compare_digest）
- ✅ 请求体大小限制（防 DoS）
- ✅ IP 限流（Redis 计数）
- ✅ 安全响应头
- ✅ 入库前脱敏

### 前端能力 ✅

- ✅ 浏览器 SDK（UMD/CJS/ESM）
- ✅ Console 自动采集（console.error/warn 自动上报 + trace_id 关联 + 脱敏）
- ✅ Playwright 自动遍历（auto_test）
- ✅ Web 控制台 Dashboard

### 工程化 ✅

- ✅ Docker Compose 一键启动
- ✅ scripts/ 目录（run_tests.sh / lint.sh / init_db.sh）
- ✅ migrations/ 目录（6 个 SQL 文件）
- ✅ 测试覆盖（当前测试状态以 [README.md](./README.md) 项目状态表为准）

---

## 5. 当前开发阶段

**当前阶段**：Phase 1.x 工程化增强阶段

**已完成**：
- Phase 0：项目标准化 ✅
- Phase 1：PostgreSQL 集成 ✅
- Phase 1 当前：规范驱动验证 ✅（V1-V5 全部完成）

**调整后的优先级**：

| 优先级 | 任务 | 目标 |
|--------|------|------|
| **P1** | Browser SDK 自动采集 | 让浏览器端错误、网络请求、UI事件自动进入 Trace 系统 |
| **P2** | SSE 实时 Dashboard | 实现 Trace 实时推送 |
| **P3** | Docker Compose 完善 | 一键启动完整开发环境 |
| **P4** | LLM Root Cause Analysis 增强 | 增强 LLM 分析能力 |
| **P5** | Repository 层优化和 spec_store 持久化 | 延后执行 |

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
| 2 | AI_RULES.md | 了解开发规则 |
| 3 | AI_HANDOFF.md | 了解当前状态，避免重复开发 |
| 4 | DESIGN.md | 理解技术设计 |
| 5 | DEV_PLAN.md | 了解当前任务 |
| 6 | CODE_REVIEW.md | 理解长期方向 |
| 7 | PRD.md | 理解产品需求（最后阅读）|

---

## 8. 关键设计决策

1. **工厂模式**：存储层（memory/PG）、状态层（memory/Redis）、LLM provider（openai/zhipu/custom）都用工厂模式，一行配置切换
2. **规范驱动**：用期望规范作为 ground truth，`assert_behavior()` 纯函数自动比对，偏离即告警，支持 api/ui/rule 三种 kind
3. **双传输**：HTTP 侧 `register_all_tools()` 注册 15 个工具；stdio 侧 `mcp_server.py` 维护独立工具清单（14 个，名称有差异如 `context` vs `get_debug_context`），handler 层复用同一批业务函数；注册表完全统一列为待办
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
docker-compose up -d

# 本地开发（确保 PostgreSQL 已运行）
python -m app.main

# stdio 模式（供 Claude Desktop 等本地客户端）
python -m app.mcp_server
```
