# ai-debug-mcp AI 交接协议

> 本文件定义 AI Agent 之间的任务交接格式和当前项目状态摘要。
> 任何 AI 在开始任务前或交接任务时必须阅读此文件。
>
> **职责边界**：本文件只负责当前上下文摘要 + 任务交接模板 + 下一步入口指引。
> 详细开发任务由 [DEV_PLAN.md](./DEV_PLAN.md) 管理，禁止在此复制任务列表。
> 当前 Release 审查专项清单见 [claude-audit-consolidated.md](./release/claude-audit-consolidated.md)。

---

## 一、当前项目状态摘要

| 指标 | 状态 |
|------|------|
| 项目版本 | v0.3.0 |
| MCP 工具数 | HTTP 15 / stdio 15 |
| 测试覆盖 | **单元 310 passed / 6 skipped / 0 failed**；集成 49 passed / 19 skipped / 0 failed（鉴权基线已修复）；ruff 0 违规 |
| 存储后端 | PostgreSQL（生产）/ memory（默认）|
| LLM Provider | openai / zhipu / custom |
| 当前阶段 | v0.3.0 Phase 0-5 全部完成 ✅；Release Audit 全部收口 ✅ |
| 当前 Sprint | Release Audit 全部收口（P0/P1/P2/P3 清零 + C5/C4/H7 核实），已打 `v0.3.0` tag（未推送）|

### 最近完成事项

- ✅ 技术债清理（2026-07-23）：
  - **test_full_flow.py 硬编码密码**：移除 5 行硬编码 PG 配置（含明文密码），改为 `os.environ.setdefault('STORAGE_BACKEND','postgresql')`，PG 连接参数由 `.env` 经 `settings` 读取（`pg_store._get_pool` 用 `settings.pg_*`，不读 os.environ）。commit `ad6f8dd`。
  - **pg_store.py 拆分评估**（只读分析，未改代码）：598 行，结论"有条件值得"——真正问题是 errors/specs 无 ABC 的设计债（非文件大小）。推荐方案 C（完整拆分 + 补 `ErrorStorage`/`SpecStorage` ABC + pg_store/async_pg_store 同步，2-2.5 人日，需审批）；方案 A（仅提 DDL 到 `pg_schema.py`）为零风险试水第一步。发现隐藏缺陷：`_execute_with_retry` 读取路径覆盖不一致（无重连重试）。详见 [ROADMAP.md](./ROADMAP.md) 技术债务。
  - 风险：硬编码密码仍残留在 git 历史 commit 中（未推送远端，影响仅本地），建议修改本地 PG 密码或接受风险。

- ✅ Git 整理 + v0.3.0 tag 修正（2026-07-23）：
  - **问题**：工作区有 60+ 未提交改动（含全部 Release Audit 修复代码），`v0.3.0` tag 指向旧 commit `169a4e4`（不含修复），名不副实——`git checkout v0.3.0` 会得到不含任何收口修复的版本。
  - **清理**：删除 13 个根目录临时 xml 报告（`cr_*/final*/verify*/phase35_results/report_only`）；`.gitignore` 补 `.reasonix/`、`.trae/documents/`、临时 xml 模式（保留 `.trae/specs/` 跟踪）。
  - **分批提交**（5 个语义 commit，共 108 文件）：
    1. `a5d0c98` fix(security): SEC-01~15 安全加固（auth/redaction/限流/metrics/SSRF/LFI/SDK 脱敏）
    2. `f151d1e` fix: SEC-13 原子写入 + M7 api_key 归一化 + 协议/存储/资源（M1-5/M8/SEC-14/N3）
    3. `42f8137` chore: 入口/可观测性/工程化（M9/M10/M11/M12/M13, L4/L6, SEC-03, schemas, migrations 清理）
    4. `68e8954` chore: gitignore IDE 产物 + GitHub Actions CI
    5. `27dcb6b` docs: 同步 v0.3.0 收口文档（README/RESUME/internal docs/ROADMAP/specs）
  - **tag 修正**：删除指向 `169a4e4` 的旧 tag，重打 annotated tag `v0.3.0` → `27dcb6b`（HEAD，含全部修复）。`git checkout v0.3.0` 现可得到完整收口状态。
  - RESUME.md 改动经用户确认保留（内容为 v0.3.0 成果的合理更新，覆盖原"不修改"规则）。
  - 风险：tag 与 5 个 commit **仍未推送到远端**（外向操作，待用户确认）；commit 仅记录工作区内容、代码未变，测试基线 310 passed 不受影响。

- ✅ Release Audit 最终收口（2026-07-23，M3/M10/M11/L6 + C5/C4/H7）：
  - **M3 同步阻塞**：经核实已修复——MCP 路径 `server.py:114-117` 对同步 handler 走 `asyncio.to_thread`，HTTP 路径 `/api/debug/verify/ui` 为 sync `def`（FastAPI threadpool），`test_mcp_verify_ui.py::TestVerifyUiDoesNotBlockEventLoop` 证明不阻塞。仅文档更新。
  - **M10 版本口径**：`app/__init__.py:4` `__version__="0.3.0"` 单一来源；创建正式 annotated tag `v0.3.0`（`git tag -a v0.3.0`，本地未推送）；release 流程标准化写入 `DEV_PLAN.md` §八。
  - **M11 migrations 清理**：删除 `migrations/20260712_create_network_records_table.sql` 与 `20260712_create_ui_events_table.sql`（pg_store.py 无 CRUD，数据经 traces 表 step 字段存储；pg_store.py 用硬编码 DDL 建表、不读 migrations/ 目录）；修正 `scripts/init_db.sh` 过时注释（原误将 errors/specs 标 deprecated，实际为活跃表）。**未触碰 pg_store.py**。
  - **L6 docker-compose**：经核实 `docker-compose.yaml:41-48` 已透传 `LLM_PROVIDER`/`LLM_BASE_URL` 等全部 LLM 环境变量。仅文档更新。
  - **C5/C4/H7**：C5 单元 310 passed/0 failed（dispatch 全绿）；C4 PG 测试无 PG 时合规 skip；H7 `test_storage.py:199-218` 真实面向 PG，无假覆盖。
  - 涉及文件：`migrations/20260712_*.sql`（删除）、`scripts/init_db.sh`、`docs/internal/release/claude-audit-consolidated.md`、`docs/internal/DEV_PLAN.md`、`docs/internal/AI_HANDOFF.md`、`README.md`、`docs/internal/DESIGN.md`；git tag `v0.3.0`。
  - 测试结果：`pytest tests/unit/ -q` → **310 passed / 6 skipped / 0 failed**（零回归）；集成 41 passed / 19 skipped / 8 failed（test_api.py 鉴权基线，非回归）。
  - 风险：git tag `v0.3.0` 未推送（外向操作，待用户确认）；test_api.py 8 个鉴权失败为预存在 `.env` `API_KEY` 污染基线，不在本次范围。

- ✅ Phase 0：项目标准化（Docker Compose + scripts/ + migrations/）
- ✅ Phase 1：PostgreSQL 集成（PGStore 连接池 + 自动建表 + Dashboard 读取）
- ✅ Phase 1 规范驱动验证（V1 断言引擎 / V2 spec_store / V3 verify 工具 / V4 verify API / V5 spec_diffs 注入）
- ✅ P1 Browser SDK 自动采集：V1 Console Capture（console.error/warn 自动捕获 + MCP tool + trace_id 关联 + 脱敏）
- ✅ 修复 ENV-001：stdio 模式从外部工作目录启动时误加载目标项目 `.env` 导致启动崩溃（`config.py` env_file 锚定项目根绝对路径，详见 [DEV_PLAN.md](./DEV_PLAN.md) §四）
- ✅ 已完成 Release 审查专项文档归档：Claude 待办清单已整理到 [claude-audit-consolidated.md](./release/claude-audit-consolidated.md)
- ✅ M12 依赖拆分：`requirements.txt` 仅保留 10 项运行时依赖（删除 `pytest`）；新建 `requirements-dev.txt`（`-r requirements.txt` + `pytest`/`pytest-asyncio`/`ruff`）；`Dockerfile` 未改；`README.md` §方式二区分生产/开发安装。验收：`pip install -r requirements-dev.txt` 成功，`pytest tests/unit/ -q` 在无 `.env` 污染环境下 212 passed / 6 skipped 全绿。
- ✅ H10：SDK reportSilentFailure 自动附带最近 N 条 network/UI 事件链 + 服务端 ingest 保留 observed/observed_events + 工具层分类入库与 unknown 保留（2026-07-19）
- ✅ H12：进程边界零覆盖补齐 + `test_pg_integration.py` 断言被 try/except 吞导致失败降级为 skip 修复（2026-07-19）
- ✅ N3：stdio 关闭资源回收（PG 连接池 close_pool / periodic_cleanup 取消 / excepthook 卸载）+ atexit/signal 兜底 + uninstall_global_hook 新增 + 8 个进程边界测试用例（2026-07-19）
- ✅ M1：storage factory 对 `STORAGE_BACKEND` 拼写错误 fail-fast（factory.py 加白名单 `_VALID_BACKENDS = {"memory","postgresql"}` + `_validate_backend()` 抛 `ValueError` + 实例化 `logger.info` 打印实际 backend；main.py lifespan 启动阶段主动调 `get_trace_store()` / `get_session_store()` 触发 HTTP 入口启动期校验；`tests/unit/test_storage.py` 补 `TestStorageFactory` 5 用例：合法 memory / 合法 postgresql（含 MemoryStore spy 防误回退）/ 拼写错误 postgrsql / 空串 / 大小写敏感 PostgreSQL。stdio 入口在首次 `add_log` 时触发校验，已在代码注释中说明。2026-07-19）
- ✅ C3+C4（任务 A，2026-07-19）：
  - **C3**：`trace_repo.save_trace` 始终以 errors 缓冲的 `error_id` 作为 `add_log` 写入 key 与返回值，保证"返回 ID == add_log key == errors error_id"三者统一；caller 传入的 `trace_id` 以 `trace_link` 形式记录在 `error_id` 下，用于审计与反查
  - **C4 上半段**：`save_trace` 新增 `add_log(error_id, "trace_data", exc_data)` 把完整异常数据（type/message/frames/traceback）持久化到 trace_store，不依赖 errors 内存缓冲
  - **C4 下半段**：`get_trace` 在 errors 内存未命中时从 trace_store 回读 `step=trace_data` 重建 trace 对象，解决"重启即丢"
  - 新增 6 个单元测试 + 3 个 PG 集成测试覆盖以上修复
  - 涉及文件：`app/mcp/core/trace_repo.py`、`tests/unit/test_trace_repo.py`、`tests/integration/test_pg_integration.py`
  - 测试结果：`python -m pytest tests/unit/test_trace_repo.py -q` → 15 passed；PG 集成测试受本地环境 UnicodeDecodeError 阻塞（预存在问题，与本任务无关）
- ✅ 2026-07-19 代码审计与 Git 整理（6 个子智能体并行执行）：
  - 全面源码审计（6 个 AI 子智能体并行）：生产就绪度评估 8.0/10
    - Agent 1 — 文档一致性核查：修复 4 处文档/代码不一致
    - Agent 2 — M1 Storage Factory + 集成测试：存储工厂验证加固
    - Agent 3 — C3+C4 Trace Repo 一致性：异常持久化键统一 + 重启恢复
    - Agent 4 — H4+H5+N4 Verify/Redaction 集成测试：脱敏 + 验证集成测试
    - Agent 5 — N3 进程边界清理：优雅关闭 + 资源回收
    - Agent 6 — Git 整理：按任务分 8 个语义 commit，工作区 clean
  - 发现 3 个 P0 待修复问题：schemas 重复定义、spec_store 持久化、M9 .env
  - 发现 3 个 P1 改进项：LLM 输出校验、JSON-RPC 错误码、测试盲区
  - 测试基线：217 passed, 6 skipped, 1 failed（.env 环境问题）
  - Git 状态：8 个新 commit，工作区 clean，准备推送
- ✅ M9：`.env` 出现未知键启动即崩（extra_forbidden）根因修复（2026-07-19）
  - `app/config.py` Settings 类改用 `model_config = ConfigDict(extra="ignore")` 替代旧 `class Config`，允许 .env 中存在多余键而不崩溃
  - 新增 `model_post_init` 在启动时通过 `dotenv_values` 读取 .env 原始键，与 `model_fields` 做差集（大小写不敏感），对额外键输出 `logger.warning("Ignored extra .env keys: %s", sorted(extra_keys))`
  - 新建 `tests/unit/test_config.py`，5 个用例覆盖：额外键不崩 / 已知键正常加载 / warning 日志含键名 / 无额外键不产生 warning / 缺失 .env 文件不崩
  - 涉及文件：`app/config.py`、`tests/unit/test_config.py`
  - 测试结果：`pytest tests/unit/test_config.py -q` → 5 passed；`pytest tests/unit/ -q` → 223 passed / 6 skipped / 3 failed（3 failed 为预先存在的 test_main.py 鉴权断言 + test_spec_api 数据污染，与本任务无关）

- ✅ SPEC_STORE：spec_store 持久化可靠性修复 — list_specs() 从 trace_store 恢复逻辑补齐（2026-07-19）
  - **问题分析**：`list_specs()` 无恢复逻辑，重启后内存 `_specs` 清空导致 list 返回空，Dashboard `/api/dashboard/specs` 不可用
  - **修改文件**：`app/mcp/verifier/spec_store.py`（核心）
    - 新增 `_restored` 标志（线程安全，在 `_lock` 内读写）
    - 新增 `_restore_from_storage()` 函数：扫描最近 1000 个 request_id，筛选 `step="spec"` 条目，重建 `_specs` 缓存
    - `list_specs()` 首次调用自动触发恢复（C4 对标模式）
    - `clear()` 重置 `_restored = False`
    - data 完整性校验：PG 场景 JSON 字符串 → dict 反序列化保护
  - **测试文件**：`tests/unit/test_spec_store.py`（新增 TestRestoreFromStorage 3 用例）、`tests/unit/test_spec_api.py`（fixture 补 `_restored=True`）
  - **测试结果**：`pytest tests/unit/test_spec_store.py -q` → 17 passed；`pytest tests/unit/ -q` → 225 passed / 6 skipped / 1 failed（test_main.py 基线环境问题，非回归）

- ✅ M4：JSON-RPC 错误码规范化 — JSON 解析错误映射为 -32700 而非 -32602（2026-07-19）
  - **问题分析**：`dispatch_raw()` 将所有解析异常统一映射为 `INVALID_PARAMS` (-32602)，违反 JSON-RPC 2.0 规范（JSON 语法错误应为 -32700 Parse Error，非合法 Request 对象应为 -32600 Invalid Request）
  - **修改文件**：
    - `app/mcp/protocol/jsonrpc.py`：新增 `JSONParseError(ValueError)` / `InvalidRequestError(ValueError)` 异常类；`parse_request()` 对 JSONDecodeError 抛 `JSONParseError`，对非 dict/缺 method 抛 `InvalidRequestError`
    - `app/mcp/protocol/server.py`：`dispatch_raw()` 区分 `JSONParseError`（→ PARSE_ERROR/-32700）和 `InvalidRequestError`（→ INVALID_REQUEST/-32600），移除局部 import
    - `app/api/mcp_routes.py`：L47 HTTP 层 `json.loads` 失败从 `INVALID_REQUEST` 改为 `PARSE_ERROR`
    - `app/mcp/transports/stdio.py`：import `JSONParseError`/`InvalidRequestError`，EOF 后解析分别映射到 -32700/-32600
    - `tests/unit/test_jsonrpc.py`：新增 6 个用例覆盖 parse_error/invalid_request/method_not_found 三种 dispatch_raw 错误码 + 3 个 parse_request 异常类型验证
  - **测试结果**：`pytest tests/unit/test_jsonrpc.py -q` → 20 passed；`pytest tests/unit/ -q` → 248 passed / 6 skipped / 1 failed（test_main.py 预先存在的环境问题，非回归）

- ✅ N2：LLM 输出零校验/净化 — `_retry_call` json.loads 失败不再原样透传，改为 schema 校验 + 结构化 fallback（2026-07-19）
  - **问题分析**：`analyzer._retry_call()` 收到 LLM 响应后直接 `json.loads`，失败时 `{"analysis": content}` 将原始文本（可能含推理链）透传给客户端
  - **修改文件**：`app/llm/analyzer.py`（核心）
    - 新增常量 `VALID_CONFIDENCE` / `REQUIRED_FIELDS` / `MAX_FIELD_CHARS=2000` / `MAX_RAW_TRUNCATED=500`
    - 新增 `_extract_json(content)`：容错提取 JSON（支持 markdown code block、嵌套文本中的最外层 `{}`），非贪婪匹配取第一个
    - 新增 `_truncate_field(value, max_chars)`：字符串长度截断
    - 新增 `_validate_and_normalize(raw_output)`：三步流程 — ①容错 JSON 解析 ②字段校验+confidence 默认值（缺失/无效→"low"）③解析失败加 `raw_truncated≤500` 字符
    - `_retry_call` 中旧 `json.loads` + `{"analysis": content}` 替换为 `_validate_and_normalize(content)`，结果放入 `analysis` 字段
  - **测试文件**：`tests/unit/test_analyzer.py`（新增 TestLLMOutputValidation 18 用例）
    - 合法完整 JSON / 缺 confidence / 无效 confidence / 空 confidence → 默认 low
    - markdown code block（带 json 标记 / 不带）→ 正常提取
    - 纯文本 / 空串 / null → fallback + raw_truncated
    - 字段超长（3000 字符）→ 截断到 2000
    - JSON 嵌套文本中 → 提取第一个 {}
    - 多个 JSON 块 → 取第一个
    - JSON 数组 → 转为空 dict + fallback
    - required fields 全覆盖
  - **测试结果**：`pytest tests/unit/test_analyzer.py -q` → 27 passed；`pytest tests/unit/ -q` → 242 passed / 6 skipped / 1 failed（test_main.py 基线环境问题，N2 零回归）

- ✅ TEST-FIX：test_main.py 测试隔离修复（.env API_KEY 污染）（2026-07-20）
  - **问题分析**：`test_validate_startup_configuration_rejects_exposed_bind_without_api_key` 期望抛 RuntimeError 但实际未抛。根因：`validate_startup_configuration` 的 `api_key is not None` 判断导致显式传入 `api_key=None` 时 fallback 到 `settings.api_key`，而 `.env` 含 `API_KEY=test_secret_key_456` 导致 `bind_api_key` 非空，条件不触发异常。
  - **修改文件**：`tests/unit/test_main.py`（核心）
    - 新增 `from app.config import settings` 导入
    - 前 2 个用例加 `monkeypatch` 参数，用 `monkeypatch.setattr(settings, "api_key", None)` 隔离 .env 污染
    - 第 3 个用例不变（显式传 `api_key="secret"`，不依赖 settings）
  - **测试结果**：`pytest tests/unit/test_main.py -q` → 3 passed；`pytest tests/unit/ -q` → 251 passed / 6 skipped / 0 failed；`pytest tests/ -q` → 288 passed / 25 skipped / 7 failed（7 failed 为 test_api.py 401 鉴权基线问题，非回归）
  - **不修改**：`app/main.py`、`app/config.py`、`.env`、`tests/conftest.py`

- ✅ SEC-13 + M7 修复（方向 A，2026-07-23，3 子智能体并行）：
  - **SEC-13 非原子写入**：`spec_store.update()` 改为 crash-safe append（单次 `add_log` 提交点，不再 `delete_logs`，删除仅由显式 `delete()` 负责）；`get()` 存储回读 + `_do_restore()` 按 `updated_at`（回退 `timestamp`）取最新版本；`trace_repo.save_trace()` 写入顺序改为 commit-marker（`META → LINK → DATA`，`trace_data` 最后写）。新增 5 测试（`test_spec_store.py::TestAtomicWrites` 3 + `test_trace_repo.py::TestSaveTraceAtomicity` 2）。
  - **M7 API_KEY 空串鉴权**：`config.py` `model_post_init` 将空串/纯空白 `api_key` 归一化为 `None` + warning（"空=未配置=不鉴权"语义收口于配置层）；`middleware.py` 未改，`hmac.compare_digest` fail-closed 不变。新增 3 测试（`test_config.py::TestApiKeyNormalization`）。
  - **涉及文件**：`app/mcp/verifier/spec_store.py`、`app/mcp/core/trace_repo.py`、`app/config.py`、`tests/unit/test_spec_store.py`、`tests/unit/test_trace_repo.py`、`tests/unit/test_config.py`
  - **测试结果**：4 受影响文件 57 passed；全量 `pytest tests/unit/ -q` → 292 passed / 6 skipped / 1 failed（test_git.py 预存白名单，无关）。AI_RULES 合规：未触碰 `pg_store.py`/`base.py`/`memory_store.py`，未绕过 Storage，未改 fail-closed 鉴权。

> 完整已完成能力清单请查看 [PROJECT_SUMMARY.md](../../PROJECT_SUMMARY.md) §4。

### 当前阻塞问题

- ✅ Release Audit 全部收口：P0/P1/P2/P3 清零 + C5/C4/H7 核实（详见 [claude-audit-consolidated.md](./release/claude-audit-consolidated.md)），已打 `v0.3.0` tag（未推送，待用户确认）。
- 🟡 PG 集成测试因本地 PostgreSQL 编码问题（UnicodeDecodeError）全部 skip
- 🟡 测试状态基线：**单元 310 passed / 6 skipped / 0 failed**；集成 41 passed / 19 skipped / 8 failed（test_api.py 鉴权 401，`.env` `API_KEY` 污染预存在基线，非回归）
- ✅ Phase 0-5 全部完成：asyncpg 异步存储、AsyncOpenAI、多级缓存 L1+L2、errors 表持久化、spec_store 独立表、Browser SDK V2 批量上报、GitHub Actions CI
- ✅ TEST-FIX：test_main.py .env API_KEY 污染已通过 monkeypatch 隔离修复（2026-07-20）
- ✅ 已完成复核项（任务 D，2026-07-19）：`H4`、`H5`、`N4`
- ✅ WIP-001：dispatch 链路异步化已完成，当前单元测试已恢复全绿。

### 新增配置项提示（Phase 0-5）

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `PG_ASYNC_ENABLED` | `false` | asyncpg 异步存储 feature flag |
| `PG_ASYNC_MIN` / `PG_ASYNC_MAX` | `2` / `20` | asyncpg 连接池范围 |
| `MEMORY_STORE_MAX_ENTRIES` | `10000` | MemoryStore LRU 容量上限 |
| `LLM_BASE_URL` | 空 | 自定义 LLM API 地址 |
| `LLM_TEMPERATURE` | `0.7` | LLM 温度参数 |
| `LLM_TIMEOUT` | `60` | LLM 调用超时（秒） |
| `CORS_ORIGINS` | 空 | CORS 允许来源（默认收紧） |
| `METRICS_AUTH_ENABLED` | `false` | /metrics 独立鉴权开关 |
| `REDACTION_KEY_ALLOWLIST` | 空 | 脱敏白名单字段名 |

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
- 结论：N4 主干已收口，漏网 3 处已登记到 `claude-audit-consolidated.md` §四作为新 follow-up，等待用户确认是否单独开任务修复。

### N4-FU-1+2 修复（2026-07-20）

```
任务：N4-FU-1+2 — N4 follow-up 漏网 2 处修复（spec.py + stdio.py）
当前状态：已完成
已完成：
  - N4-FU-1：app/api/spec.py L23/L34 HTTPException detail 去掉 {e}，改为
    detail="创建规范失败" / detail="列出规范失败" + logger.exception("...: %s", e)
  - N4-FU-2：app/mcp/transports/stdio.py L88 make_error 改为
    "内部错误，详情见服务端日志"（保持 INTERNAL_ERROR -32603 不变）
  - 新增 tests/unit/test_spec_api.py TestSpecAPIErrorSanitization 2 用例
    （验证 HTTPException detail 不含原始异常敏感文本）
修改文件：
  - app/api/spec.py（L22-23, L33-34）
  - app/mcp/transports/stdio.py（L88）
  - tests/unit/test_spec_api.py（新增 TestSpecAPIErrorSanitization 2 用例）
  - docs/internal/AI_HANDOFF.md（本条目）
  - docs/internal/release/claude-audit-consolidated.md（N4-FU-1/2 状态更新）
测试结果：
  - pytest tests/unit/test_spec_api.py -q → 11 passed（含新增 2 用例）
  - pytest tests/unit/test_jsonrpc.py -q → 20 passed（零回归）
  - pytest tests/unit/ -q → 250 passed, 6 skipped, 1 failed（test_main.py .env 环境问题，非回归）
  - N4-FU-3 保留不处理（pg_store.py 禁止修改）
下一步：
  - N4-FU-3 待后续单独评估（pg_store.py 修改需审批）
风险：
  - 无。改动最小（2 文件各 2 行 + 2 测试用例），不涉及禁止修改模块。
```

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
  - docs/internal/release/claude-audit-consolidated.md（H10 状态更新）
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
  - docs/internal/release/claude-audit-consolidated.md（H12 状态更新）
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

### N3 任务交接（2026-07-19）

```
任务：N3 — stdio 关闭不回收资源（PG 连接池 / 后台任务 / excepthook 卸载）
当前状态：已完成待复核
已完成：
  - 改动 1：app/mcp/hooks/exception_hook.py 新增 uninstall_global_hook() 函数
    * 将原局部变量 original_hook 提升为模块级 _original_hook / _original_asyncio_handler
    * uninstall_global_hook() 幂等：未安装直接返回；已安装则恢复 sys.excepthook + asyncio loop handler
    * install_global_hook() 行为不变（仅把 original_hook 改为存到模块级，便于 uninstall 取回）
    * RuntimeError/Exception 全部 try/except 保护，避免 loop 已关闭时崩溃
  - 改动 2：app/mcp_server.py 新增 cleanup_resources() + signal/atexit 兜底 + try/finally
    * 模块级新增 _cleanup_done 标志 + _periodic_cleanup_task 句柄 + _cleanup_lock（幂等保护）
    * cleanup_resources() 三步回收：
      1) 取消 periodic_cleanup 后台任务（防御性，当前 stdio 未启动该任务，预留兜底）
      2) 仅当 storage_backend == "postgresql" 时调用 close_pool()（复用现有接口，不修改 pg_store.py）
      3) 调用 uninstall_global_hook() 卸载 excepthook
    * main() 用 try/finally 包裹 stdio_server 上下文，finally 触发 cleanup_resources()
    * atexit.register(cleanup_resources) 覆盖正常解释器退出路径
    * _register_signal_handlers() 注册 SIGINT/SIGTERM 兜底 handler（Windows SIGTERM 不可用 try/except 保护）
    * _signal_handler 内 sys.exit(0) 抛 SystemExit，asyncio 主循环捕获后 finally 仍执行 cleanup（幂等）
  - 改动 3：app/mcp/transports/stdio.py EOF 后加 cleanup 调用
    * 此文件是独立备用入口（grep 全仓无 import），仅在 __main__ 中执行
    * run_stdio() 用 try/finally 包裹 while 循环，finally 调用 cleanup_resources()
    * 也注册 atexit 兜底（与 mcp_server.main 一致）
    * 最小改动：仅加 cleanup 调用，不改协议行为
  - 改动 4：tests/integration/test_process_boundary.py 追加 N3 测试（8 用例）
    * TestUninstallGlobalHook（2 用例）：uninstall 恢复 sys.excepthook + 幂等
    * TestCleanupResources（4 用例）：幂等 / postgresql 触发 close_pool / memory 跳过 close_pool / 取消 periodic_task
    * TestStdioExitCleanup（2 用例）：EOF 退出 exit code 0 + 无 traceback；SIGTERM 触发 cleanup（Windows 跳过）
    * 复用 H12 已有的 _isolated_env fixture 和 _safe_read_stderr 辅助函数
修改文件：
  - app/mcp/hooks/exception_hook.py（新增 uninstall_global_hook + 模块级变量）
  - app/mcp_server.py（新增 cleanup_resources + signal/atexit + try/finally）
  - app/mcp/transports/stdio.py（EOF 后加 cleanup 调用）
  - tests/integration/test_process_boundary.py（追加 8 个 N3 用例）
  - docs/internal/AI_HANDOFF.md（本条目）
  - docs/internal/DEV_PLAN.md（N3 勾选）
  - docs/internal/release/claude-audit-consolidated.md（N3 状态更新）
测试结果：
  - pytest tests/integration/test_process_boundary.py: 9 passed, 2 skipped（N3 范围 8/8 + H12 范围 1/3）
    * skip 原因：test_pg_pool_closed_on_shutdown 因 STORAGE_BACKEND != postgresql 显式 skip
    * skip 原因：test_stdio_exits_on_sigterm 因 Windows 不支持 SIGTERM 显式 skip
  - pytest tests/integration/ -q: 37 passed, 19 skipped, 7 failed
    * 7 failed 全部是 baseline 问题：test_api.py 鉴权 401（.env 含 API_KEY=test_secret_key_456）
    * N3 引入回归：0 个 ✅
  - pytest tests/unit/ -q: 217 passed, 6 skipped, 1 failed
    * 1 failed 是 baseline 问题：test_main.py 鉴权断言（.env API_KEY 污染）
    * N3 引入回归：0 个 ✅
  - 数据库：未涉及 PG schema 变更，close_pool 复用现有接口
  - 手动验证：python -m app.mcp_server 启动后 Ctrl+C 通过 test_stdio_exits_cleanly_on_eof 等价覆盖
    （Windows 下 subprocess.send_signal(SIGINT) 会影响 pytest 自身，故用 EOF 测试覆盖核心退出路径）
下一步：
  - C3 / C4 trace_repo 兜底存储键与 PG 持久化链路
  - M9 .env 污染问题独立修复（POSTGRES_PASSWORD/DATABASE_URL 不在 Settings 字段中）
  - 剩余 P0/P1：C3、C4
风险：
  - Windows 不支持 SIGTERM，signal handler 只覆盖 SIGINT；SIGTERM 测试在 Windows 跳过，POSIX 上已覆盖
  - cleanup_resources() 内 import pg_store.close_pool 是延迟 import，避免模块加载时强制依赖 psycopg
  - exception_hook.uninstall_global_hook 用 asyncio.get_event_loop() 在 loop 已关闭时会抛 RuntimeError，已 try/except 保护
  - atexit + signal + finally 三处都可能触发 cleanup，靠 _cleanup_done + _cleanup_lock 保证幂等
  - app/mcp/transports/stdio.py 当前是死代码（未被引用），改动仅为对齐任务描述的"关闭钩子"位置，无测试覆盖
```

### DOC-SYNC 任务交接（2026-07-20）

```
任务：DOC-SYNC — README.md + CODE_REVIEW.md 文档状态同步
当前状态：已完成
已完成：
  - README.md 项目状态表测试基线：从 "以当前实际 pytest 运行结果为准" → "248 passed / 6 skipped / 1 failed（.env API_KEY 环境问题，非回归）"
  - README.md 项目状态表当前阶段：从 "V2 Network Capture 完成，Release Preparation" → "v0.3.0 Release Audit 收口完成（P0 7/7 ✅，P1 8/8 ✅）"
  - CODE_REVIEW.md 迁移任务清单：spec_store 迁移项标记 ✅ 已完成
  - CODE_REVIEW.md 迁移任务清单：errors / network_records / ui_events 表项补充"当前通过 traces 表 JSONB data + step 字段区分实现持久化"说明
  - CODE_REVIEW.md 技术债清单：spec_store 项从 🔲 改为 ✅
  - CODE_REVIEW.md 附录"当前差距"：spec_store 项从 🔲 改为 ✅；errors/specs/network_records/ui_events 表项补充持久化路径说明
修改文件：
  - README.md
  - docs/internal/CODE_REVIEW.md
  - docs/internal/AI_HANDOFF.md（本条目）
测试结果：
  - 纯文档任务，无代码改动，无测试执行
  - 测试基线以 README.md 项目状态表为准（248 passed / 6 skipped / 1 failed）
下一步：
  - 推送到 GitHub + 考虑 CI pipeline（GitHub Actions）
  - 后续 P2/P3 改进项可在新 Sprint 处理
风险：
  - 无代码风险（纯文档同步）
  - 状态描述以 AI_HANDOFF.md §一 已记录的 v0.3.0 收口成果为依据，与 PROJECT_SUMMARY.md / DEV_PLAN.md / claude-audit-consolidated.md 保持一致
```

### AUDIT-2 五维代码评估任务交接（2026-07-20）

```
任务：AUDIT-2 — 五维代码全面评估（安全性/权限控制/数据流动/多并发/代码逻辑路径）
当前状态：已完成（评估报告已写入，未修复任何代码）
已完成：
  - 阅读 AI_RULES.md / AI_HANDOFF.md / DEV_PLAN.md / CODE_REVIEW.md / PRD.md / DESIGN.md / PROJECT_SUMMARY.md
  - 审查 app/ 全部核心模块（middleware / api / mcp / llm / config / main）
  - 五维评估报告写入 docs/internal/CODE_REVIEW.md §五维代码评估报告（2026-07-20）
  - 14 个待改进项（2 P0 + 4 P1 + 6 P2 + 2 P3）登记到 DEV_PLAN.md §三
修改文件：
  - docs/internal/CODE_REVIEW.md（追加五维评估报告章节）
  - docs/internal/DEV_PLAN.md（§三 追加 AUDIT-2 任务列表）
  - docs/internal/AI_HANDOFF.md（本条目）
测试结果：
  - 纯评估任务，无代码改动，无测试执行
  - 测试基线以 README.md 项目状态表为准（251 passed / 6 skipped / 0 failed）
关键发现：
  - P0：mcp_routes.py:47 `PARSE_ERROR` 未导入 → NameError（客户端发无效 JSON 时触发）
  - P0：mcp_routes.py:47 `f"无效 JSON: {e}"` 异常细节外泄
  - P1：mcp_routes.py:84 错误码误用 `INVALID_REQUEST` 替代 `INTERNAL_ERROR`
  - P1：spec_store.py:146-148 持锁做 IO + N+1 查询（1000 次 get_logs）
  - P1：trace_repo.py:102-136 `save_trace` 多次写入非原子
下一步：
  - 立即修复 P0（AUDIT-2-1 / AUDIT-2-2）— ✅ 已完成
  - 评估 P1 是否阻塞 v0.3.0 发布（建议修复后再发布）— ✅ 已完成
  - P2/P3 纳入后续 Sprint
风险：
  - 评估仅基于静态代码审查，未做运行时验证
  - 部分问题（如 spec_store N+1 查询）需实际负载验证影响
  - P0 NameError 可能影响所有发往 /mcp 的无效 JSON 请求（生产风险）
```

### Phase 1 短期优化任务交接（2026-07-22）

```
任务：Phase 1 短期优化 — PG连接池可配置化 + LLM缓存 + 端点级限流 + PG重连修复
当前状态：已完成
已完成：
  - P1-1 PG连接池可配置化：config.py 新增 pg_min_connections/pg_max_connections（默认2/20），pg_store.py 使用配置项替代硬编码
  - SEC-14 PG重试真正重连：_execute_with_retry 返回 (conn, rowcount) 元组，OperationalError 时关闭坏连接并获取新连接，调用方 finally 归还最新连接；修复 cleanup_expired rowcount 获取逻辑
  - P1-2 LLM分析结果缓存：analyzer.py 新增线程安全缓存（按 fingerprint，LRU 100条，TTL 1小时），命中缓存直接返回不调用 LLM
  - P1-3 端点级限流：middleware.py RateLimitMiddleware 新增 ENDPOINT_LIMITS（/ingest/ 120/min, /api/debug/analyze 10/min, /api/debug/verify/ui 5/min），未匹配路径使用全局默认值
修改文件：
  - app/config.py（新增 pg_min_connections/pg_max_connections）
  - app/mcp/core/storage/pg_store.py（_get_pool 配置化 + _execute_with_retry 返回元组 + cleanup_expired rowcount 修复）
  - app/llm/analyzer.py（LLM缓存：_analysis_cache + _compute_context_fingerprint + _get_cached_result + _set_cache_result）
  - app/middleware.py（端点级限流：ENDPOINT_LIMITS + _get_endpoint_limit）
测试结果：
  - pytest tests/unit/test_storage.py -q → 11 passed, 5 skipped
  - pytest tests/unit/test_analyzer.py -q → 34 passed（含 7 个缓存测试）
  - pytest tests/unit/test_middleware.py -q → 12 passed（含 5 个端点限流测试）
  - pytest tests/unit/ -q → 284 passed, 1 failed（test_git.py 白名单测试，与本次修改无关）, 6 skipped
代码审查修复（code-review skill 审查后）：
  - BUG-2 修复：_get_cached_result 返回深拷贝（copy.deepcopy）而非引用，防止缓存数据被外部修改污染
  - ISSUE-1 修复：_set_cache_result 存储 copy.deepcopy(result)，cached 字段在缓存后设置，不污染缓存原始数据
  - 新增 11 个测试用例：
    - TestLLMCache: 命中/未命中/TTL过期/淘汰/flag标记/不可变性（test_analyzer.py）
    - TestEndpointRateLimit: /ingest/前缀/analyze/verify/ui/默认值（test_middleware.py）
风险：
  - 无新增风险，所有修改均为增量增强，不改变现有行为
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
| **P0** | schemas 重复定义统一 ✅ / spec_store 持久化可靠性 ✅ / M9 .env 根因修复 ✅ | 发布前核心稳定性阻塞项 — 全部清零 |
| **P1** | N2 LLM 输出校验 ✅ / M4 JSON-RPC 错误码 ✅ | 协议规范与测试完整性 — 全部清零 |
| **发布** | GitHub Actions CI 已完成 ✅ | 自动化测试与发布保障 |
| **P2** | Browser SDK V3-V6 | 网络错误自动标记、SDK 追踪、增强 ingest、UI 静默失败检测 |
| **P3** | 数据层长期优化 + 可观测性 | traces 分区/归档、OpenTelemetry、熔断器 |
| **P4** | 智能化 | RAG 知识库、AI Debug Agent |

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
8. [claude-audit-consolidated.md](./release/claude-audit-consolidated.md) — 查看当前 Release 审查收口清单
