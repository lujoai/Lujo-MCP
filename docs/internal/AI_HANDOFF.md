# ai-debug-mcp AI 交接协议

> 本文件定义 AI Agent 之间的任务交接格式和当前项目状态摘要。
> 任何 AI 在开始任务前或交接任务时必须阅读此文件。
>
> **职责边界**：本文件只负责当前上下文摘要 + 任务交接模板 + 下一步入口指引。
> 详细开发任务由 [DEV_PLAN.md](./DEV_PLAN.md) 管理，禁止在此复制任务列表。
> 当前 Release 审查专项清单见 [claude-v0.3.0-audit-todos.md](./release/claude-v0.3.0-audit-todos.md)。

---

## 一、当前项目状态摘要

| 指标 | 状态 |
|------|------|
| 项目版本 | v0.3.0 |
| MCP 工具数 | HTTP 15 / stdio 15 |
| 测试覆盖 | 当前测试状态以 [README.md](../../README.md) 项目状态表为准 |
| 存储后端 | PostgreSQL（生产）/ memory（默认）|
| LLM Provider | openai / zhipu / custom |
| 当前阶段 | Release Audit 收口阶段 |
| 当前 Sprint | Claude v0.3.0 审查项归档与 P0/P1 收口 |

### 最近完成事项

- ✅ Phase 0：项目标准化（Docker Compose + scripts/ + migrations/）
- ✅ Phase 1：PostgreSQL 集成（PGStore 连接池 + 自动建表 + Dashboard 读取）
- ✅ Phase 1 规范驱动验证（V1 断言引擎 / V2 spec_store / V3 verify 工具 / V4 verify API / V5 spec_diffs 注入）
- ✅ P1 Browser SDK 自动采集：V1 Console Capture（console.error/warn 自动捕获 + MCP tool + trace_id 关联 + 脱敏）
- ✅ 修复 ENV-001：stdio 模式从外部工作目录启动时误加载目标项目 `.env` 导致启动崩溃（`config.py` env_file 锚定项目根绝对路径，详见 [DEV_PLAN.md](./DEV_PLAN.md) §四）
- ✅ 已完成 Release 审查专项文档归档：Claude 待办清单已整理到 [claude-v0.3.0-audit-todos.md](./release/claude-v0.3.0-audit-todos.md)
- ✅ M12 依赖拆分：`requirements.txt` 仅保留 10 项运行时依赖（删除 `pytest`）；新建 `requirements-dev.txt`（`-r requirements.txt` + `pytest`/`pytest-asyncio`/`ruff`）；`Dockerfile` 未改；`README.md` §方式二区分生产/开发安装。验收：`pip install -r requirements-dev.txt` 成功，`pytest tests/unit/ -q` 在无 `.env` 污染环境下 212 passed / 6 skipped 全绿。
- ✅ H10：SDK reportSilentFailure 自动附带最近 N 条 network/UI 事件链 + 服务端 ingest 保留 observed/observed_events + 工具层分类入库与 unknown 保留（2026-07-19）
- ✅ H12：进程边界零覆盖补齐 + `test_pg_integration.py` 断言被 try/except 吞导致失败降级为 skip 修复（2026-07-19）

> 完整已完成能力清单请查看 [PROJECT_SUMMARY.md](../../PROJECT_SUMMARY.md) §4。

### 当前阻塞问题

- 🔴 当前发布收口以 [claude-v0.3.0-audit-todos.md](./release/claude-v0.3.0-audit-todos.md) 为准。
- 🔴 剩余 P0 待处理项：`C3`、`C4`、`H10`（H10 已完成，待 SDK 端手动复核）
- ✅ 已完成复核项（任务 D，2026-07-19）：`H4`、`H5`、`N4`
- ✅ WIP-001：dispatch 链路异步化已完成，当前单元测试已恢复全绿。

### 任务 D 复核结论（H4 + H5 + N4）

> 复核交付：新增 `tests/integration/test_mcp_verify_ui.py`、`tests/integration/test_redaction_integration.py`；M9 环境阻断 workaround `tests/conftest.py`。原则：不修改业务代码，仅补测试 + 文档。

**H4（verify_ui MCP 通道复核）— ✅ 已完成**
- 交付：`tests/integration/test_mcp_verify_ui.py`（11/11 全绿）
- 覆盖：
  - handler 同步性 + 注册入口（4 用例）
  - `_handle_tools_call` / `_run_registered_tool` 源码含 `asyncio.to_thread` 包装
  - dispatch 调 `verify_ui` 的 no_spec / kind_not_ui / playwright_not_installed / unknown_tool 路径（4 用例）
  - `monkeypatch.setitem` 替换为 sleep handler，并发 tick 计数器证明事件循环不阻塞（2 用例，pytest 自动还原）
  - mcp SDK `stdio_client` + `ClientSession` 拉起子进程，完整 stdio MCP 链路验证（1 用例）
- 结论：H4 修复有效，verify_ui 经协议层与 stdio 子进程双通道验证不阻塞事件循环。

**H5（locals / ingest frames 脱敏复核）— ✅ 已完成**
- 交付：`tests/integration/test_redaction_integration.py`（13/13 全绿 + 1 条审计发现）
- 覆盖：
  - `save_trace` locals 敏感键名（password / token / secret / api_key / authorization / cookie）→ `"***REDACTED***"`、嵌套 dict 递归脱敏、复合键名 gap 审计发现（3 用例）
  - `save_trace` message 走 `redact()` 正则（2 用例）
  - `save_trace` extra 嵌套脱敏（1 用例）
  - `ingest_api._parse_frames` 仅保留 file/line/function/code，code 字段经 `redact()` 正则脱敏（2 用例）
  - `save_network_record` url/body 脱敏（1 用例）
  - `save_ui_event` payload_json 脱敏（1 用例）
  - `save_console_log` message 脱敏（1 用例）
  - 非敏感数据保留 + 手机号 `***PHONE***` 非回归（2 用例）
- 审计发现：`_SENSITIVE_KEYS` 为精确匹配集合，复合键名（如 `db_password`、`user_token`）不被 dict-key 路径脱敏，仅当字符串值命中 `redact()` 正则时才被掩码。登记为 follow-up，未改业务代码。
- 结论：H5 修复有效，端到端脱敏链路验证通过。

**N4（内部错误串全仓复核）— ✅ 已完成**
- 交付：6 模式 grep 复核报告（含补充模式 `HTTPException\([^)]*detail.*str\(e`）
- 已收口（17 类）：`api/debug.py`、`api/ingest.py`、`app/mcp_server.py`、`protocol/server.py`、`error_handlers.py`、`api/mcp_routes.py`、`collectors/runtime.py`、`tools/auto_test_api.py`、`tools/debug_api.py`、`collectors/stacktrace.py`（采集数据经 redact 入库）等
- 明确漏网（3 处，登记为新 follow-up，不修复）：
  - `app/api/spec.py:23,34` — `HTTPException(detail=f"创建规范失败: {e}")` / `f"列出规范失败: {e}"`，原始异常外泄到客户端
  - `app/mcp/transports/stdio.py:70` — `make_error(req.id, INTERNAL_ERROR, f"内部错误: {e}")`，stdio 通道异常细节外泄（注：此模块未被 mcp_server 主入口使用）
  - `app/mcp/core/storage/pg_store.py:59` — `raise RuntimeError(f"无法连接 PostgreSQL: {e}")`，启动期错误含 PG 连接参数细节
- 边界（4 处，风险较低）：`app/mcp/transports/stdio.py:61`、`app/mcp/protocol/server.py:160`、`app/api/mcp_routes.py:47`、`app/mcp/verifier/ui_runner.py:86,116,132,193`
- 日志路径（不算漏网）：`logger.error/exception` 写 `str(e)` 是服务端内部日志，不外泄给客户端
- 结论：N4 主干已收口，漏网 3 处已登记到 `claude-v0.3.0-audit-todos.md` §四作为新 follow-up，等待用户确认是否单独开任务修复。

### H10 任务交接（2026-07-19）

```
任务：H10 — SDK reportSilentFailure 不带事件链 + 服务端丢弃 observed
当前状态：已完成待复核
已完成：
  - SDK 端：browser-sdk/ai-debug.js 新增 silentFailureContextSize 配置（默认 20）+ _recentNetwork/_recentUI 环形缓冲；
    network 摘要（method/url/status_code/duration_ms/timestamp/request_body_preview≤512 字符/error）入缓冲；
    UI 事件原始结构入缓冲；reportSilentFailure 自动拼装 observed_events 数组（{kind:"network"|"ui", data:{...}}）上报。
    完整 record 仍走 /ingest/network 实时上报，环形缓冲仅给 reportSilentFailure 用。
  - 服务端：app/api/ingest.py 的 /ingest/silent-failure 端点透传 observed 与 observed_events 字段。
  - 工具层：app/mcp/tools/silent_failure_api.py 新增 observed/observed_events 参数；
    observed_events 按 kind 分流（network→parse_network_records→save_network_record，ui→parse_ui_events→save_ui_event）；
    无法识别 kind 的事件保留到 extra.observed_events_unknown（不丢弃，约束 2）；
    extra 记录 observed_event_count/observed_event_merged_count/observed_event_unknown_count；
    SILENT_FAILURE_DEF schema 用 JSON Schema 语法声明 observed/observed_events 字段（约束 1）。
  - trace_repo/get_debug_context 不修改：observed 字符串与 observed_events 通过 trace.extra 自动持久化，
    observed_events 分流入库后通过 network_trace/ui_events 自动可被 get_debug_context 取回。
  - 测试：tests/unit/test_silent_failure.py 新增 6 个用例覆盖 observed 文本持久化、observed_events 链路、
    脱敏（password/token 字段位置明确）、unknown 保留、与外部记录合并、TestClient 端到端字段透传。
修改文件：
  - browser-sdk/ai-debug.js
  - app/api/ingest.py
  - app/mcp/tools/silent_failure_api.py
  - tests/unit/test_silent_failure.py
  - docs/internal/AI_HANDOFF.md（本条目）
  - docs/internal/DEV_PLAN.md（H10 勾选）
  - docs/internal/release/claude-v0.3.0-audit-todos.md（H10 状态更新）
测试结果：
  - pytest tests/unit/test_silent_failure.py: 12 passed
  - pytest tests/unit/: 211 passed, 1 failed（test_main.py 鉴权断言，.env API_KEY 污染，与本任务无关）, 6 skipped
  - pytest tests/integration/ (不含 PG): 28 passed, 7 failed（test_api.py 鉴权断言，同 .env API_KEY 污染，与本任务无关）, 1 skipped
  - SDK 端 JS 行为：未跑（项目无 JS 测试基础设施），待手动跑 examples/silent_failure_demo.html 验证
  - 数据库：未涉及 PG schema 变更，trace_repo 复用现有 extra dict 持久化路径
下一步：
  - H12 进程边界 / PG 测试卫生
  - H10 复核：手动跑 examples/silent_failure_demo.html 验证 SDK 端 observed_events 拼装实际落到服务端
  - C3 / C4 trace_repo 兜底存储键与 PG 持久化链路
风险：
  - JS SDK 行为仅靠代码审查与单元测试间接覆盖，端到端验证需手动操作
  - observed_events 与外部 network_records/ui_events 不去重合并，AI 需结合 observed_event_count/merged_count 判断
  - 测试环境 .env 含 API_KEY=test_secret_key_456，导致 test_main.py 与 test_api.py 鉴权断言失败，属 M7 范畴，本任务不修
```

### H12 任务交接（2026-07-19）

```
任务：H12 — 进程边界零覆盖 + test_pg_integration.py 断言被 try 吞导致失败降级为 skip
当前状态：已完成待复核
已完成：
  - 改动 1：tests/integration/test_pg_integration.py 重写 TestLLMIntegration.test_analyze_with_llm_returns_structure
    * 移除 try/except Exception 吞断言模式
    * 三状态显式处理：未配置 OPENAI_API_KEY → pytest.skip(明确原因)；
      配置但调用失败 → 异常真实抛出 fail；配置且成功 → 断言返回结构
    * 同文件其他 try/finally 资源清理模式（L37-L43、L49-L56）保留不动
  - 改动 2：新增 tests/integration/test_process_boundary.py（3 用例）
    * test_stdio_mcp_server_handshake：python -m app.mcp_server 启动 + JSON-RPC initialize 握手
      - readline + JSON 解析（线程+Queue 10s 超时）
      - 失败附 stderr 诊断
    * test_http_main_health_endpoint：python -m app.main 启动 + /health 200
      - _find_free_port() 系统分配空闲端口，_wait_for_health() 轮询 15s 超时
      - 断言 service/status 字段，status ∈ {ok,degraded,unhealthy}
    * test_pg_pool_closed_on_shutdown：进程终止后 PG 池不泄漏
      - 前置探测 STORAGE_BACKEND=postgresql + PG 可连通性（SELECT 1），失败显式 skip
      - 平台差异：Unix SIGTERM 严格断言 "连接池已关闭" 日志；Windows terminate() 只断言进程超时内退出
      - 严格断言（所有平台）：进程在 15s 超时内退出（无挂死 = 无连接泄漏阻塞）
  - 关键设计决策：_isolated_env fixture 临时备份+恢复 .env
    * 原因：.env 含 POSTGRES_PASSWORD/DATABASE_URL 等未知键触发 pydantic extra_forbidden（M9 已知问题）
    * 时序：fixture 进入备份 .env → 测试启动子进程（读不到 .env，只从 env 读配置）→ fixture 退出恢复 .env
    * 用 os.replace 原子操作，try/finally 严格保证恢复
    * 验证：测试后 .env 内容完整恢复，无 .env.h12_test_bak 残留
  - 所有跳过用例给出明确原因（STORAGE_BACKEND != postgresql / PG 不可连通 / OPENAI_API_KEY 为空）
修改文件：
  - tests/integration/test_pg_integration.py（重写 test_analyze_with_llm_returns_structure）
  - tests/integration/test_process_boundary.py（新建，3 用例 + 1 fixture + 4 辅助函数）
  - docs/internal/AI_HANDOFF.md（本条目）
  - docs/internal/DEV_PLAN.md（H12 勾选）
  - docs/internal/release/claude-v0.3.0-audit-todos.md（H12 状态更新）
测试结果：
  - pytest tests/integration/test_process_boundary.py: 2 passed, 1 skipped（PG 池测试因 STORAGE_BACKEND != postgresql 显式 skip）
  - pytest tests/integration/test_pg_integration.py: 16 skipped（PG 未启动，全部显式 skip）
  - pytest tests/integration/test_debug_flow.py: 2 passed（无回归）
  - pytest tests/unit/: 211 passed, 1 failed（test_main.py 鉴权断言，.env API_KEY 污染，与本任务无关）, 6 skipped
  - 数据库：未涉及 PG schema 变更，PG 池测试在 PG 启动时才能跑（当前环境未启动 PG）
下一步：
  - C3 / C4 trace_repo 兜底存储键与 PG 持久化链路
  - H10 复核：手动跑 examples/silent_failure_demo.html 验证 SDK 端 observed_events
  - M9 .env 污染问题独立修复（POSTGRES_PASSWORD/DATABASE_URL 不在 Settings 字段中）
风险：
  - Windows TerminateProcess 不触发 lifespan shutdown，PG 池关闭日志在 Windows 上不严格断言（best-effort），已在 docstring 明确说明平台差异
  - .env 隔离 fixture 通过文件 rename 实现，pytest 默认顺序执行无并发冲突；若未来引入 pytest-xdist 并行需重新评估
  - stdio 握手用例依赖 mcp SDK stdio_server 的 newline-delimited JSON-RPC 协议，若 SDK 升级变更协议格式需同步调整 readline 解析
```

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

> 详细开发执行顺序请查看 [DEV_PLAN.md](./DEV_PLAN.md)。

| 优先级 | 任务 | 目标 |
|--------|------|------|
| **P0** | Release Audit 阻塞项收口 | 修复发布前阻塞问题并完成复核 |
| **P1** | 高优先级协议/安全补洞 | 收口 N2/N3/M1/M4 等问题 |
| **P2** | Browser SDK 自动采集后续项 | 完成 V2-V6 |
| **P3** | SSE 实时 Dashboard | Trace 实时推送 |
| **P4** | Docker Compose 与 LLM 配置完善 | 完善开发/部署体验 |

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
2. **更新 [PROJECT_SUMMARY.md](../../PROJECT_SUMMARY.md)** — 如有新完成能力，更新 §4
3. **更新本文件 §一** — 更新最近完成事项和当前 Sprint 状态
4. **运行测试** — `python -m pytest tests/ -q`，结果以 [README.md](../../README.md) 项目状态表为准

---

## 六、推荐阅读顺序

任何 AI 进入项目，请按以下顺序阅读：

1. [PROJECT_SUMMARY.md](../../PROJECT_SUMMARY.md) — 快速理解项目
2. [AI_RULES.md](./AI_RULES.md) — 了解开发规则
3. [AI_HANDOFF.md](./AI_HANDOFF.md) — 了解当前状态（本文件）
4. [DESIGN.md](./DESIGN.md) — 理解技术设计
5. [DEV_PLAN.md](./DEV_PLAN.md) — 了解当前任务
6. [CODE_REVIEW.md](./CODE_REVIEW.md) — 理解长期方向
7. [PRD.md](./PRD.md) — 理解产品需求（最后阅读）
8. [claude-v0.3.0-audit-todos.md](./release/claude-v0.3.0-audit-todos.md) — 查看当前 Release 审查收口清单
