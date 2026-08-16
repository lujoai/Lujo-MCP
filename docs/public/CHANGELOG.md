# 变更记录（CHANGELOG）

> 本文件记录 Lujo-MCP 项目对外文档与代码的变更历史。
> 格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [Unreleased]

> v0.5.2 已发布（2026-08-15）：品牌统一 —— 全仓 `ai-debug-mcp` 标识改为 `lujo-mcp`。当前测试基线 **1105 passed / 6 skipped / 0 failed**（2026-08-16 第 3 轮审查 P2 清零 + P3 高价值 4 项收口后）。

### 修复

#### 代码

- **KB 淘汰索引泄漏**（R3-1）：`knowledge_base.py` LRU 淘汰时 `popitem` 返回的 entry 被丢弃，`_remove_from_index` 永不执行，`_norm_index`/`_type_index` 中陈旧 fingerprint 永久累积（索引无界增长 + 候选集混入已淘汰条目）；改为直接使用 `popitem` 返回值清理索引
- **非 ASCII API Key 头 500**（S3-1）：`key_rotation.py` `hmac.compare_digest` 对含非 ASCII 的 str 抛 `TypeError`，畸形 `Authorization` 头可稳定打出 500；比较前统一 encode 为 UTF-8 bytes（恒定时间语义不变）
- **JSON-RPC 非 dict 请求 500**（P3-2）：`mcp_routes.py` 合法 JSON 但非对象（`[1]` / `"abc"` / `123`）时 `parsed.get` 抛 `AttributeError` → 500，违反 JSON-RPC 规范；补 `isinstance(parsed, dict)` 校验返回 -32600 Invalid Request
- **HTTP 指标 path 失效**（R3-2）：`observability.py` 在 `call_next` 之前读取 `scope["route"]` 恒为 None，所有请求 path 归并为 "404-other"，指标失去区分度；path 计算移到 `call_next` 之后
- **路径白名单 symlink 绕过**（S3-2）：`code_locator.py` / `git.py` 白名单校验用 `abspath` 不解析符号链接，白名单根内 symlink 指向根外文件可绕过校验；统一改用 `realpath`（与 static_analyzer 一致）
- **LLM 复合键脱敏缺口**（S3-3）：`analyzer.py` `_redact_value_for_llm` 精确匹配敏感键名，`user_token` / `db_password` / `apikey` / `x-api-key` 等复合键不脱敏即发往外部 LLM；复用 `trace_repo._is_sensitive_key` 子串匹配 + 白名单策略
- **data_table 断言 NameError**（R3-6）：`ui_runner.py` 表格元素未找到时引用仅 form 分支定义的 `expected_values` → NameError 被外层 except 吞掉，返回误导性 error_type；改用 `expected_rows/expected_columns/expected_headers`
- **限流误伤结果轮询**（R3-5）：`middleware.py` `/api/debug/analyze`（10 次/分）前缀匹配误伤 `/api/debug/analyze/result/{job_id}` 轮询端点，合法客户端轮询超 10 次/分即 429；为 result 子路径单独设置 60 次/分
- **beacon scope 前缀无边界**（P3-4）：`beacon.py` `path.startswith(scope)` 会误放行 `/ingest-malicious` / `/ingestion` / `/ingestfoo` 等前缀相似但属于不同端点的路径；改为 `path == scope or path.startswith(scope + "/")`，新增 `test_prefix_boundary_not_bypassed` 覆盖三类边界
- **会话驱逐 DoS**（P3-3）：`session.py` 会话表达上限时无条件驱逐 `last_active` 最小的会话（可为活跃会话），攻击者高频建会话可把正常用户挤下线，`SessionLimitExceeded` 形同虚设；改为仅驱逐超过 TTL（1800s）的过期会话，全活跃时抛 `SessionLimitExceeded`（HTTP 层返回 503），新增 3 项驱逐策略测试
- **定时清理任务静默死亡**（R3-3）：`main.py` `periodic_cleanup` 循环体内 `get_state_store()` 在 try 之外，一次异常即导致整个清理任务永久失效（traces/sessions 过期数据不再清理）；`get_state_store()` 补兜底 try/except 跳过当期继续下一周期；停机时 `task.cancel()` 后补 `await task`（抑制 `CancelledError`），确保协程真正退出
- **stdio 阻塞读退出挂死**（R3-4）：`stdio.py` 用默认线程池 `run_in_executor(None, sys.stdin.readline)`，阻塞读无法被取消，stdin 未关闭时进程退出可能挂死；改为专用 daemon 线程读 stdin + `call_soon_threadsafe` 回投事件循环队列（EOF 哨兵退出），daemon 线程不阻止解释器退出，新增 `test_stdio_transport.py` 3 项测试
- **errors bucket 无界内存增长**（R3-7）：`errors.py` `_recent` 按用户可控 `session_id` 建 bucket（每个 ≤200 条），bucket 总数无上限，高频伪造 session 可无界撑爆内存；`_recent` 改 `OrderedDict` + `_MAX_BUCKETS=1000` LRU 淘汰最久未写入的 bucket，新增 LRU 上限测试
- **默认无鉴权暴露无告警**（S3-4）：`api_key=None`（不鉴权）+ 绑定非回环地址（如局域网 IP）时启动无任何提示，默认部署即无鉴权对外暴露而不自知；`validate_startup_configuration` 对"无 key + 非 loopback 绑定"组合打 WARNING（`0.0.0.0` 保持硬拒绝），新增 3 项告警分支测试
- **beacon 令牌内存无界增长**（P3-5）：`beacon.py` `_mem` 在 Redis 降级/单机模式下仅惰性删除过期项，从不主动清理，可无限堆积；新增 `_MAX_MEM_TOKENS=10000` 容量上限，满时先清理过期项，仍满则驱逐最接近过期的令牌，新增 2 项容量测试
- **MCP initialize 会话固定**（P3-8）：`mcp_routes.py` initialize 分支携带他人有效 `Mcp-Session-Id` 时直接复用该会话（通知流劫持面），且双重 `registry.get` 存在 TOCTOU；改为无条件 `registry.create()` 新建会话，新增会话隔离测试
- **SSE Hub 跨线程竞争**（P3-11）：`sse.py` `SSEHub._queues` 跨线程读写无锁，close_session pop 与 subscribe setdefault/append 交错存在丢订阅窗口；加 `threading.Lock` 保护字典结构，锁内复制队列列表后在锁外 `call_soon_threadsafe` 发布，新增 2 项并发竞争测试
- **/ingest/batch 批量无条数上限**（P3-6）：`ingest.py` batch 端点 events 数组仅受 10MB 解压体限制，可被滥用撑爆内存/CPU；新增 `_MAX_BATCH_EVENTS=100` 上限，超限返回 413，新增 2 项边界测试

### 变更

#### 代码

- **品牌统一（v0.5.2）**：MCP server 名 `ai-debug-mcp` → `lujo-mcp`（`app/mcp_server.py`）；全部 `logging.getLogger("ai-debug-mcp.*")` → `lujo-mcp.*`；`config.py` `otel_service_name` / `service_name` → `lujo-mcp`；`browser-sdk/`（package.json + ai-debug.js）、`mcp_config_example.json` 示例路径同步；对应测试断言更新（`test_api.py` / `test_otel.py` / `test_jsonrpc.py`）；删除本地 `.claude` 配置
- **LICENSE**：版权署名 `ai-debug-mcp` → **LujoAI**

---

## [0.5.0] — 2026-08-13

> v0.5.0 工程质量加固与 Runtime 数据契约对齐。测试基线 992 passed / 6 skipped / 0 failed。

### 新增

#### 代码

- **DebugContext Schema Alignment**：`DebugContext` Pydantic model 从 7 字段扩展至 20 字段，对齐 `build_debug_context()` 实际输出；新增字段全部 Optional + default，`model_config = {"extra": "allow"}` 支持未来扩展
- **DebugContext Runtime Integration**：`build_debug_context()` 返回类型从 `dict | None` 升级为 `DebugContext | None`；所有调用方（MCP tools / Dashboard API）通过 `.model_dump()` 适配，外部 JSON 结构不变
- **MCP Tool Category Metadata**：`tools/list` 响应为每个工具新增 `category`（agent / sdk）和 `experimental`（bool）字段；HTTP 与 stdio 传输层均支持；旧 MCP 客户端可忽略额外字段
- **Prompt Injection 防护**（P2-1）：LLM analyzer 与 Agent 层引入 `_INJECTION_GUARD` 安全边界声明 + `_wrap_evidence()` XML 标签隔离，防止 Debug Context 中的恶意指令文本诱导 LLM
- **API Schema Validation**（P2-2）：`/verify` 和 `/verify/ui` 端点从 `body: dict` 改为 Pydantic 模型（`VerifyRequest` / `VerifyUiRequest`），`extra="ignore"` 保证旧客户端兼容
- **Session 安全加固**：MCP 会话表新增 `_MAX_SESSIONS` 上限（10,000）+ LRU 驱逐 + `SessionLimitExceeded` 503 响应；`/internal/health` 端点新增内网 IP 鉴权（外网需 API Key）

#### 测试

- `tests/unit/test_debug_context_schema.py`（14 tests）— DebugContext 字段存在性、向后兼容、unknown field、`model_dump(exclude_none=True)`
- `tests/unit/test_debug_context_integration.py`（14 tests）— 返回类型验证、MCP/Dashboard JSON 结构不变、model_dump 等价性
- `tests/unit/test_tool_category_metadata.py`（17 tests）— tools/list metadata、tool name/inputSchema 不变、分类映射、experimental 标记、向后兼容

### 变更

- `app/runtime/context/builder.py`：`build_debug_context()` 返回 `DebugContext(**result)` 而非裸 dict
- `app/mcp/tools/debug_api.py`：`get_debug_context()` / `analyze_with_llm()` 适配 `.model_dump()`
- `app/api/dashboard.py`：`get_trace_detail()` / `get_trace_quality()` 适配 `.model_dump()`
- `app/mcp/protocol/server.py`：`register_tool()` 存储 category/experimental；`_handle_tools_list()` 响应包含 metadata
- `app/mcp/tools/__init__.py`：`register_all_tools()` 中 17 个工具标注 category 和 experimental
- `app/mcp_server.py`：stdio `list_tools()` 传入 category/experimental

### 兼容性

- **MCP Client**：`tools/list` 新增 `category`/`experimental` 字段，旧客户端可忽略（JSON 语义安全）
- **API Client**：`/verify` 端点 Pydantic 模型替换 dict，`extra="ignore"` 不拒绝多余字段
- **DebugContext JSON**：`model_dump()` 产出与原始 dict 等价的 20 字段结构，外部 JSON 不变
- **无 Breaking Change**：所有新增字段 Optional + default，旧数据可 validate

---

## [0.5.1] — 2026-08-15

> v0.5.1 已发布（2026-08-15）：Source Map 解析（测试基线 992 → **1087 passed / 6 skipped / 0 failed**）+ Browser SDK 增强（column 保留 + release 透传）+ deepseek provider base_url 修复 + LLM 集成 e2e 链路修复（DeepSeek key 有效后 2 项 e2e 全绿）。

### 新增

#### 代码

- **Source Map 解析（v0.5.1 主线）**：把前端 minified JS 堆栈帧还原为原始源码位置，补齐 Debug Context 前端盲区（此前 code_locator / static_analyzer / fault_localizer 三条证据链对 minified 帧全部失效）
  - **SM1 解析核心**（`app/runtime/collectors/sourcemap_resolver.py`）：纯 Python base64-VLQ 解码 mappings（零新依赖）；`SourceMapParser` 按 (line, column) 二分查询最近段；`resolve_frames()` 产出 StackFrame 兼容的还原帧（含 original 原位置与 resolved 标记）+ 源码片段（sourcesContent 优先，code_locator 白名单兑底）；LRU 解析缓存（mtime/token 指纹失效）；任何失败静默降级保留原始帧
  - **SM2 获取通道**（`sourcemap_store.py`，均默认关闭）：上传通道 `POST /api/debug/sourcemap`（进程内 TTL + LRU 容量驱逐）+ 磁盘约定通道（`SOURCEMAP_PATH_PREFIX`，路径须在白名单内防 LFI）；自动选路：显式 artifact > 上传按帧文件名 > 磁盘
  - **SM3 集成与工具**：`DebugContext` 新增 `resolved_frames` 字段（21 字段，向后兼容）；`build_debug_context()` 还原命中后 code_snippets / fault_localization / git 归因 / 相关规范均改用还原帧，exception.frames 保留 minified 原帧；新 MCP 工具 `resolve_stack`（category=agent，experimental）—— Agent 可直接调用还原堆栈；MCP 工具数 HTTP 17 / stdio 17 → **18 / 18**
  - **SM4 质量联动**：QualityScorer TRACE 维度还原加成（+0.3 封顶 1.0）+ sourcemap_resolver 证据项；Benchmark 新增 Case 6 `frontend_minified_sourcemap`（还原前/后 A/B 对照，`frontend_sourcemap_ab()`，验证还原后 Quality 评分提升——v0.4.0「Debug Context 价值可量化」目标的直接证据）
  - **Browser SDK 最小增强**（`ai-debug.js`）：`_parseStack` 保留 column（source map 精确定位必需，旧版丢弃了该值）；新增可选 `release` 配置随错误 extra 透传（空 = 不发送，向后兼容）
  - **配置项**（`app/config.py`）：`sourcemap_enabled`（默认 False）/ `sourcemap_path_prefix` / `sourcemap_upload_ttl_seconds`（3600）/ `sourcemap_max_uploads`（100）
  - **测试**：新增 94 项（`test_sourcemap_resolver.py` 43 + `test_sourcemap_store.py` 29 + `test_sourcemap_integration.py` 22），基线 992 → **1087 passed / 6 skipped / 0 failed**；工具数/字段数/Case 数断言同步更新

#### 修复

- **deepseek provider base_url 缺失**（`app/llm/analyzer.py` + `app/rag/qdrant_vector_store.py`）：`_PROVIDER_BASE_URLS` 缺少 `"deepseek"` 映射，`LLM_PROVIDER=deepseek` 时 `_resolve_base_url()` 返回空 → openai SDK 回落 OpenAI 官方端点 → DeepSeek key 必然 401，LLM 分析链不可用。已补 `https://api.deepseek.com`，并新增 `test_resolve_base_url_deepseek` 用例；实测真实调用返回结构化分析 JSON
- **`tests/unit/test_debug_context_integration.py`**：`test_analyze_with_llm_returns_dict` 增加环境隔离（monkeypatch 无 Key 快速回退）——本地 .env 若配置了不可达/无效 LLM 端点，真实 socket 连接挂起 + 重试会阻塞测试（环境依赖非代码回归）
- **LLM 集成 e2e 测试配置隔离**（`tests/integration/test_agent_repair_e2e.py`）：本地 `.env` 若打开 `AGENT_MULTI_AGENT_ENABLED` / `AGENT_VERIFY_LOOP_ENABLED`，e2e 会误走 Verify Loop（最多 3 轮 × 多 Agent），30s 轮询必然超时；fixture 显式隔离两开关走受控 Phase 1 单 Agent 链路，轮询超时对齐 `agent_timeout`（90s）。DeepSeek key 有效后 2 项真实 e2e 全绿（此前无 key 时 skip）
- **git 子进程输出编码**（`app/runtime/core/git.py`）：Windows 上 `subprocess.run(text=True)` 默认按本地 gbk 解码 git 的 UTF-8 输出会抛 `UnicodeDecodeError`，导致 diff/blame 静默失败；显式 `encoding="utf-8"` + `errors="replace"` 兜底非法字节

#### 环境备注（非代码变更）

- 本地 venv 曾缺失 `pytest-asyncio` / `qdrant-client` / `opentelemetry-*` / `pybreaker`（与 requirements-dev.txt 漂移），导致 13+ 项环境性失败；已按 `pip install -r requirements-dev.txt` 补齐，全量回归恢复全绿

- **MCP Debug Context 可观测性**（Phase 3 D5，2026-08-11）：`app/mcp/observability.py` 新增 `DebugContextTrace`（记录 request_id / Runtime Context 可用性与大小 / Debug Experience 开关与命中数 / Context 构建耗时 / Tool 响应耗时）+ `observe_context` / `attach_metadata`；context/debug/stacktrace 工具成功分支注入可选 `metadata` 字段（向后兼容）；stdio+HTTP 传输层记录 tool 响应耗时/大小（仅日志，不打印敏感负载）
- **Benchmark 框架**（Phase 3 D6，2026-08-11）：`benchmark/` 新增 `schemas.py`（`BenchmarkCase` / `EvaluationMetrics`）+ `cases.py`（5 个手写 fixture：api_500 / frontend_blank / db_error / auth_403 / perf_slow）+ `runner.py`（CLI：list / show / quality 旁证）；验证 MCP Debug Context 是否提升外部 AI Debug 能力（与 QualityScorer 两个体系分离）
- **MCP stdio 冒烟验证脚本**（Phase 3 D7）：`scripts/mcp_smoke_test.py` —— 验证 stdio 启动 → initialize 握手 → tools/list 枚举 → 工具调用往返；`app/mcp_server.py` 的 `Server(...)` 传入 `version=__version__` 对齐 serverInfo 版本

- **Quality System 核心框架**（`app/quality/`）
  - `schemas.py`：`QualityReport` / `ContextCompleteness` / `AnalysisConfidence` / `EvidenceItem` / `DimensionScore` 数据模型
  - `scorer.py`：规则引擎 `QualityScorer.evaluate()`——9 维度加权评分 + 证据提取 + 可信度评分 + 改进建议，纯函数 + 静默降级
  - `__init__.py`：包导出
- **配置项**（`app/config.py`）：`quality_scoring_enabled` / `agent_iterative_repair_enabled` / `agent_max_iterations` / KB 三级 fallback 开关 / Agent Verify Loop 开关
- **Context Assembler 质量注入**（`app/agent/context_assembler.py`）：`assemble()` 返回新增 `quality_report` 字段，feature flag 控制，失败静默降级
- **LLM 分析增强**（`app/llm/analyzer.py`）：SYSTEM_PROMPT 新增 `reasoning_chain` + `evidence_items`；`_validate_and_normalize` 向后兼容旧格式
- **Dashboard 质量报告**（`app/api/dashboard.py` + `app/web/dashboard.html`）
  - `GET /api/dashboard/trace/{tid}/quality` 独立端点
  - `get_trace_detail` 注入 `quality_report` 字段
  - 前端 Quality 卡片：综合评分进度条 + 9 维度网格 + 证据列表 + 改进建议
- **StaticAnalyzer**（`app/runtime/collectors/static_analyzer.py`）：基于 Python `ast` 标准库的函数级静态分析，提取函数签名/参数/类型注解/内部调用/复杂度/可疑输入（M3 Task 12，零外部依赖）
- **DebugCase 标准 Schema**（`app/rag/debug_case.py`）：异常调试案例结构化记录 + 三级指纹计算（归一化消息 / 类型指纹），M2 引入
- **知识库三级 fallback 匹配**（`app/rag/knowledge_base.py`）：L1 精确指纹 → L1.5 归一化指纹 → L2 类型级 Jaccard；向量索引双写同步（M2）
- **种子知识库**（`app/rag/seed_data.py`）：30 条覆盖常见异常的种子案例，启动时加载（M2）
- **URL Resolver**（`app/runtime/collectors/url_resolver.py`）：无堆栈场景下按 HTTP 方法+路径反查 FastAPI 路由表定位 handler 源码（M3）
- **无堆栈静态分析**（`app/runtime/context/builder.py`）：静默失败无异常堆栈时，基于网络请求反查 handler 并做函数级静态分析，注入 `static_analysis` 字段（M3）
- **Agent Verify Loop**（`app/agent/verify_loop.py`）：迭代修复闭环——三层开关（agent→multi→verify）+ 四级判定（high_confidence/passed/partial/failed）+ 验证通过后 KB 写回（M4）
- **KB 验证写回**（`app/rag/knowledge_base.py`）：`record_verification()` 递增 `verify_count` / 提升 `case_confidence`，写入后同步向量库（M4）
- **测试**（`tests/unit/test_quality.py`）：86 个用例覆盖 19 个测试类；`tests/unit/test_dashboard.py` 新增 6 个质量报告测试用例；`tests/unit/test_url_resolver.py`（M3）、`tests/unit/test_verify_loop.py`（M4）
- **npm 开箱即用分发（2026-08-09）**：PyInstaller 单文件打包 + npm 元包 + 平台二进制包
  - `packaging/lujo-mcp-server.spec` + `packaging/entry_stdio.py`：PyInstaller 单文件二进制打包（修复 `__file__` NameError、补充 hiddenimports、Windows 启用 UPX）
  - `npm/packages/lujo-mcp`（元包）+ `bin/cli.js` / `bin/check.js` / `scripts/check-clean-bin.js`：`npm install -g @lujoai/lujo-mcp` 开箱即用，按系统自动安装对应平台二进制
  - 平台包 `lujo-mcp-win32-x64` / `lujo-mcp-linux-x64` / `lujo-mcp-osx-arm64`（3 平台，optionalDependencies 自动选择）
  - `.github/workflows/release-npm.yml`：GitHub Actions 矩阵构建（Windows/Linux/macOS 并行 PyInstaller 打包）+ 自动发布 npm（先平台包后元包）
  - `npm/scripts/gen-platform-packages.js`：一键生成平台包 `package.json` 骨架
- **测试补齐（2026-08-11）**：新增 `tests/unit/test_stacktrace_api.py`（9 用例，`stacktrace` MCP 工具 handler/`get_stacktrace` 各分支与边界）+ `tests/unit/test_factory.py`（8 用例，存储工厂后端校验 fail-fast、error/spec no-op、async 混合 fail-fast、PG 失败 fallback 与 fail-fast 双路径）；测试基线 891 → 908

#### 文档

- **PRD.md §12.2**：v0.4.0 路线图——Milestone 概览 + M1 评分基线（5 场景对比）+ M2-M4 评分提升预期 + 各 Milestone 贡献分解
- **DESIGN.md §19**：v0.4.0 架构评审决策（§19.1-19.6）
  - §19.1 项目当前状态评估（Beta 偏 Demo 判定）
  - §19.2 Quality System 评分模型设计（9 维度权重 + 模块结构 + 设计约束）
  - §19.3 M1 评分基线（5 场景对比 + 基线分析要点）
  - §19.4 M2-M4 改进逻辑与评分推演（逐场景维度变化 + 综合评分推演汇总）
  - §19.5 架构稳定性约束（6 个禁止大改模块）
  - §19.6 v0.4.0 明确不做（7 项）
- **README.md**：新增「方式零：npm 全局安装（开箱即用）」章节（`npm install -g @lujoai/lujo-mcp` + MCP 客户端配置示例）
- **npm/README.md**：新增 npm 分发说明（发布结构、用户使用、发布流程、CI 自动构建 + 发布、token 配置）

### 修复

#### 代码

- **CODE_REVIEW_FIX_PROMPT 代码审查修复（2026-08-08，commit `8089525`）**：按内部代码审查修复清单修复 P0×5 + P1-A×6 + P1-B×2 + P1-9×9 + P1-10×3 + P2 全部项 + 2 追加（回归测试），另修 2 个审查中发现 bug
  - **P0 崩溃/安全漏洞（5）**：
    - `debug.py` 补 `import time`（`/api/debug/session`、`/api/debug/health` 端点必然 500）
    - `static_analyzer._resolve_path` LFI：`realpath` 归一化 + 允许前缀白名单校验，拒绝返回 None
    - `ui_runner` SSRF 重定向绕过：导航前固定解析 IP + 逐跳校验，私网/回环/链路本地拒绝
    - `dashboard.html` 存储型 XSS：`esc()` 补引号转义 + 事件委托去内联 onclick + `main.py` dashboard 响应加 CSP 头
    - DDL 双源分叉：抽取 `app/runtime/core/storage/ddl.py` 共享 DDL 常量，pg_store / async_pg_store / migrations 三处一致（`test_ddl_consistency.py` 断言列一致性）
  - **P1-A 数据丢失/静默失败链（6）**：
    - SDK 离线重试数据全丢：`_restorePendingBatches` 展开 `parsed.events` 逐个入队 + 坏数据 `localStorage.removeItem`
    - beacon 压缩必然失败：beacon 分支不压缩以原始 JSON 发送，fetch 分支保留 gzip
    - `repair_queue` / `analysis_queue`：drain 超时残留标记 rejected + worker 取消时标记 in-flight + `_jobs` 加 TTL 清理
    - `pg_async_enabled` 混合行为 fail-fast：启动期校验同步/异步调用链一致性
    - 启动鉴权校验与中间件语义统一（`API_KEYS` 有效 key 非空即已鉴权）
    - `redact()` 递归脱敏覆盖全部存储边界（复用 `_redact_nested`）
  - **P1-B 安全（2）**：`mcp_routes` RBAC 默认角色 fail-closed；analyzer 上下文指纹去 request_id（error-surface 指纹，缓存命中率恢复）
  - **P1-9 正确性（9）**：fault_localizer 帧索引错位（按原始 index 关联）；scorer RUNTIME 维度嵌套键对齐；分区表检测跳过普通表并 warning；**PG 池耗尽超时 + 修复 `_get_conn` 无限递归 bug**（`return _get_conn()` → `pool.getconn()`）；errors 同指纹节流调度（2 秒窗口 + 10000 条上限）；verify_loop 单轮超时 watchdog + 迭代语义；coordinator `dag_degraded` 计入 repair 失败 + warning 日志；stdio 畸形输入捕获 `UnicodeDecodeError`/`RecursionError` → PARSE_ERROR；params 非 dict → -32602
  - **P1-10 资源上限（3）**：MCP SSE 每订阅有界队列（maxsize=256 丢最旧）；observability 指标 key 归一化（未命中路由统一 "404-other" + `_MAX_METRIC_KEYS=5000` 上限）；state.store 限流键驱逐（`_timestamps` 同步驱逐 + `incr_float` 触发）
  - **P2（简洁项）**：spec_store 缓存刷新跳过 .venv、LIKE 参数转义、`delete` 先查 PG、`get` 回源比对；ui_runner `browser.close()` 移入 finally；assert_engine 值类型归一 / 带点字段路径 / `expected=None` 语义；死配置收敛（移除 `cb_llm_window_size`/`cb_pg_window_size`/`qdrant_connect_timeout`，接入 `agent_dag_parallel_timeout`/`debug_experience_min_score` 默认 0.0）；版本号对齐 `0.4.0-beta`；Dockerfile `USER` 非 root + `requirements-locked.txt`
  - **额外 bug（2）**：P1-7 RBAC 语义回归（`role is None` 时按 `rbac_enabled` 判定，统一 rbac.py fail-closed 语义）；`test_mcp_verify_ui.py` `_FakeBrowser` 补 `new_context`/`route` mock（ui_runner 标准 API）
  - **回归测试（追加 2）**：新增 `tests/unit/test_state_store.py`（4 用例）/ `test_ddl_consistency.py`（2 用例）/ `test_debug_endpoints.py`（3 用例）+ 扩充 `test_jsonrpc` / `test_otel` / `test_sse_hub` / `test_static_analyzer` / `test_url_resolver` 用例
  - **验证**：`pytest tests/unit/` = **891 passed / 6 skipped / 0 failed**（零回归）
- **`tests/unit/test_static_analyzer.py`**：移除已删除的 `analyze_source_code` / 旧版 `analyze_handler(module_path=...)` API 用例，仅保留当前 `analyze()` 堆栈帧分析用例（无堆栈入口由 `test_url_resolver.py` 覆盖），修正合入 main 后的测试回归（M5）
- **`tests/unit/test_security_agent_severity.py`**：`VALID_SEVERITY` 不含 `unknown`（其为哨兵值），改为断言无效值映射为 `unknown`，修正合入 main 后的测试回归（M5）

#### 文档

- **PRD.md**：修订记录 v5.6 中的 README.md 链接 `../README.md` → `../../README.md`（路径修正）
- **DESIGN.md**：3 处 `§6.1` 死链修复为 `§6`（§6 无子章节）

### 变更

#### 文档

- **PRD.md**：修订记录新增 v5.6（v0.4.0 开发路线制定 + M1 Quality Foundation 交付）
- **PRD.md**：修订记录新增 v5.8（M5 全量回归 + 文档同步交付）；产品版本 v0.3.0 → v0.4.0；M5 Milestone 状态更新为已完成
- **CHANGELOG.md**：测试基线更新为 M5 全量回归结果（单元 792 + e2e 10）
- **CHANGELOG.md**：测试基线更新为 CODE_REVIEW_FIX_PROMPT 修复后全量回归结果（单元 891 + e2e 10）

> 测试基线：单元 927 passed / 6 skipped / 0 failed（含 CODE_REVIEW_FIX_PROMPT 修复与回归测试 + stacktrace 工具与存储工厂边界测试 17 项 + D5 MCP 可观测性 16 项 + D6 Benchmark 框架 19 项，不含依赖真实 LLM 的 `coordinator` 用例）+ e2e 10 passed（需启动 uvicorn 服务器）。`test_coordinator.py`、`test_agent_repair_e2e.py` 依赖有效 API Key，无 Key 时 skip，属环境依赖非代码回归。

---

## [v0.3.0] - 2026-07-30

### 新增

- Dashboard 实时 SSE 推送（`DASH-SSE-001`）：`DashboardEventBus` 广播总线 + `GET /api/dashboard/stream` SSE 端点 + 前端 EventSource 集成
- FR20 Dashboard 实时 SSE 推送功能需求

> 测试基线：654 passed, 6 skipped, 0 failed

---

## [v0.2.0] - 2026-07-25

### 新增

- 三轨并行交付：异步分析队列 + 向量检索 RAG（in-process + Qdrant）+ RBAC + API Key 轮换
- Browser SDK V3/V6（网络错误自动标记、UI 静默失败自动检测）
- 指纹知识库基础能力（命中优先 + 自动沉淀）
- Phase 5 数据层长期优化（分区、归档、批量写入、降级、熔断器）

> 测试基线：520 passed, 6 skipped, 0 failed

---

## [v0.1.0] - 2026-07-08

### 新增

- 项目首版发布
- 8 个 Phase 全部落地：trace_repo / network / ui_event / git / silent_failure / ingest_error / build_debug_context / redaction
- FR13 assert_engine + verify / FR14 Playwright UI 遍历 + verify_ui / FR15 spec_store + 闭环
- 多 LLM provider 支持（openai / zhipu / custom）
- Web 控制台 Dashboard
- 17 个 MCP 工具双传输注册（stdio + HTTP）

> 测试基线：369 passed, 6 skipped, 0 failed
