# 开发计划（当前 Sprint）

> 定位：当前开发执行计划，记录当前 Sprint 目标、近期任务、Bug 列表和开发顺序。
> 长期路线请见 [CODE_REVIEW.md](./CODE_REVIEW.md)。
> 最近更新：2026-07-16
> 当前进度：Phase 1.x 工程化增强阶段，V1-V5 verify ✅，下一阶段 P1 Browser SDK 自动采集

---

## 一、当前 Sprint 目标

**目标**：完成 P1 Browser SDK 自动采集（V1-V6）

**为什么先做这个**：当前浏览器 SDK 只支持基础的手动上报，需要让浏览器端错误、网络请求、UI 事件自动进入 Trace 系统，形成完整的端到端调试链路。

**交付物**：
- 控制台错误自动上报
- 网络请求默认捕获（带采样率）
- 网络错误自动标记静默失败
- SDK 初始化追踪
- UI 静默失败自动检测

---

## 二、近期任务（P1 Browser SDK 自动采集）

| 步骤 | 文件 | 说明 | 状态 |
|------|------|------|------|
| V1 | `browser-sdk/ai-debug.js` | 控制台错误捕获（console.error/warn） | ✅ 已完成 |
| V2 | `browser-sdk/ai-debug.js` | 默认开启网络捕获 + 性能优化（采样率+节流） | 🔲 待开发 |
| V3 | `browser-sdk/ai-debug.js` | 网络错误自动标记静默失败（4xx/5xx） | 🔲 待开发 |
| V4 | `browser-sdk/ai-debug.js` | SDK 初始化追踪 + 请求关联（trace_id） | 🔲 待开发 |
| V5 | `app/api/ingest.py` | 增强 ingest 端点，支持 SDK 数据关联 | 🔲 待开发 |
| V6 | `browser-sdk/ai-debug.js` | 自动检测 UI 静默失败（点击无响应） | 🔲 待开发 |

### V1 验收标准（已完成）
- `console.error` 调用自动上报到 `/api/ingest/console`
- `console.warn` 调用自动上报到 `/api/ingest/console`

**V1 完成内容**：
- console.error 自动捕获
- console.warn 自动捕获
- MCP console ingest tool
- trace_id/request_id 关联
- 脱敏保护（前端 `_redact()` + 后端 `redact()`）
- 测试覆盖（4 个新增测试用例）

### V2 验收标准
- 网络请求默认捕获
- 采样率生效（networkSampleRate）
- 节流生效（networkThrottleMs）

### V3 验收标准
- 网络 4xx/5xx 自动标记 `silent_failure=true`
- 静默失败数据写入 spec_diffs

### V4 验收标准
- SDK 初始化创建 trace
- 后续上报关联同一 session_id

### V5 验收标准
- `/api/ingest/console` 端点正常接收
- `/api/ingest/init` 端点正常接收
- 数据关联正确

### V6 验收标准
- 点击按钮无响应（2秒内无网络请求）自动报告静默失败

---

## 三、后续 Sprint 预告

> 以下为后续 Sprint 的优先级排序，详细任务将在进入对应 Sprint 时拆分。

| 优先级 | 任务 | 目标 |
|--------|------|------|
| **P2** | SSE 实时 Dashboard | 实现 Trace 实时推送 |
| **P3** | Docker Compose 完善 | 一键启动完整开发环境 |
| **P4** | LLM Root Cause Analysis 增强 | 增强 LLM 分析能力 |
| **P5** | Repository 层优化和 spec_store 持久化 | 延后执行 |

---

## 四、Bug 列表

| Bug | 描述 | 状态 | 处置 |
|-----|------|------|------|
| ENV-001 | stdio 模式被 MCP 客户端从其他项目的工作目录拉起时，`config.py` 相对路径 `env_file=".env"` 按 CWD 解析，加载到目标项目的 `.env`；陌生键触发 pydantic `extra_forbidden`，`Settings()` 初始化即崩，服务无法启动 | ✅ 已修复（2026-07-16） | `config.py` 将 `env_file` 锚定为基于 `__file__` 的项目根绝对路径；已验证项目根目录与外部目录双场景加载正常 |
| WIP-001 | dispatch 链路异步化改动未提交（`protocol/server.py`、`mcp_routes.py`、`auto_test_api.py`、`transports/stdio.py` 等 7 文件），`tests/unit/test_jsonrpc.py` 3 个用例失败（`dispatch` 已改 async，测试仍同步调用） | ⚠️ 半成品 | 待决策：完成异步化并同步更新测试，或回滚改动 |

---

## 五、开发顺序

1. **先做 V1-V2**：基础能力（控制台错误 + 网络捕获）
2. **再做 V3-V4**：增强能力（静默失败标记 + 请求关联）
3. **然后 V5**：服务端配合（ingest 端点增强）
4. **最后 V6**：高级能力（UI 静默失败自动检测）

每步完成后：
- 运行测试（`python -m pytest tests/ -q`）
- 更新 [AI_HANDOFF.md](./AI_HANDOFF.md) 任务交接
- 提交代码（遵循 [AI_RULES.md](./AI_RULES.md) Git 规范）

---

## 六、每日 Review 清单

- [ ] 跑测试：`python -m pytest tests/unit/ -q`
- [ ] 看本文件"二、近期任务"当前做到哪一步（V1–V6 勾选）
- [ ] 决定今天推进哪一步，做完在此勾选 + 提交

### 进度勾选

- [x] V1 控制台错误捕获
- [ ] V2 默认开启网络捕获 + 性能优化
- [ ] V3 网络错误自动标记静默失败
- [ ] V4 SDK 初始化追踪 + 请求关联
- [ ] V5 增强 ingest 端点
- [ ] V6 自动检测 UI 静默失败

---

## 七、关键约束（不可违反）

参见 [AI_RULES.md](./AI_RULES.md)：

- 只改必要文件；保留 TraceStorage/SessionStorage 抽象、MemoryStore/PGStore、middleware.py 安全栈、error_handlers、metrics/health、测试结构
- 不复制外部代码，按现有架构重新实现
- 每模块少量文件、做完即停、汇报"改了什么/为什么/如何测试"
- PGStore 修改须先输出问题分析、影响范围、测试方案
