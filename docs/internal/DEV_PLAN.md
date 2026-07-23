# 开发计划（当前 Sprint）

> 定位：当前开发执行计划，记录当前 Sprint 目标、近期任务、Bug 列表和开发顺序。
> 长期路线请见 [CODE_REVIEW.md](./CODE_REVIEW.md)。
> 最近更新：2026-07-23
> 当前进度：v0.3.0 Phase 0-5 全部完成 ✅；Release Audit 全部收口 ✅（P0/P1/P2/P3 清零 + C5/C4/H7 核实，已打 `v0.3.0` tag）；测试基线全绿 **单元 310 / 集成 49 passed，0 failed**（test_api.py 鉴权基线已修复）；ruff 0 违规；`.env` BOM 已剥离

---

## 一、当前 Sprint 目标

**目标**：完成 Claude v0.3.0 Release Audit 收口 — ✅ 已完成

**为什么先做这个**：当前已经进入发布前收口阶段，Claude 审查中仍有阻塞发布与高优先级协议/安全问题未清理，必须先确保核心能力、协议一致性、测试与仓库卫生达标。**所有 P0/P1 已清零。**

**交付物**：
- ✅ 剩余 P0 问题完成修复并标记验证状态
- ✅ P1 高优先级问题完成归类与执行排期
- ✅ 发布审查专项清单与交接文档保持同步
- ✅ 核心测试命令具备可复核结果（**340 passed / 6 skipped / 0 failed**）

---

## 二、近期任务（Release Audit 收口）

> Release Audit 审计追踪与待办状态详见 [claude-audit-consolidated.md](./release/claude-audit-consolidated.md)（P0/P1 已全部清零）。

### Browser SDK 后续项

> Browser SDK V2（批量上报 + sendBeacon）已完成，V3-V6 为下一阶段任务。

- ~~`V2` 默认开启网络捕获 + 批量上报 + sendBeacon 兜底~~ — ✅ 已完成
- `V3` 网络错误自动标记静默失败
- `V4` SDK 初始化追踪 + 请求关联
- `V5` 增强 ingest 端点
- `V6` 自动检测 UI 静默失败

---

## 三、后续 Sprint 预告

> 以下为后续 Sprint 的优先级排序，详细任务将在进入对应 Sprint 时拆分。

### 五维代码评估发现的新任务（2026-07-20）

> 来源：[CODE_REVIEW.md](./CODE_REVIEW.md) §五维代码评估报告

| 优先级 | 编号 | 说明 | 状态 |
|--------|------|------|------|
| **P0** | `AUDIT-2-1` | mcp_routes.py:47 `PARSE_ERROR` 未导入 → NameError | ✅ 已修复（2026-07-20）|
| **P0** | `AUDIT-2-2` | mcp_routes.py:47 JSON 解析错误信息外泄 `{e}` | ✅ 已修复（2026-07-20）|
| **P1** | `AUDIT-2-3` | mcp_routes.py:84 错误码误用 `INVALID_REQUEST` → `INTERNAL_ERROR` | ✅ 已修复（2026-07-20）|
| **P1** | `AUDIT-2-4` | spec_store.py:146-148 持锁做 IO（`_restore_from_storage` 阻塞）| ✅ 已修复（2026-07-20）|
| **P1** | `AUDIT-2-5` | spec_store.py:41-43 N+1 查询（1000 次 `get_logs`）| ✅ 已修复（2026-07-20）|
| **P1** | `AUDIT-2-6` | trace_repo.py:102-136 `save_trace` 多次写入非原子 | ✅ 已标注最终一致（2026-07-20）|
| P2 | `AUDIT-2-7` | analyzer.py:42-59 `_get_client` 无锁 | ✅ 已修复（2026-07-20）|
| P2 | `AUDIT-2-8` | session.py:32-37 `registry.get` 返回引用 | ✅ 已修复（2026-07-20）|
| P2 | `AUDIT-2-9` | spec_store.py:117-122 update 非原子 | ✅ 已修复（2026-07-20）|
| P2 | `AUDIT-2-10` | exception_hook.py:58,86 `asyncio.get_event_loop()` 废弃 | ✅ 已修复（2026-07-20）|
| P2 | `AUDIT-2-11` | main.py:135-140 `/health` PG 检查未 commit/rollback | ✅ 已修复（2026-07-20）|
| P2 | `AUDIT-2-12` | redaction.py:51 手机号正则 `\b` 失效 | ✅ 已修复（2026-07-20）|
| P3 | `AUDIT-2-13` | RBAC 角色分级 | 🔲 长期（待后续 Sprint）|
| P3 | `AUDIT-2-14` | API_KEY 轮换机制 | 🔲 长期（待后续 Sprint）|

### 原 Sprint 任务

| 优先级 | 任务 | 目标 |
|--------|------|------|
| **P1** | Browser SDK 自动采集续作 | 完成 V2-V6 |
| **P2** | SSE 实时 Dashboard | 实现 Trace 实时推送 |
| **P3** | Docker Compose 完善 | 一键启动完整开发环境 |
| **P4** | LLM Root Cause Analysis 增强 | 增强 LLM 分析能力 |

### 高并发与企业级数据预防任务（2026-07-22 高级架构师评审）

> 来源：[CODE_REVIEW.md](./CODE_REVIEW.md) §企业级架构综合评审
> 设计详情：[DESIGN.md](./DESIGN.md) §14 高并发设计评审
> 参考架构：无人机巡检平台思路（令牌桶/消息队列/多级缓存/熔断器）

#### Phase 1：短期优化（1-2 周，低风险高收益）

| 优先级 | 编号 | 任务 | 修改文件 | 状态 |
|--------|------|------|----------|------|
| P0 | P1-1 | PG 连接池可配置化（`pg_max_connections` 环境变量，默认 20） | `app/config.py`, `app/mcp/core/storage/pg_store.py` | ✅ 已完成（2026-07-22）|
| P0 | P1-2 | LLM 分析结果缓存（按 fingerprint 缓存，TTL 1h，LRU 100 条） | `app/llm/analyzer.py` | ✅ 已完成（2026-07-22）|
| P1 | P1-3 | 端点级限流（`/ingest/*` 120/min，`/analyze` 10/min） | `app/middleware.py` | ✅ 已完成（2026-07-22）|
| P1 | P1-4 | Dashboard 查询缓存（TTL 30s） | `app/api/dashboard.py` | ✅ 已完成（任务 D）|

#### Phase 2：中期优化（1 个月，中等风险中等收益）

| 优先级 | 编号 | 任务 | 修改文件 | 状态 |
|--------|------|------|----------|------|
| P0 | P2-1 | PG 异步化（asyncpg + asyncio） | `app/mcp/core/storage/pg_store.py`, `app/mcp/core/logs.py` | ✅ 已完成（Phase 2，asyncpg 异步存储 feature flag 灰度）|
| P0 | P2-2 | LLM 调用异步化（AsyncOpenAI） | `app/llm/analyzer.py`, `app/api/debug.py` | ✅ 已完成（Phase 3，AsyncOpenAI + 多级缓存 L1+L2）|
| P1 | P2-3 | 异常聚合持久化（新增 errors 表） | `app/mcp/core/errors.py`, `app/mcp/core/storage/pg_store.py` | ✅ 已完成（Phase 2，errors 表持久化聚合）|
| P1 | P2-4 | spec_store 独立表（替代从 traces 扫描恢复） | `app/mcp/verifier/spec_store.py` | ✅ 已完成（Phase 2，独立 specs 表）|
| P1 | P2-5 | 滑动窗口限流（Redis ZSET 替代固定窗口） | `app/state/store.py` | ✅ 已完成 |

#### Phase 3：长期优化（3 个月，架构升级）

| 优先级 | 编号 | 任务 | 修改文件 | 状态 |
|--------|------|------|----------|------|
| P1 | P3-1 | 数据分区（traces 表按月分区） | `migrations/` | 🔲 待开发 |
| P1 | P3-2 | 归档策略（自动归档 >30 天数据） | `app/mcp/core/storage/pg_store.py` | 🔲 待开发 |
| P2 | P3-3 | 批量写入（executemany 优化） | `app/mcp/core/trace_repo.py` | 🔲 待开发 |
| P2 | P3-4 | 分布式追踪（OpenTelemetry 集成） | 新增 `app/tracing.py` | 🔲 待开发 |
| P2 | P3-5 | 优雅降级（PG 不可用时降级到内存存储） | `app/mcp/core/storage/factory.py` | 🔲 待开发 |
| P3 | P3-6 | 消息队列削峰（Celery/BackgroundTasks 异步 LLM 分析） | `app/llm/analyzer.py`, `app/api/debug.py` | 🔲 待开发 |
| P3 | P3-7 | 多级缓存（L1 进程 LRU + L2 Redis + 防穿透/雪崩/击穿） | 新增 `app/cache.py` | ✅ 已完成（Phase 3，L1 LRU + L2 Redis）|
| P3 | P3-8 | 熔断器（LLM 调用 pybreaker 熔断） | `app/llm/analyzer.py` | 🔲 待开发 |

**预期收益：**
- Phase 1 完成后：并发承载能力提升 3x，LLM 费用降低 60%
- Phase 2 完成后：单 worker 吞吐量提升 5-10x，Dashboard 响应时间降低 90%
- Phase 3 完成后：支持千万级数据量，支持多租户 SaaS 模式

---

## 四、Bug 列表

| Bug | 描述 | 状态 | 处置 |
|-----|------|------|------|
| ENV-001 | stdio 模式被 MCP 客户端从其他项目的工作目录拉起时，`config.py` 相对路径 `env_file=".env"` 按 CWD 解析，加载到目标项目的 `.env`；陌生键触发 pydantic `extra_forbidden`，`Settings()` 初始化即崩，服务无法启动 | ✅ 已修复（2026-07-16） | `config.py` 将 `env_file` 锚定为基于 `__file__` 的项目根绝对路径；已验证项目根目录与外部目录双场景加载正常 |
| WIP-001 | dispatch 链路异步化收口问题 | ✅ 已修复 | 当前单元测试已恢复全绿 |
| AUDIT-001 | Claude v0.3.0 审查项未收口 | ✅ 已收口 | P0 7/7 ✅，P1 8/8 ✅，详见 [claude-audit-consolidated.md](./release/claude-audit-consolidated.md) |

---

## 五、开发顺序

1. ~~先做剩余 P0~~：确保发布阻塞项清零 — **✅ 全部完成**
2. ~~再做专项复核~~：验证已修项经真实通道调用可用 — **✅ 全部完成**
3. ~~然后做 P1~~：收口输出净化、stdio 生命周期、错误码与依赖管理 — **✅ 全部完成**
4. **当前状态**：v0.3.0 Phase 0-5 全部完成 ✅，测试基线 340 passed / 6 skipped / 0 failed
5. 最后恢复业务迭代：继续 Browser SDK V2-V6（V2 批量上报已完成）

每步完成后：
- 运行测试（`python -m pytest tests/ -q`）
- 更新 [AI_HANDOFF.md](./AI_HANDOFF.md) 任务交接
- 提交代码（遵循 [AI_RULES.md](./AI_RULES.md) Git 规范）

---

## 六、每日 Review 清单

- [ ] 跑测试：`python -m pytest tests/unit/ -q`
- [ ] 看本文件“二、近期任务”当前做到哪一项
- [ ] 决定今天推进哪个 `P0/P1`，做完后同步审查清单

### 进度勾选

- [x] C3 trace_repo 兜底存储键与返回 ID 对齐（任务 A，2026-07-19，单测 6 用例全绿；trace_repo.save_trace 始终返回 error_id 作为 add_log key 与返回值）
- [x] C4 PG 持久化与 trace 回读链路（任务 A，2026-07-19，单测 3 用例全绿；save_trace 落 trace_data 到 trace_store + get_trace 回读重建）
- [x] H10 SDK 静默失败上下文补齐（2026-07-19，SDK 环形缓冲 + 服务端透传 + 工具分类入库，12 单测全绿，SDK 端到端待手动复核）
- [x] SCHEMAS schemas 重复定义统一（2026-07-19，删除死代码 debug.py + 重命名 context.py 冲突类，零回归）
- [x] H12 进程边界 / PG 测试卫生（2026-07-19，test_process_boundary.py 3 用例 + test_pg_integration.py LLM 测试重写）
- [x] H4 verify_ui MCP 通道复核（任务 D，2026-07-19，11/11 全绿）
- [x] H5 ingest frames / locals 脱敏复核（任务 D，2026-07-19，13/13 全绿 + 1 审计发现）
- [x] N4 内部错误串全仓复核（任务 D，2026-07-19，已收口 17 类 / 漏网 3 处登记为 follow-up）
- [x] M9 .env 出现未知键启动即崩（2026-07-19，`config.py` 设 `extra="ignore"` + `model_post_init` warning；5 单测全绿）
- [x] SPEC_STORE spec_store 持久化可靠性（2026-07-19，`list_specs()` 新增 `_restore_from_storage()` 恢复逻辑 + 3 新增单测；17/17 全绿）
- [x] M4 JSON-RPC 错误码规范化（2026-07-19，新增 JSONParseError/InvalidRequestError 异常类，dispatch_raw 区分 -32700/-32600，5 文件修改，20 单测全绿）
- [x] N2 LLM 输出零校验/净化（2026-07-19，`analyzer.py` 新增 schema 校验 + fallback，18 单测全绿）
- [x] TEST-FIX test_main.py 测试隔离修复（2026-07-20，`monkeypatch.setattr` 隔离 .env API_KEY 污染，3/3 全绿；unit 251 passed / 6 skipped / 0 failed）

---

## 七、关键约束（不可违反）

参见 [AI_RULES.md](./AI_RULES.md)：

- 只改必要文件；保留 TraceStorage/SessionStorage 抽象、MemoryStore/PGStore、middleware.py 安全栈、error_handlers、metrics/health、测试结构
- 不复制外部代码，按现有架构重新实现
- 每模块少量文件、做完即停、汇报"改了什么/为什么/如何测试"
- PGStore 修改须先输出问题分析、影响范围、测试方案

---

## 八、Release 流程（M10 标准化）

> 版本单一来源：[app/__init__.py](../../app/__init__.py) `__version__`。所有模块统一引用，禁止多处硬编码版本字面量。

每次正式发版按以下步骤执行：

1. **确认版本号**：`app/__init__.py` 的 `__version__` 即将发布的版本（当前 `0.3.0`）。
2. **跑测试基线**：`python -m pytest tests/unit/ -q`（必须 0 failed）+ `python -m pytest tests/integration/ -q`（确认无新增回归；`test_api.py` 鉴权基线失败属已知 .env `API_KEY` 污染，不计入回归）。
3. **打 annotated tag**：`git tag -a v<x.y.z> -m "<版本摘要>"`（如 `git tag -a v0.3.0 -m "v0.3.0 正式发布：P0/P1 全部清零，Phase 0-5 完成"`）。
4. **推送 tag**（外向操作，需用户确认）：`git push origin v<x.y.z>`。
5. **同步文档**：更新 README.md 项目状态表、AI_HANDOFF.md §一、claude-audit-consolidated.md 状态总览与测试基线。

> 历史 tag：`v0.3.0-beta`（beta 阶段）、`v0.3.0-contest`（参赛版本）、`v0.3.0`（正式发布，2026-07-23）。
