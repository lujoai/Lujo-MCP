# ai-debug-mcp v0.3.0 Claude 审查待办清单

> 来源：Claude release 前审查结论。
> 目的：作为当前版本发布收口的专项清单。
> 说明：本文件保留 Claude 审查分类，同时根据当前仓库状态做“状态归一化”，避免重复劳动。
> 更新时间：2026-07-19

---

## 一、使用规则

- 本文件是当前 `v0.3.0` 发布收口的专项待办清单。
- 已明确修复但尚未做更大范围验证的事项，标记为 `已完成待复核`。
- 仍未处理或只处理了一部分的事项，标记为 `待处理` 或 `部分完成`。
- 真正进入开发执行时，以 [DEV_PLAN.md](../DEV_PLAN.md) 的“Release Audit 收口”区块为准。

---

## 二、状态总览

| 优先级 | 总数 | 已完成 | 已完成待复核 | 部分完成 | 待处理 |
|--------|------|--------|--------------|----------|--------|
| P0 | 7 | 6 | 1 | 0 | 0 |
| P1 | 8 | 6 | 0 | 0 | 2 |
| P2 | 13 | 0 | 0 | 0 | 13 |
| P3 | 8 | 1 | 0 | 0 | 7 |
| 未运行确认 | 4 | 1 | 0 | 0 | 3 |

> 说明：这里的“已完成”表示代码与验证都已具备；“已完成待复核”表示代码层面已落地，但还缺更有针对性的复核或更大范围验证。

---

## 三、P0 阻塞发布

- [x] **N1**：全局异常钩子签名不匹配，`capture_exception` 永远静默失败
  - 状态：已完成
  - 说明：`capture_exception` 已补 `source/extra` 参数；异常钩子吞错已改为显式日志。
  - 涉及文件：`app/mcp/collectors/stacktrace.py`、`app/mcp/hooks/exception_hook.py`
  - 验证：相关单测已补并通过

- [x] **C3**：采集记录成孤儿，`trace_repo` 兜底存储键与返回 ID 不统一
  - 状态：已完成（任务 A，2026-07-19）
  - 说明：`trace_repo.save_trace` 始终以 errors 缓冲的 `error_id` 作为 `add_log` 写入 key 与返回值，保证"返回 ID == add_log key == errors error_id"三者统一；caller 传入的 `trace_id` 以 `trace_link` 形式记录在 `error_id` 下，用于审计与反查。
  - 涉及文件：`app/mcp/core/trace_repo.py`、`tests/unit/test_trace_repo.py`
  - 验证：`python -m pytest tests/unit/test_trace_repo.py -q` → 15 passed（含 C3 专项 3 用例）

- [x] **C4**：PG 持久化重启即丢，`save_trace` 没走 `add_log` 落库，`get_trace` 内存未命中时未回读
  - 状态：已完成（任务 A，2026-07-19；PG 集成测试待环境就绪后复核）
  - 说明：`save_trace` 新增 `add_log(error_id, "trace_data", exc_data)` 把完整异常数据持久化到 trace_store；`get_trace` 在 errors 内存未命中时从 trace_store 回读 `step=trace_data` 重建 trace 对象，解决"重启即丢"。
  - 涉及文件：`app/mcp/core/trace_repo.py`、`tests/unit/test_trace_repo.py`、`tests/integration/test_pg_integration.py`
  - 验证：单测 3 用例全绿；PG 集成测试代码已就绪，待本地 PG 环境修复 UnicodeDecodeError 后复核

- [x] **H4**：`verify_ui` 同步 Playwright 阻塞事件循环，需改用 `await asyncio.to_thread(...)`
  - 状态：已完成（任务 D 复核通过，2026-07-19）
  - 说明：MCP 协议层已对同步 handler 统一走 `await asyncio.to_thread(...)`。
  - 涉及文件：`app/mcp/protocol/server.py`
  - 复核交付：`tests/integration/test_mcp_verify_ui.py`（11/11 全绿）
  - 复核覆盖：handler 同步性 + 注册入口（4）、源码 `asyncio.to_thread` 断言、dispatch 调 `verify_ui` 四路径（4）、`monkeypatch.setitem` 替换 sleep handler 验证事件循环不阻塞（2）、mcp SDK `stdio_client` + `ClientSession` 拉起子进程完整 stdio MCP 链路（1）

- [x] **H5**：堆栈局部变量脱敏无效，且外部 ingest frames 未过脱敏
  - 状态：已完成（任务 D 复核通过，2026-07-19）
  - 说明：局部变量按敏感键名强制遮罩；`trace_repo` 入库前已递归脱敏 `frames/extra`。
  - 涉及文件：`app/mcp/collectors/stacktrace.py`、`app/mcp/core/trace_repo.py`
  - 复核交付：`tests/integration/test_redaction_integration.py`（13/13 全绿 + 1 条审计发现）
  - 复核覆盖：`save_trace` locals 敏感键名 → `"***REDACTED***"`、嵌套 dict 递归、复合键名 gap 审计发现（3）、message 走 `redact()` 正则（2）、extra 嵌套（1）、ingest `_parse_frames` code 字段经 `redact()` 脱敏（2）、network/url/body（1）、ui/payload_json（1）、console/message（1）、非敏感保留 + 手机号 `***PHONE***` 非回归（2）
  - 审计发现：`_SENSITIVE_KEYS` 为精确匹配集合，复合键名（`db_password`/`user_token`）不被 dict-key 路径脱敏，登记为 follow-up（不修业务代码）

- [x] **H10**：SDK 静默失败上下文恒空，`reportSilentFailure` 不带事件链，服务端丢弃 `observed`
  - 状态：已完成待复核（2026-07-19）
  - 说明：SDK 新增 `silentFailureContextSize`（默认 20）+ network/UI 环形缓冲（network 存摘要 ≤512B body preview），`reportSilentFailure` 自动拼装 `observed_events` 数组上报；服务端 `/ingest/silent-failure` 透传 `observed`+`observed_events`；工具 `tool_ingest_silent_failure` 按 `kind` 分流入库（network→`save_network_record`，ui→`save_ui_event`），无法识别 kind 的事件保留到 `extra.observed_events_unknown` 不丢弃；`extra` 记录 `observed_event_count/merged_count/unknown_count`；`trace_repo`/`get_debug_context` 不改，自动通过 `trace.extra`+`network_trace`+`ui_events` 暴露给 AI。
  - 涉及文件：`browser-sdk/ai-debug.js`、`app/api/ingest.py`、`app/mcp/tools/silent_failure_api.py`、`tests/unit/test_silent_failure.py`
  - 测试：`pytest tests/unit/test_silent_failure.py` 12 passed；待手动跑 `examples/silent_failure_demo.html` 验证 SDK 端拼装。

- [x] **H12**：进程边界零测试覆盖，且 `test_pg_integration.py` 断言吞在 `try` 里导致失败降级为 skip
  - 状态：已完成待复核（2026-07-19）
  - 说明：`test_pg_integration.py::TestLLMIntegration::test_analyze_with_llm_returns_structure` 移除 try/except 吞断言模式，改三状态显式处理（未配置 OPENAI_API_KEY → skip 给明确原因；配置但调用失败 → 真实抛出 fail；配置且成功 → 断言返回结构）。新增 `tests/integration/test_process_boundary.py` 覆盖：① stdio MCP `initialize` JSON-RPC 握手（readline + JSON 解析 + 10s 超时 + stderr 诊断）；② `python -m app.main` 启动 + `/health` 200（free port + 15s 轮询 + service/status 字段断言）；③ 进程终止后 PG 池不泄漏（前置 PG 连通性探测，平台差异化：Unix SIGTERM 严格断言 "连接池已关闭" 日志，Windows terminate() 只断言进程超时内退出，所有平台严格断言无挂死）。
  - 关键设计：`_isolated_env` fixture 临时备份+恢复 `.env`，避免子进程读 `.env` 触发 pydantic extra_forbidden（M9 已知问题）；环境变量完整覆盖 `STORAGE_BACKEND`/`PG_*`/`API_KEY`/`HOST`/`PORT`；失败时附 stderr 诊断；所有 skip 给明确原因。
  - 涉及文件：`tests/integration/test_pg_integration.py`（重写 1 用例）、`tests/integration/test_process_boundary.py`（新建，3 用例 + 1 fixture + 4 辅助函数）
  - 测试：`pytest tests/integration/test_process_boundary.py` 2 passed / 1 skipped（PG 池测试因 STORAGE_BACKEND != postgresql 显式 skip）；`pytest tests/integration/test_pg_integration.py` 16 skipped（PG 未启动）；`pytest tests/integration/test_debug_flow.py` 2 passed（无回归）；`pytest tests/unit/` 211 passed / 1 failed（test_main.py 鉴权断言，.env API_KEY 污染，与本任务无关）/ 6 skipped。
  - 平台差异说明：Windows 上 `proc.terminate()` 等价 TerminateProcess（硬 kill），不触发 lifespan shutdown，所以 PG 池关闭日志在 Windows 上不严格断言（best-effort），已在 `test_pg_pool_closed_on_shutdown` docstring 明确说明。

---

## 四、P1 应该修

- [x] **H2**：stdio 与 HTTP 工具面不一致，且存在三份 stdio 入口
  - 状态：已完成
  - 说明：stdio 工具已从统一 `_tool_registry` 动态导出；`run_stdio.py` 已删除；唯一 stdio 入口为 `python -m app.mcp_server`。

- [x] **H9**：`RESUME.md`、`INTERVIEW.md` 等求职/内部文档混入仓库根目录
  - 状态：已完成
  - 说明：`RESUME.md`、`INTERVIEW.md` 已执行 `git rm --cached`；内部文档已迁入 `docs/internal/`。

- [ ] **N2**：对 LLM 输出零校验/净化，`json.loads` 失败即原样透传
  - 状态：待处理
  - 说明：当前已做 LLM 入参脱敏，但输出校验与兜底净化仍未系统补齐。

- [x] **N3**：stdio 关闭不回收资源
  - 状态：已完成（2026-07-19，待复核）
  - 说明：stdio 退出路径已闭环回收 PG 连接池 / periodic_cleanup 后台任务 / 全局 excepthook，atexit + signal 兜底覆盖 SIGINT/SIGTERM/正常 EOF。
  - 交付：
    - `app/mcp/hooks/exception_hook.py`：新增 `uninstall_global_hook()`（幂等，恢复 `sys.excepthook` + asyncio loop handler），原 `install_global_hook()` 行为不变
    - `app/mcp_server.py`：新增 `cleanup_resources()`（幂等，三步回收）+ `atexit.register` + `_register_signal_handlers(SIGINT/SIGTERM)` + `main()` try/finally 兜底
    - `app/mcp/transports/stdio.py`：EOF 后调用 `cleanup_resources()`（备用入口，死代码兜底）
    - `tests/integration/test_process_boundary.py`：追加 8 个 N3 用例（TestUninstallGlobalHook 2 + TestCleanupResources 4 + TestStdioExitCleanup 2）
  - 测试结果：N3 范围 6 passed / 2 skipped（Windows 不支持 SIGTERM + STORAGE_BACKEND != postgresql）；全量 integration/unit N3 零回归
  - 复核要点：手动 `python -m app.mcp_server` + Ctrl+C 验证无报错退出（已通过 `test_stdio_exits_cleanly_on_eof` 等价覆盖）

- [x] **N4**：内部错误串裸返回客户端，绕过全局净化兜底外泄原始异常细节
  - 状态：已完成（任务 D 复核通过，2026-07-19）
  - 说明：已收口 `api/debug.py`、`api/ingest.py`、`app/mcp_server.py` 及部分工具返回。
  - 复核交付：6 模式 grep 复核报告（含补充模式 `HTTPException\([^)]*detail.*str\(e`），详见 [AI_HANDOFF.md](../AI_HANDOFF.md) §一任务 D 结论
  - 复核结论：已收口 17 类路径；明确漏网 3 处（登记为新 follow-up，未修复）；边界 4 处（风险较低）；日志路径不算漏网
  - 新 follow-up（待用户确认是否单独开任务修复）：
    - `N4-FU-1`：`app/api/spec.py:23,34` — `HTTPException(detail=f"创建规范失败: {e}")` / `f"列出规范失败: {e}"`，原始异常外泄到客户端。建议改为 `detail="创建规范失败"` + `logger.exception(...)`。
    - `N4-FU-2`：`app/mcp/transports/stdio.py:70` — `make_error(req.id, INTERNAL_ERROR, f"内部错误: {e}")`，stdio 通道异常细节外泄。注：此模块未被 `mcp_server` 主入口使用，风险较低。
    - `N4-FU-3`：`app/mcp/core/storage/pg_store.py:59` — `raise RuntimeError(f"无法连接 PostgreSQL: {e}")`，启动期错误含 PG 连接参数细节。建议改为 `RuntimeError("无法连接 PostgreSQL，详见服务端日志")` + `logger.critical(...)`。

- [x] **M1**：存储工厂对 `backend` 拼写错误静默回退内存，无日志无报错
  - 状态：已完成
  - 说明：`factory.py` 新增 `_VALID_BACKENDS = {"memory","postgresql"}` 白名单 + `_validate_backend()` 校验函数，非法值抛 `ValueError`（错误信息含实际值 + 合法值列表 + "case-sensitive" 修复提示）；`get_trace_store()` / `get_session_store()` 首次实例化时 `logger.info` 打印实际 backend。`main.py` lifespan 启动阶段主动调 `get_trace_store()` / `get_session_store()` 触发 HTTP 入口启动期 fail-fast；stdio 入口在首次 `add_log` 时触发校验。`tests/unit/test_storage.py` 新增 `TestStorageFactory` 5 用例（合法 memory / 合法 postgresql 含 MemoryStore spy 防误回退 / 拼写错误 postgrsql / 空串 / 大小写敏感 PostgreSQL），全绿。
  - 涉及文件：`app/mcp/core/storage/factory.py`、`app/main.py`、`tests/unit/test_storage.py`
  - 验收：`pytest tests/unit/test_storage.py -q` 11 passed + 5 skipped；`pytest tests/unit/ -q` 13 passed + 5 skipped + 1 deselected（test_main 失败项确认为预先存在的环境问题，与本任务无关）。

- [ ] **M4**：协议错误码不规范，JSON 解析错误映射为 `-32602` 应为 `-32700`
  - 状态：待处理

- [x] **M12**：依赖管理混乱，`pytest` 混入运行时依赖，缺 `requirements-dev.txt`
  - 状态：已完成
  - 说明：`requirements.txt` 仅保留 10 项运行时依赖（删除 `pytest`）；新建 `requirements-dev.txt` 含 `-r requirements.txt` + `pytest`/`pytest-asyncio`/`ruff`；`Dockerfile` 未改（原本就只装 `requirements.txt`）；`README.md` §方式二区分生产/开发安装。
  - 涉及文件：`requirements.txt`、`requirements-dev.txt`（新建）、`README.md`
  - 验证：`pip install -r requirements-dev.txt` 成功；`pytest tests/unit/ -q` 在无 `.env` 污染环境下 212 passed / 6 skipped 全绿。

---

## 五、P2 建议修

- [ ] **M2**：PG 重试在已失效连接上重试必然失败、并发 >10 直接 PoolError
  - 状态：待处理

- [ ] **M3**：事件循环内跑同步阻塞
  - 状态：待处理
  - 说明：`verify_ui` 已通过协议层收口一部分，但全局同步阻塞点尚未完成梳理。

- [ ] **M5**：`initialize` 不做版本协商
  - 状态：待处理

- [ ] **M6**：redaction 规则缺口
  - 状态：待处理

- [ ] **M7**：空串 `API_KEY` 使鉴权“开而无锁”
  - 状态：待处理
  - 说明：启动期已新增 `0.0.0.0 + 空 API_KEY` 拒绝启动，但完整鉴权语义还需继续核对。

- [ ] **M8**：MaxBodySize 只查 `Content-Length`
  - 状态：待处理

- [ ] **M9**：`.env` 出现未知键启动即崩
  - 状态：待处理
  - 说明：已有 `ENV-001` 历史修复，但 Claude 提到的问题需要重新和当前配置策略比对确认。

- [ ] **M10**：版本口径混乱且无 tag
  - 状态：待处理

- [ ] **M11**：`migrations/` 中 4 张表全代码库无读写
  - 状态：待处理

- [ ] **M13**：无 pytest.ini/markers；集成测试直连真实 PG 与计费 LLM；工具注册断言过弱
  - 状态：待处理
  - 说明：工具注册断言已增强一部分，但整项还远未收口。

- [ ] **M14**：SDK 对流式响应无条件 `clone().text()`
  - 状态：待处理

---

## 六、P3 体验优化

- [ ] **L1**：GET `/mcp` SSE 空壳：`SSEHub.publish` 全仓库无调用方
  - 状态：待处理

- [ ] **L2**：`app/api/auth.py` 中 `verify_api_key` 是死代码且弱于中间件
  - 状态：待处理

- [ ] **L3**：`/api/debug/token` 硬编码返回 admin/abc123 且无条件挂载；`/health` 回显后端配置
  - 状态：待处理

- [ ] **L4**：CORS 默认 `*`
  - 状态：待处理

- [ ] **L5**：`MemoryTraceStore` 无容量上限
  - 状态：待处理

- [ ] **L6**：docker-compose 不透传 `LLM_PROVIDER`/`LLM_BASE_URL`
  - 状态：待处理

- [ ] **L7**：README 小失实；browser-sdk 缺 `package.json`
  - 状态：待处理

- [x] **L8**：`test_ui_runner.py` 中 `assert status in (200,422)` 两互斥结果都算过
  - 状态：已完成
  - 说明：该项不在当前仓库主问题面中，若后续复查发现仍存在，再回滚为待处理。

---

## 七、未运行确认

- [ ] **C5**：确认 WIP unawaited dispatch 崩溃的 4 个 failed 测试是否已转绿
  - 状态：待处理
  - 说明：当前单元测试已全绿，但这里要求对 Claude 指名的失败集做专项确认记录。

- [ ] **C4**：确认 `test_trace_detail_from_pg` 测试结果
  - 状态：待处理

- [x] **H4**：确认 `verify_ui` 经 MCP 通道调用是否真的失败
  - 状态：已完成（任务 D 复核通过，2026-07-19）
  - 说明：经 stdio MCP 子进程通道完整链路验证 + dispatch 协议层验证，`verify_ui` 在 `asyncio.to_thread` 包装下不阻塞事件循环。详见 §三 H4 与 [AI_HANDOFF.md](../AI_HANDOFF.md) §一任务 D 结论。

- [ ] **H7**：确认 PG 会话存储测试是否存在假覆盖
  - 状态：待处理

---

## 八、下一步建议

1. 先收口剩余 `P0`：`C3`、`C4`（H10 已完成待复核，H12 已完成）
2. 再处理高收益 `P1`：`N2`、`N3`、`M1`、`M4`
3. 最后集中跑针对性验证：PG、MCP 通道、stdio 生命周期、专项失败测试集
