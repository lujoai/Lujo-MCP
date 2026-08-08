# 修复提示词：交给 Trae 执行代码审查修复

> 使用方式：将两个代码块（`# 任务说明` + `# 审查发现清单`）整体复制到 Trae 对话框（或新建会话后粘贴）。

---

# 任务说明

你是资深后端工程师。针对 Lujo-MCP（基于 MCP 协议的 AI 智能调试服务，Python + FastAPI）执行一次代码审查修复任务。

## 硬性要求

1. **只修复下面清单列出的问题**，禁止重构、禁止"顺手优化"未列出的代码，保持向后兼容。
2. 修复遵循项目现有代码风格（中文注释、`# ── 分区标题 ──` 风格、已有工具函数优先复用）。
3. 安全边界修复必须 fail-closed：拿不准时拒绝而非放行。
4. 每修完一组问题后，运行 `python -m pytest tests/ -k <相关测试> --tb=short -q` 验证；全部完成后再运行一次 `python -m pytest tests/unit/ --tb=short -q` 确认无回归。
5. 每处修复在代码里用 `# FIX: <编号> <原因>` 注释标注，方便事后核对。
6. 修复完输出清单：每个问题 → 改了哪些文件 / 是否完成 / 测试结果。
7. 不要修改 `.env`、不要提交 git。

---

## 项目背景

- app/ —— 服务端（FastAPI + MCP 双传输；五层架构：transport / middleware / router / engine / storage）
- browser-sdk/ai-debug.js —— 浏览器采集 SDK（V2-V6）
- migrations/ —— SQL 迁移（注意：已有基线与代码 DDL 分叉，见 P0-5）
- tests/ —— pytest（单元 + 集成 + e2e），配置见 pytest.ini
- 环境变量与配置项在 app/config.py（Settings 类，全局单例 `settings`）
- 依赖方向已冻结：Agent → RAG 可用，Runtime → RAG/Agent/LLM/MCP 禁止

## 修复前必读（避免修坏）：

- `app/config.py` 是配置唯一来源，新增可配置项要加到这里并给默认值（默认值须保持现状行为）
- 存储层有 memory / postgresql / asyncpg 三后端 + 工厂（app/runtime/core/storage/factory.py），修复要三处同步考虑（pg_store.py 与 async_pg_store.py 的 DDL/SQL 是双份维护，改一处必须改另一处）——**最好先把 DDL 抽成共享常量**
- 中间件顺序不许乱动（Auth 是最外层，NetworkCapture 在最内层是设计的）
- 脱敏入口 app/runtime/core/redaction.py 的 redact() 只处理 str，调用方需要递归脱敏时用 trace_repo 里的 `_redact_nested` 风格
- 测试基线：874 passed / 6 skipped（完整环境），修复不得引入失败

---

## 审查发现清单（按优先级，含文件:行号与修复方向）

### P0 —— 崩溃 / 安全漏洞 / 数据丢失（必须全部完成）

**P0-1 `app/api/debug.py:311,359` —— 未导入 time，端点必然 500**
`/api/debug/session`（list_sessions）与 `/api/debug/health`（debug_health）调用 `time.time()`，但文件顶部没有 `import time`（全文件 import 只有 logging/fastapi/json 等）。补 `import time`。

**P0-2 `app/runtime/collectors/static_analyzer.py:194-217` 任意文件读取（LFI）**
`_resolve_path` 对帧里的 file 直接 `isfile` + `open`，没有任何路径白名单：
- `source_path_map` 分支：`local + file_path[len(remote):]` 未做 `abspath` 归一化，`/app/../../etc/passwd` 可穿越
- CWD 分支：`os.path.join(os.getcwd(), file_path)` 同样可读取任意绝对路径/`../../`
修复：先 `os.path.realpath` 归一化，再校验结果必须位于允许前缀内（复用 `app/runtime/core/code_locator.py` 中已有的白名单逻辑或 `settings.whitelist_path_prefix`，为空时默认收敛到项目根 `app/config.py` 的 `_PROJECT_ROOT`），拒绝则返回 None。

**P0-3 `app/runtime/verifier/ui_runner.py`（inspect_url_security ~90-125 行，navigate ~223 行）SSRF 重定向绕过**
`inspect_url_security` 只对初始 URL 做一次 getaddrinfo 校验。修复：
a) 校验与导航之间的 TOCTOU/DNS rebinding：导航前复解析并固定 IP
b) 重定向：Playwright goto 前禁用重定向（如 `max_redirects=0`），或在每跳手动校验解析结果，任一跳到私网/回环/链路本地（169.254.x/127.x/10.x/192.168.x 及 IPv6 等价）即拒绝
保持 ML 时无法预校验的兜底拒绝逻辑（allowlist/allow_private 配置语义不变）。

**P0-4 `app/web/dashboard.html`（innerHTML 拼接，139/141/144/176 行附近）存储型 XSS**
`onclick="selectTrace('${t.trace_id}')"`、`data-tid="${t.trace_id}"`、`${t.trace_kind}`、exception frames 的 `${f.file}:${f.line} in ${f.function}` 均未转义，trace_id/文件名/函数名由客户端可控（经 /ingest/error）。
修复：
a) 补 HTML 转义函数（现有 `esc()` 是否覆盖单引号/双引号，不足则补）
b) 去掉内联 onclick，改用 `data-tid` + `addEventListener` 事件委托
c) frame 的 file/function 输出过 esc()
d) main.py 的 dashboard 响应（`/dashboard`、`/demo`、`/demo/silent-failure`、`/ai-debug.js`）加 `Content-Security-Policy` 响应头（最小允许：default-src 'self'，inline style 可放 style-src 'unsafe-inline' 若页面需要）

**P0-5 migrations/ 与代码 DDL 分叉（PG 后端 errors/specs 静默失效）**
- migrate `20260711_create_errors_table.sql`：列（trace_id/exception_type/message/stack/file/line/fingerprint/occurrence_count/created_at/updated_at + created_at TIMESTAMP）与代码 `app/runtime/core/storage/pg_store.py:133-151`（`DDL_ERRORS`：error_id/frames/frame_count/traceback/source/session_id/first_seen/last_seen + `uq_errors_fp_session(fingerprint, session_id)` 唯一索引）完全不一致
- migrate `20260711_create_specs_table.sql`：`created_at/updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP`，代码（pg_store.py:162-163 与 async_pg_store.py:100-101）用 `DOUBLE PRECISION` 且写入 epoch 浮点
修复：
a) 把 `pg_store.py` 里的 `DDL_TRACES`/`DDL_SESSIONS`/`DDL_ERRORS`/`DDL_SPECS`/`DDL_ARCHIVE` 常量（以及 async_pg_store.py 同名常量）抽取到共享模块（如 `app/runtime/core/storage/ddl.py`），两处 import 同一份，消除双源分叉
b) 用同一 DDL 重写两个迁移文件（保证 init_db.sh 建的库和代码自建一致）
c) 对已按旧 schema 建库的环境：迁移里补 ALTER / 兼容处理（或文档说明 drop 重建），不得静默失败

### P1-A 数据丢失 / 静默失败链（全部修复）

**P1-1 `browser-sdk/ai-debug.js`（`_saveToLocalStorage` ~479-492 行与 `_restorePendingBatches` ~494-524 行）离线重试数据全丢**
`_saveToLocalStorage` 存的是整包字符串 `{"events":[...]}`；`_restorePendingBatches` 把 `JSON.parse(stored)` 结果当单元素 `push` 进 `_batchQueue`（514 行），flush 时二次包裹 `{"events":[{"events":[...]}]}`，后端逐条取 event.path 为空 → 全丢。
修复：恢复时展开：`parsed.events` 缺省为空数组，逐个 `_batchQueue.push({path: ..., payload: ...})`；解析失败的坏数据要 `localStorage.removeItem` 清掉，不能留死循环。

**P1-2 `browser-sdk/ai-debug.js`（`_compressAndSend` ~320-337 行）beacon 压缩必然失败，页面关闭丢数据**
sendBeacon 无法设置 `Content-Encoding: gzip`（Blob type 只能是 MIME），后端只在 `content-encoding: gzip` 时解压（`app/api/ingest.py:213-217`）→ gzip 字节按 JSON 解析 400，且 sendBeacon 返回 true 时 SDK 认为成功。
修复：beacon 分支不压缩、以原始 JSON 发送（页面关闭场景数据量小可接受）；fetch 分支保留压缩。

**P1-3 `app/agent/repair_queue.py`（drain ~127-141 行、_jobs 记账）队列残留与状态泄漏**
- drain 超时取消 workers 后队列中残留 item 不清：此后 enqueue 的 job 永不消费、永远 pending —— 修复：drain 超时路径把队列 item 标记 rejected，并让 worker 在收到 `CancelledError` 时先把 in-flight job 标记 cancelled/failed 再 re-raise（修正 `_jobs` 永久卡 running）
- `_jobs` 只增不减：加 TTL/上限清理（如 enqueue 时清理 done/failed 超过 N 分钟的记录），注意与 `app/llm/analysis_queue.py` 同样问题（`_jobs` 无清理）一起处理

**P1-4 `app/runtime/core/storage/factory.py`（+ `app/runtime/core/logs.py`、`errors.py`、`spec_store.py`）pg_async_enabled=True 时混合行为**
开启 `pg_async_enabled` 后 factory 返回 async store，但 `logs.add_log` 等同步调用方拿到 coroutine 不 await 静默丢数据；errors.py/spec_store 又主动跳过 PG 走内存。
修复（选一，推荐 a）：
a) 开启 pg_async_enabled 时对同步调用链 fail-fast：启动期（main.py lifespan 或 factory 首次调用）校验所有同步 store 用户必须走 async 版本，否则抛配置错误
b) 给同步调用方提供 to_thread 桥（如 add_log 内建内部 async 洞 或 asyncio.run 一次一请求）——注意事件循环冲突，慎用
修复后行为必须一致：要么全链路用 async、要么全链路降级并告警，不得"部分丢部分写"。

**P1-5 `app/main.py`（lifespan：启动期 `get_trace_store()/get_session_store()` 校验）与 `api_keys` 配置语义**
`validate_startup_configuration` 只查 `settings.api_key`，只配 `API_KEYS`（多 key）未配 API_KEY 的合法部署被误拒（提示 "API_KEY is empty"）；而中间件用 `auth_enabled()`（含 api_keys）。修复：启动校验改用与中间件一致的判定（API_KEYS 解析出的有效 key 列表非空即视为已鉴权），两端语义统一。

**P1-6 `app/runtime/core/redaction.py`（redact 只对 str）+ 各存储边界**
`redact()` 对非 str 原样返回（90-105 行）。`save_ui_event` 的 payload_json、`save_network_record` 的 request/response body、以及 errors/specs 相关 save 的 `extra` 字段为 dict/list 时完全不脱敏。
修复：在存储边界统一递归脱敏（对 dict/list 逐层应用 redact 到 key 和 string 叶值，参考 trace_repo.py 里已有的 `_redact_nested` 实现并复用它/抽取公共函数），覆盖 save_ui_event、save_network_record、save_trace 之外的所有其余 save 函数的 extra/嵌套字段。

### P1-B 安全（修复）

**P1-7 `app/api/mcp_routes.py:99` RBAC 默认角色 fail-open**
`role = getattr(request.state, "role", "admin")`：rbac_enabled=True 但鉴权未启用时默认 admin（全权）；`app/auth/rbac.py:76` 的 `require_role` 同场景默认 viewer（fail-closed）。
修复：默认改为 "viewer"（或直接拒绝），保证两条路径 fail-closed 语义一致。

**P1-8 `app/llm/analyzer.py:582-584` 上下文指纹含 request_id → 缓存命中率趋近 0**
换成 error-surface 指纹为主键（如 error_fingerprint/message+key frames file+line+function 组合）；hash 计算前注意去除 request_id 字段。前提：现有缓存淘汰与 TTL 语义不变；补充说明并发防抖非本次要求（可记录 TODO）。

### P1-9 正确性 / 回归修复

**P1-9a `app/runtime/context/fault_localizer.py:210-222` 帧索引错位**
`static_analyzer.analyze(frames)` 会因文件不存在/语法错误等跳过部分帧，返回结果少于 frames；`static_by_index` 用结果下标映射全量帧 → 张冠李戴。
修复：`static_analyzer.analyze` 改为返回 `(原始索引, FaultLocation)` 对，或返回与输入 frames 同长的稀疏列表（缺失为 None），fault_localizer 按原始 index 关联。

**P1-9b `app/quality/scorer.py:157-162,324-337` 评分键与真实快照结构不符**
`_scoreRuntime` 取 `runtime.get("pid")`/`cpu_percent`/`memory_mb`/`thread_count`，真实快照是 `runtime.process.pid` / `runtime.system.cpu_percent` 等（`app/runtime/collectors/runtime.py:95-102`）→ RUNTIME 维度恒 0 分。
修复：对齐嵌套键（读 `runtime.process`/`runtime.system` 结构），或固定读取函数，评分与 evidence 同时修。

**P1-9c `app/runtime/core/storage/pg_store.py:300-340` 与 `async_pg_store.py:328-359` 分区 + 已有普通表 → 每次启动崩**
注释说"已存在普通表不转换保持原状"，但 `_ensure_partitions` 无条件 `CREATE TABLE traces_YYYY_MM PARTITION OF traces`，普通表必然报 "not partitioned"。
修复：检测 `pg_partition_enabled` 且 traces 表已存在且不是分区表时，跳过建分区并 logger.warning（或按配置执行 ALTER 转换，二选一，注释写清行为）。

**P1-9d `app/runtime/core/storage/pg_store.py`（`_conn`/`getconn` 附近 ~493）池耗尽无超时**
psycopg2 `ThreadedConnectionPool.getconn()` 无超时参数，全占用时永久阻塞 + `_execute_with_retry` 重试还会再拿连接加剧。
修复：包一层带超时的获取（如 `asyncio.timeout`/线程内 `queue.get(timeout)`），超时抛可识别错误并计入熔断/降级路径（storage_fallback_to_memory）。

**P1-9e `app/runtime/core/errors.py:63-80` 每错误一线程无上限**
修复：合并/去重调度（同 fingerprint 节流窗口 N 秒内只入队一次，或单消费者队列 + 有界队列），压力下丢新条目也要打日志，禁止线程无界创建。

**P1-9f `app/agent/verify_loop.py:124-141,156-166` 迭代语义与超时**
- `round_timeout=0` 默认不设单轮超时：实际最差 ≈ 3 轮 × (repair 90s + 3 并行各 90s)
- "未通过 → 注入下一轮"但 `RepairAgent._build_messages`（repair_agent.py:130-143）不读上一轮 `repair_plan`，每轮全新生成，不收敛
修复：
a) `round_timeout=0` 时默认取 `agent_timeout`（或按整体 agent_verify_loop_max_iterations 折算），实现 watchdog
b) 若按需求仍要做真迭代：把上一轮 repair_plan/审查结果拼进下一轮 prompt（保留字段命名与 DTO 结构）；否则把 docstring 与"迭代收敛"文案改为"最多 N 次独立尝试"

**P1-9g `app/agent/coordinator.py`（~190 dag_degraded 语义）**
- RepairAgent 失败时 test/security 为 SKIPPED 不计入 parallel_failures → 整个 DAG 失效却被报告健康
- 该标志无任何消费方、无日志/指标
修复：degraded 判定把 repair 层失败也计入；coordinator 在 degraded 时打印 warning 日志（包含 skipped/failed 计数）。

**P1-9h `app/mcp/transports/stdio.py:70-81` 畸形输入杀进程**
`json.loads` 遇孤立代理项（`\ud800`）抛 UnicodeDecodeError 不在 except 列表（只捕获 JSONParseError/InvalidRequestError）→ 逃逸到外层 finally 退出整个服务。
修复：except 加 `(UnicodeDecodeError, RecursionError)` → 返回 PARSE_ERROR。

**P1-9i `app/mcp/protocol/server.py:101-103` 与 `app/api/mcp_routes.py:93` params 非 dict → 500**
`params.get` 直接 AttributeError。修复：解析层统一校验 `isinstance(params, dict)`，否则 make_error(-32602, "Invalid params")+（HTTP 路径同防控）。

### P1-10 资源上限（修复）

**P1-10a `app/mcp/transports/sse.py:37` 无界 Queue**
参考 `app/api/dashboard_events.py:49`（maxsize=256 + 丢最旧）做法，给 MCP SSE 每订阅队列加 maxsize 与丢旧策略。

**P1-10b `app/observability.py:27-30` 指标 key 无界**
`(method, path, status)` 中 path 读取的是完整 URL（含用户可控动态路径）。修复：正常归一化匹配已注册 route（未命中统一 "404-other"），并给指标表加上限裁剪。

**P1-10c `app/state/store.py`（~74-95） 限流窗口 key 永不驱逐**
`_evict_if_needed` 只遍历 `_data` 的 key，`allow()` 滑动窗口时间戳 key 不入 `_data` → 高基数限流键无限增长。修复：驱逐逻辑同时覆盖 `_timestamps`；`incr_float` 也触发驱逐。

### P2 需修复（简洁项）
- `spec_store.py:185-194`：缓存刷新每次全树遍历 .venv/node_modules，先跳过目录再 feature 文件、或改用 mtime 快照缓存
- `pg_store.py:860-874` / `async_pg_store.py:727-739`：list_specs 的 LIKE 参数 `%`/`_` 未转义 → `escape()` 或参数化结合 `ESCAPE`
- `spec_store.delete`（spec_store.py:264-285）：先查 PG 再判 exists，否则 PG 侧记录永远删不掉
- `code_locator`/`spec_store.py:177-186`：get() 内存优先永不回源，进程重启后旧值残留，改为回源比对
- `ui_runner.py` 215-294：browser.close() 移入 finally（含超时/异常路径）
- `assert_engine.py：58-72,124-140：值类型归一（int/str 转字符串或数字比较）、支持带.` 的字段路径 find、`expected=None` 语义明确
- 配置死项收敛：`cb_llm_window_size`、`cb_pg_window_size`、`qdrant_connect_timeout`、`agent_dag_parallel_timeout`、`debug_experience_min_score` —— 要么接上实现，要么从 config.py 移除并文档说明（接上的优先）
- `app/__init__.py` 版本号与 README/RELEASE 对齐（当前 0.3.0 vs 声称 v0.4.0-beta）
- Dockerfile 增加 `USER` 非 root 运行、改为安装锁定的 `requirements-locked.txt`

### 2 追加（可选，若时间允许）
- 测试补强：为 P0/P1 中的每个修复点补一条回归测试（至少：debug.py 端点 200、static_analyzer 路径白名单拒 `../`、mcp_routes params 非 dict → -32602、stdio 畸形代理项 → PARSE_ERROR、sse 超长订阅) 、redaction 递归脱敏 dict、dashboard 转义、migrations 与 DDL 一致性断言）

## 验收清单（全部达成才算完成）

1. P0 五项全部修复并有测试证明
2. P1 数据丢失组（SDK 2 条、repair/analysis queue、asyncpg 同步链、KB 索引、redact 递归）全部修复
3. `python -m pytest tests/unit/ --tb=short -q` 全绿（环境允许的 integration 若有损坏需说明原因）
4. 无新增 dead config（新增配置项均有消费点）
5. 输出完整修复清单（问题 → 文件 → 做法 → 验证结果）