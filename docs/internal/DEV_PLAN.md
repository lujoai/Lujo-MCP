# 开发计划（当前 Sprint）

> 定位：当前开发执行计划，记录当前 Sprint 目标、近期任务、Bug 列表和开发顺序。
> 长期路线请见 [CODE_REVIEW.md](./CODE_REVIEW.md)。
> 最近更新：2026-07-19
> 当前进度：Release Audit 收口阶段；Claude 审查清单已归档，当前优先处理剩余 P0/P1

---

## 一、当前 Sprint 目标

**目标**：完成 Claude v0.3.0 Release Audit 收口，优先清空剩余 P0，再处理 P1。

**为什么先做这个**：当前已经进入发布前收口阶段，Claude 审查中仍有阻塞发布与高优先级协议/安全问题未清理，必须先确保核心能力、协议一致性、测试与仓库卫生达标。

**交付物**：
- 剩余 P0 问题完成修复并标记验证状态
- P1 高优先级问题完成归类与执行排期
- 发布审查专项清单与交接文档保持同步
- 核心测试命令具备可复核结果

---

## 二、近期任务（Release Audit 收口）

> Claude 审查原文与状态归一化结果见 [claude-v0.3.0-audit-todos.md](./release/claude-v0.3.0-audit-todos.md)。

| 优先级 | 编号 | 说明 | 状态 |
|--------|------|------|------|
| P0 | `C3` | `trace_repo` 兜底存储键与返回 ID 不统一 | 🔲 待处理 |
| P0 | `C4` | PG 持久化与 trace 回读链路缺失 | 🔲 待处理 |
| P0 | `H10` | SDK 静默失败上下文恒空，事件链缺失 | 🟡 已完成待复核 |
| P0 | `H12` | 进程边界零覆盖，PG 测试断言被降级为 skip | ✅ 已完成（2026-07-19，3 用例 2 passed/1 skipped） |
| P0 | `H4` | `verify_ui` 同步阻塞已改，待 MCP 通道复核 | ✅ 已完成（任务 D，11/11） |
| P0 | `H5` | 局部变量/ingest frames 脱敏已改，待集成复核 | ✅ 已完成（任务 D，13/13） |
| P1 | `N4` | 内部错误串收口已改，待全仓复核 | ✅ 已完成（任务 D，漏网 3 处登记为 follow-up） |
| P1 | `N2` | LLM 输出零校验/净化 | 🔲 待处理 |
| P1 | `N3` | stdio 生命周期资源回收 | 🔲 待处理 |
| P1 | `M1` | storage factory 拼写错误静默回退 | 🔲 待处理 |
| P1 | `M4` | JSON-RPC 错误码不规范 | 🔲 待处理 |
| P1 | `M12` | 依赖管理拆分运行时与开发时依赖 | ✅ 已完成 |

### 当前执行顺序

1. 先完成剩余 P0：`C3`、`C4`（H10 已完成待复核，H12 已完成）
2. 再对已完成待复核项做专项验证：`H4`、`H5`、`N4`、`H10`、`H12`
3. 最后处理 P1：`N2`、`N3`、`M1`、`M4`、`M12`

### Browser SDK 后续项

> Browser SDK 的 V2-V6 暂时降为发布收口后的下一阶段任务，避免与当前 P0/P1 抢优先级。

- `V2` 默认开启网络捕获 + 性能优化
- `V3` 网络错误自动标记静默失败
- `V4` SDK 初始化追踪 + 请求关联
- `V5` 增强 ingest 端点
- `V6` 自动检测 UI 静默失败

---

## 三、后续 Sprint 预告

> 以下为后续 Sprint 的优先级排序，详细任务将在进入对应 Sprint 时拆分。

| 优先级 | 任务 | 目标 |
|--------|------|------|
| **P1** | Browser SDK 自动采集续作 | 完成 V2-V6 |
| **P2** | SSE 实时 Dashboard | 实现 Trace 实时推送 |
| **P3** | Docker Compose 完善 | 一键启动完整开发环境 |
| **P4** | LLM Root Cause Analysis 增强 | 增强 LLM 分析能力 |

---

## 四、Bug 列表

| Bug | 描述 | 状态 | 处置 |
|-----|------|------|------|
| ENV-001 | stdio 模式被 MCP 客户端从其他项目的工作目录拉起时，`config.py` 相对路径 `env_file=".env"` 按 CWD 解析，加载到目标项目的 `.env`；陌生键触发 pydantic `extra_forbidden`，`Settings()` 初始化即崩，服务无法启动 | ✅ 已修复（2026-07-16） | `config.py` 将 `env_file` 锚定为基于 `__file__` 的项目根绝对路径；已验证项目根目录与外部目录双场景加载正常 |
| WIP-001 | dispatch 链路异步化收口问题 | ✅ 已修复 | 当前单元测试已恢复全绿 |
| AUDIT-001 | Claude v0.3.0 审查项未收口 | ⚠️ 进行中 | 详见 [claude-v0.3.0-audit-todos.md](./release/claude-v0.3.0-audit-todos.md) |

---

## 五、开发顺序

1. **先做剩余 P0**：确保发布阻塞项清零
2. **再做专项复核**：验证已修项经真实通道调用可用
3. **然后做 P1**：收口输出净化、stdio 生命周期、错误码与依赖管理
4. **最后恢复业务迭代**：继续 Browser SDK V2-V6

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

- [ ] C3 trace_repo 兜底存储键与返回 ID 对齐
- [ ] C4 PG 持久化与 trace 回读链路
- [ ] H10 SDK 静默失败上下文补齐
- [x] H12 进程边界 / PG 测试卫生（2026-07-19，test_process_boundary.py 3 用例 + test_pg_integration.py LLM 测试重写）
- [x] H4 verify_ui MCP 通道复核（任务 D，2026-07-19，11/11 全绿）
- [x] H5 ingest frames / locals 脱敏复核（任务 D，2026-07-19，13/13 全绿 + 1 审计发现）
- [x] N4 内部错误串全仓复核（任务 D，2026-07-19，已收口 17 类 / 漏网 3 处登记为 follow-up）

---

## 七、关键约束（不可违反）

参见 [AI_RULES.md](./AI_RULES.md)：

- 只改必要文件；保留 TraceStorage/SessionStorage 抽象、MemoryStore/PGStore、middleware.py 安全栈、error_handlers、metrics/health、测试结构
- 不复制外部代码，按现有架构重新实现
- 每模块少量文件、做完即停、汇报"改了什么/为什么/如何测试"
- PGStore 修改须先输出问题分析、影响范围、测试方案
