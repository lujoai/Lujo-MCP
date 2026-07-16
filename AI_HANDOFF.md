# ai-debug-mcp AI 交接协议

> 本文件定义 AI Agent 之间的任务交接格式和当前项目状态摘要。
> 任何 AI 在开始任务前或交接任务时必须阅读此文件。
>
> **职责边界**：本文件只负责当前上下文摘要 + 任务交接模板 + 下一步入口指引。
> 详细开发任务由 [DEV_PLAN.md](./DEV_PLAN.md) 管理，禁止在此复制任务列表。

---

## 一、当前项目状态摘要

| 指标 | 状态 |
|------|------|
| 项目版本 | v0.3.0 |
| MCP 工具数 | HTTP 15 / stdio 14 |
| 测试覆盖 | 当前测试状态以 [README.md](./README.md) 项目状态表为准 |
| 存储后端 | PostgreSQL（生产）/ memory（默认）|
| LLM Provider | openai / zhipu / custom |
| 当前阶段 | Phase 1.x 工程化增强阶段 |
| 当前 Sprint | P1 Browser SDK 自动采集（V1 完成，V2-V6 待开发） |

### 最近完成事项

- ✅ Phase 0：项目标准化（Docker Compose + scripts/ + migrations/）
- ✅ Phase 1：PostgreSQL 集成（PGStore 连接池 + 自动建表 + Dashboard 读取）
- ✅ Phase 1 规范驱动验证（V1 断言引擎 / V2 spec_store / V3 verify 工具 / V4 verify API / V5 spec_diffs 注入）
- ✅ P1 Browser SDK 自动采集：V1 Console Capture（console.error/warn 自动捕获 + MCP tool + trace_id 关联 + 脱敏）
- ✅ 修复 ENV-001：stdio 模式从外部工作目录启动时误加载目标项目 `.env` 导致启动崩溃（`config.py` env_file 锚定项目根绝对路径，详见 [DEV_PLAN.md](./DEV_PLAN.md) §四）

> 完整已完成能力清单请查看 [PROJECT_SUMMARY.md](./PROJECT_SUMMARY.md) §4。

### 当前阻塞问题

- ⚠️ WIP-001：dispatch 链路异步化改动未提交（7 个文件），`tests/unit/test_jsonrpc.py` 3 个用例失败，待决策完成或回滚（详见 [DEV_PLAN.md](./DEV_PLAN.md) §四）。

---

## 二、当前阶段禁止事项

> 完整禁止事项请查看 [AI_RULES.md](./AI_RULES.md) §三。以下为关键提示：

- ❌ 重构 Storage 架构（当前已稳定）
- ❌ 引入 SQLAlchemy / Alembic
- ❌ 绕过 Storage 访问数据库
- ❌ 修改中间件安全栈 / 全局异常处理 / 可观测性模块
- ❌ 大规模重构（除非明确要求）

**PGStore 修改规则**：如需修改 [./app/mcp/core/storage/pg_store.py](./app/mcp/core/storage/pg_store.py)，必须先输出问题分析、影响范围、测试方案，等待确认后再修改。

---

## 三、当前开发方向（摘要）

> 详细任务拆分（V1-V6）请查看 [DEV_PLAN.md](./DEV_PLAN.md)。

| 优先级 | 任务 | 目标 |
|--------|------|------|
| **P1** | Browser SDK 自动采集 | 浏览器端错误/网络/UI 事件自动进入 Trace 系统 |
| **P2** | SSE 实时 Dashboard | Trace 实时推送 |
| **P3** | Docker Compose 完善 | 一键启动完整开发环境 |
| **P4** | LLM Root Cause Analysis 增强 | 增强 LLM 分析能力 |
| **P5** | Repository 层优化和 spec_store 持久化 | 延后执行 |

---

## 四、AI 任务交接模板

每次完成任务后，按以下模板更新交接信息：

```
任务：<任务名称>
当前状态：<进行中/阻塞/完成>
已完成：
  - <已完成项1>
  - <已完成项2>
修改文件：
  - <文件路径1>
  - <文件路径2>
测试结果：
  - pytest: 当前测试状态以 README.md 项目状态表为准
  - API测试: <测试结果>
  - 数据库: <验证结果>
下一步：
  - <下一步计划>
风险：
  - <风险提示>
```

---

## 五、下一步入口

完成任务后，按以下顺序更新文档：

1. **更新 [DEV_PLAN.md](./DEV_PLAN.md)** — 勾选已完成任务，记录下一步
2. **更新 [PROJECT_SUMMARY.md](./PROJECT_SUMMARY.md)** — 如有新完成能力，更新 §4
3. **更新本文件 §一** — 更新最近完成事项和当前 Sprint 状态
4. **运行测试** — `python -m pytest tests/ -q`，结果以 README 项目状态表为准

---

## 六、推荐阅读顺序

任何 AI 进入项目，请按以下顺序阅读：

1. [PROJECT_SUMMARY.md](./PROJECT_SUMMARY.md) — 快速理解项目
2. [AI_RULES.md](./AI_RULES.md) — 了解开发规则
3. [AI_HANDOFF.md](./AI_HANDOFF.md) — 了解当前状态（本文件）
4. [DESIGN.md](./DESIGN.md) — 理解技术设计
5. [DEV_PLAN.md](./DEV_PLAN.md) — 了解当前任务
6. [CODE_REVIEW.md](./CODE_REVIEW.md) — 理解长期方向
7. [PRD.md](./PRD.md) — 理解产品需求（最后阅读）
