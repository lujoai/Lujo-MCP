# 变更记录（CHANGELOG）

> 本文件记录 Lujo-MCP 项目对外文档与代码的变更历史。
> 格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [Unreleased]

> v0.7.1 Minor 债务清理·批次1（正确性/健壮性 10 项，全部来自第 6/7 轮审查 Minor 索引）。零回归铁律：全部既有路径行为不变，仅修复此前出错/失真/盲区的路径。

### 🔒 修复（Minor 批次1：正确性/健壮性 10 项）

- **verify_loop 阈值跨字段校验（fail-fast）**：误配 `agent_verify_loop_partial_threshold >= pass_threshold` 时，安全门钳制（未过安全门的分数压到 partial 阈值）后仍 >= pass 阈值——安全门反向失效，含 high 风险的方案照样 PASSED；`pass > high_confidence` 时高置信档永不可达。新增 pydantic `model_validator` 校验 `partial < pass <= high_confidence`，启动即拒绝非法配置。
- **verify_loop 超时存根形状对齐**：单轮超时降级存根此前仅 `{"repair_plan": None}`（真实 DAG 聚合结果有 10 个键），下游消费方读 `test_plan`/`security_review` 等键从「超时缺省」突变为 KeyError；存根补齐 `Coordinator._run_dag` 聚合的完整键集（超时轮无产出，其余键按失败降级语义置空）。
- **`parse_llm_json` 非字符串输入防御**：`parse_llm_json(None)` 此前经 `extract_json(None).strip()` 抛 AttributeError 逃逸出契约；非字符串一律返回 `(None, False)` 按解析失败处理。
- **MCP 同步池拒绝日志口径**：槽位打满拒绝日志此前误打配置超时 `busy_timeout`，与 async 分支（打实际等待时长）口径不一致、误导排障；统一为实际等待 `wait_sec`。
- **MCP async 工具排队耗时指标盲区**：async 工具成功取得槽位后补记 `record_mcp_tool_wait`，与同步分支口径一致（此前 async 排队耗时无指标）。
- **DELETE /mcp 缺 `Mcp-Session-Id` 返回 400**：此前静默 204，调用方无法区分「无会话可删」与「删除成功」；按 MCP 传输规范补语义化拒绝。
- **`/analyze/stream` build_context 异常保护**：与兄弟端点 `/analyze` 同构包 try/except，畸形 trace 转语义化 500（此前异常裸抛）。
- **trace_api `first_seen` 0 哨兵退化**：条目时间戳含真实 0 时 `first_seen == 0` 恒成立，后续任意 ts 都会覆盖，first_seen 退化为「最后一条时间」；改用 None 哨兵保证取最小值。
- **SDK XHR 对象复用监听器累积**：同一 XHR 对象复用（open+send 多轮）时每轮 send 叠加 4 个监听器且从不摘除——第 3 次复用后单个请求被记录 3 次（已复现）；挂新监听前先摘除上一轮的 + 终态 handler 触发时自摘除（once 语义），同时消除旧轮次闭包（旧 url/method）误报新请求。
- **SDK `window.onerror` 透传原 handler 返回值**：原 handler 返回 true 可抑制浏览器默认错误上报，包装器此前丢弃返回值导致宿主页面抑制语义失效。

### 🛠️ 工程与测试

- **CI sdk-js-smoke 补登记 `sdk-destroy.test.js`**：v0.6.9 新增的该文件此前漏登 CI 门禁（本地 npm test 有跑、CI 没跑），与 v0.6.7 门禁缺口同型；同时登记本批新增 `sdk-minor-reuse.test.js`，CI 与 npm test 文件清单恢复一致。
- **测试**：新增回归 13 项（unit 11：阈值校验 3 + 超时存根形状 1 + parse_llm_json 1 + MCP 日志口径/async wait 指标 2 + DELETE 400 1 + stream 异常保护 1 + first_seen 2 + 阈值用例对 .env 免疫加固；SDK JS 2：XHR 复用防叠加 + onerror 返回值透传）。
- **验证**：`ruff check .` 全绿；unit **1420 tests / 0 failed / 6 skipped**（1409 基线 + 11）；integration **113 tests / 0 failed**（与 v0.7.0 首绿基线持平）；e2e **9 passed / 1 skipped**（持平）；Browser SDK JS **49/49**（47 + 2）。

## [0.7.0] - 2026-08-29

> 主题「稳定 + 可观测 + 债务清理」。v0.7.0 不含 P0/P1 缺陷修复——全部缺陷治理已在 v0.6.x 收口；本版交付 **KB 学习闭环可观测性**（唯一面向用户的新能力）+ **Minor 大扫除两批 20 项**（安全/健壮性 10 项 + 死代码/文档口径 10 项）+ **工程卫生**，并首次达成 **integration 套件全绿**。测试基线：unit **1409 tests / 0 failed / 6 skipped**（junit 权威计数，v0.6.9 基线 1409 同口径持平——本版以加固与清理为主，新增 KB 可观测性测试对冲了死代码删除）、integration **113 tests / 0 failed**（96 passed / 17 skipped）、Browser SDK JS **47/47**、e2e **9 passed / 1 skipped**、ruff 硬门禁全绿。**无 Breaking Change，配置全部向后兼容**（升级注意见下）。

### ⚠️ 升级注意

1. **npm 包要求 Node >=18**（`engines` 自 `>=16` 收紧）：cli.js/check.js 运行时无 Node 20+ 特性依赖，Node 18 为事实最低维护线；Node 16 已 EOL 不再受支持。`npm install` 在旧 Node 上会收到 engine 警告。
2. **无 breaking change**：全部配置向后兼容；KB 可观测性指标/端点为纯新增，`/api/dashboard/kb-stats` 默认 viewer 可读。

### ✨ 新增：KB 学习闭环可观测性（工作包②）

> v0.6.9 修复了「异常指纹算好被丢」的闭环断点（R7-P1-2），KB 三级命中 / 向量 RAG / 分析回写 / verify 写回 / 经验召回整条学习闭环复活——但没有任何指标能证明"它在学"。本版补齐观测面：

- **Prometheus 指标**（对齐现有 dict counter + `_counter_lock` + 上限治理 + OTel 双写惯例）：
  - `kb_hits_total{level}`：`l1_fingerprint`（精确指纹）/ `l1_5_normalized`（归一化指纹）/ `l2_type`（类型级 Jaccard）/ `vector_rag` / `miss`——四条命中路径逐一埋点，miss 单独计数（闭环空转的关键信号）；
  - `kb_writeback_total{kind,status}`：分析回写（`success` / `failed` / `skipped`）与 verify 写回（`success` / `miss` / `skipped`）；
  - `kb_experience_recall_total{status}`：经验召回 `hit` / `miss`；
  - 全部 record_* 函数体 try/except：埋点永不影响业务返回值与异常传播。
- **`GET /api/dashboard/kb-stats`**（viewer 可读）：KB 条目总数 / seed vs llm 来源分布 / 学习条目占比 / verify_count>1 重复验证条数（新增 `KnowledgeBaseStore.stats()` 锁内只读汇总）+ 指标快照；store 或快照失败静默降级零值，不抛 500。
- **Dashboard 前端「KB Learning Loop」面板**：总条目（学习 n · 占比 · 重复验证）、三级命中分布、分析/verify 回写、经验召回数字；复用现有样式，零新依赖，加载失败静默占位。
- **埋点挂在真实生产路径**（v0.6.9 教训）：集成测试走真实链路（`save_trace` → `build_debug_context` → `analyzer.analyze` 主入口携带真实生产指纹命中 KB）断言 `l1_fingerprint` counter 增长且 `analysis_source == "knowledge_base"`。

### 🔒 修复与加固（Minor 大扫除第一批，安全/健壮性 10 项）

- **SDK `_redact` 环引用保护**：上报 extra 含循环引用对象（`a.self = a`）此前递归爆栈，`reportError` 抛 RangeError 整条上报丢失；现 WeakSet 标记截断为 null，输出仍可安全序列化。
- **SDK `_isSelfRequest` 改 URL 解析后比较 host**：`http://localhost:8000.evil.com` 此前命中端点前缀被误判为自请求 → 上报数据静默丢弃；现按 scheme/host 比较（相似域名绕过防丢数据；非安全边界）。
- **beacon 体积检查改 UTF-8 字节数**（TextEncoder + 老浏览器兜底）：中文等多字节字符约 3 万字（≈9 万字节）此前按字符数误判"可走 sendBeacon"→ 超 64KB 限制丢数据。
- **spec PATCH 字段白名单**：未知字段此前直接写入规范持久化/回显，现按 Spec 模型对齐过滤（id 保护语义不变）。
- **`.env.example` 补安全配置段**：`API_KEYS`（多 Key 轮换）/ `RBAC_ENABLED` / `RBAC_ROLE_MAPPING`（fail-closed 说明）/ `BEACON_TOKEN_TTL_SECONDS` / `BEACON_TOKEN_SCOPE`，CORS 补生产建议（防样例部署漏配安全项）。
- 其余：SDK 压缩路径节流时间戳同步登记配套的空队列 flush 定时器句柄清理（防御性）、`_pendingBatches` 上限 50（丢最旧 + 告警，防长时间断网恢复内存无界）。

### 🧹 清理（Minor 大扫除第二批，死代码 / 文档口径 9 项）

- **死代码删除**（均经全仓零引用验证）：`agent/schemas.py` 四个零消费方 Pydantic 模型（RepairRequest/RepairPlan/RepairJob/Sources——真实契约是 dict 形态 repair_plan）；`_compressAndSend` 内部 useBeacon 恒为 false 的分支与专用 `_sendBatchSyncCompressed`；deploy 两文件的 `PROMETHEUS_ENABLED` 死配置（app 从未读取）。
- **命名与文档口径修正**：`_safe_blame_frames` → `_collect_frame_changes`（实际语义是逐帧收集可疑改动，非 blame 且非异常安全包装）；SDK 头注释版本同步；agent 三处 docstring 更新为 `agent_mode`/`is_agent_active` 语义（6414fed 后滞后）；`seed_data` 种子计数 30→45（运行时权威 `len(SEED_CASES)==45`）；`analysis_queue.start()` docstring 改为非幂等口径；SDK_GUIDE `sampleRate`/`networkSampleRate` 描述与 G2 后实现对齐（错误类豁免采样、网络事件双重采样如实标注）。
- **一项按纪律跳过**：`capture_exception` 的 `sys.exc_info()` 分支经核实为受测试保护的公开契约（`test_no_exception` 显式断言无参行为），非死代码——保留。

### 🛠️ 工程与测试（工作包④ + 计划外）

- **npm 5 包**：`engines` 统一 `>=18`（元包自 `>=16` 收紧），browser-sdk 补 `license: MIT`。
- **CI**：ci.yml 加 concurrency（同分支重复推送取消在途旧 run）+ setup-python pip cache（requirements-locked/dev 双依赖路径）；release-npm.yml 加 concurrency 但 **cancel-in-progress: false**（发布中途取消会留下半发布状态——npm 禁止覆盖已发布版本，v0.6.5 事故即此类窗口）。
- **pytest 开 `--strict-markers`**：未注册 marker 直接报错；unit 1409 tests 基线不变。
- **lint.sh 锁定 ruff==0.16.4**（与 requirements-dev 同源）+ smoke_test clientInfo 动态读 `app.__version__`。
- **integration 套件首次全绿**（计划外）：修复 11 个长期存在的历史盲区失败（B2 门控统一后的 monkeypatch 遗留 ×6、C2 子进程化对闭包 handler/父进程打桩的影响 ×3、断言未适配 ×1、队列串扰根治 ×1）——`pytest tests/integration` **113 tests / 0 failed**（96 passed / 17 skipped）。

### 📊 测试与质量

> unit **1409 tests / 0 failed / 6 skipped**（junit 权威计数）；integration **113 tests / 0 failed**（96 passed / 17 skipped，首次全绿）；Browser SDK JS **47/47**；e2e **9 passed / 1 skipped**；`check_doc_links` 166 链接 0 错误；ruff 硬门禁全绿。

## [0.6.9] - 2026-08-29

> 第 7 轮全量代码审查修复发布：**P1 两项**（XFF 反代限流绕过复活修复 / 异常指纹"算好被丢"闭环修复）+ **Major·P2 22 项** + **第 6 轮遗留 P2 全部收口**（B2/B4/B5、C2 僵尸线程、G3 SDK 泄漏）。测试基线 unit **1298 → 1386**（+88）/ 6 skipped / 0 failed，Browser SDK JS **35 → 42**（+7），ruff 硬门禁全绿，零回归。反代部署语义不变：仍须配置 `TRUSTED_PROXY_COUNT`（本版修复其 off-by-one 解析错误）；heavy 工具改为子进程执行（见运维注意）。

### 🔒 安全

- **XFF 可信代理解析 off-by-one 修复，反代限流绕过复活（R7-P1-1）**：v0.6.8 引入的 `TRUSTED_PROXY_COUNT` 存在 off-by-one——候选取 `entries[-(count+1)]` 多退一格落进客户端可伪造的前缀区（count=1 标准反代下攻击者发 `X-Forwarded-For: 1.1.1.1`，nginx 追加真实 IP 后解析出的是伪造值当限流键，轮换伪造 IP 即得全新限流桶，完全绕过 `/api/debug/analyze` 10/min 与全局限流，与 A1 修复前漏洞同形）。现候选改取 `entries[-count]`、guard 改 `len(entries) >= count`，按标准 nginx `$proxy_add_x_forwarded_for` 追加语义解析（每层可信代理追加其对端地址，XFF 最右 N 条中最左一条即真实客户端）；测试 fixture 按真实代理语义重写并新增伪造前缀攻击回归（旧 fixture 把代理自身 IP 当 XFF 最右段——现实中不存在的形状掩盖了契约错误）。
- **SecurityAgent severity 归一化 fail-open 修复（R7-S1）**：风险 severity 归一化把 `critical` / `High`（大小写变体，集合匹配大小写敏感）静默降成 `low`——verify_loop 安全门的 `("critical","high")` 分支对真实 LLM 输出不可达（与 overall_severity 非法归 `unknown` 被拒绝的安全姿态不一致）。现按 fail-safe 归一：critical→high、匹配前大小写归一、非法/缺失值保守按 `high` 交由安全门钳制（宁可多拦，不可漏放）。
- **redaction 子串误伤 author 修复（R7-S2）**：`_SENSITIVE_SUBSTRINGS` 含裸 `"auth"`，`is_sensitive_key("author")` 命中 → git blame 的 author 归因字段在送 LLM 前被整值掩码，"这行谁改的"归因核心信息失效。现内置白名单补 `author` / `authority` 等 6 键；`authorization` 头仍按敏感键处理（不引入 CR-2 回归）。

### 🏗️ 结构 / 学习闭环

- **异常指纹"算好被丢"三断点修复，KB 学习闭环复活（R7-P1-2）**：异常指纹上游已算好（`errors.record` 用 `compute_fingerprint` 计算、`trace_repo.get_trace` 返回），但 context 构建侧把它丢弃——下游 KB 三级命中、向量 RAG、分析回写、verify 写回、经验召回因"指纹为空"整体短路：每次分析都打 LLM（成本/延迟）、LLM 结论永不回写（KB 永远只有种子）、verify 置信度永不累积、经验召回恒空。三断点同批修复：① `build_debug_context` 注入 exception / errors[0] / 顶层三处指纹（fallback 合成 trace 不再显式 None，error 条目指纹透传或现算）；② `save_trace` 落库持久化指纹，重启/缓冲淘汰回读路径重算兜底（旧数据无字段兼容）；③ `capture_exception` 返回结构增加指纹（`/api/debug/run` 等不经 `errors.record` 的直连路径首次产指纹）。配套真实生产链路契约测试 9 项（`/run` 端点函数真实执行、`save_trace` 真实落库、重启回读），杜绝"同形状 fixture 自证"（第 6/7 轮反复翻车的根因）。
- **重活僵尸线程根治——线程池改子进程隔离 + 超时强杀（C2，第 6 轮遗留）**：heavy 工具（verify_ui / auto_test 等 Playwright 自动化）由 2-worker 重型线程池改为每次调用独立 spawn 子进程执行（新增 `app/mcp/protocol/heavy_process.py`）：超时 `terminate()`（钉死升级 `kill()`）强杀回收——线程无法强杀是根因，进程可杀；不再有僵尸线程打满 heavy 池致恒 TOOL_BUSY/TOOL_TIMEOUT 的无自愈状态，将来任何同步重活自动受此保护。`register_tool` 新增 `prepare_args` 钩子（verify_ui 的 spec_id 在父进程预解析——子进程读不到父进程内存态 spec_store）；HTTP 与 stdio 两个入口的同步 heavy 分支统一走子进程；auto_test 浏览器必关（异常路径也保证浏览器进程回收）。**⚠️ 运维注意：heavy 工具现在每次调用起子进程（Windows 用 spawn），冷启动开销略增。**

### 🛠️ SDK 数据采集（browser-sdk）

- **destroy() 销毁接口 + 去重表清理（G3，第 6 轮遗留）**：`_debounce` 去重表键含动态 className 只增不删（无限增长）；SDK 无销毁接口，Vite HMR 热更新产生监听器二次包裹 + 事件重复上报。现去重表加过期清理（TTL 1s）与尺寸上限（1000 键按时间戳淘汰最旧）；新增公开 `AiDebug.destroy()`：摘除全部监听器（UI 捕获需同参 capture=true）、还原 window.onerror / fetch / XHR 原型 / console 为安装前原始值、停止全部定时器、清空队列/缓冲/去重表、复位 `_inited`（幂等、可安全重新 init）；修复 25s beacon 心跳 setInterval 句柄未保存无法停止的问题。**升级注意：页面卸载 / HMR 场景建议显式调用 `AiDebug.destroy()`。**
- **压缩路径节流失效修复（R7-G1，已运行时复现）**：节流时间戳只在异步压缩回调里登记，JS 单线程下同步 while 循环跑完全部分片后回调才执行 → 所有分片看到过期时间戳，`maxBatchesPerWindow:2` 时实测 4 个请求齐发（恢复积压时瞬时并发尖峰 + 限流负载放大）。现时间戳同步登记在 `_flushBatch` / `_drainPendingBatches` 的发送决策点（`_sendBatchDirect` / 压缩回调内部登记移除避免双计数）；顺带修复 pagehide 时事件队列为空但暂存批次非空被提前 return 静默丢数据的缺口。新增「压缩×节流」组合回归测试（此前 4 项节流回归全部 `enableCompression:false`，零覆盖）。

### 🛠️ 正确性与可靠性（第 7 轮 Major·P2 其余）

**MCP / runtime**

- **槽位取消泄漏修复（R7-T1）**：等槽位期间外层请求协程被取消（如 HTTP 断连）时，`wait_for` 传播 CancelledError 不被捕获——若此刻内层 acquire 已完成则槽位取得却无人归还（与 v0.6.6 修的"超时同拍"同形），重复发生使池容量永久缩减至恒 TOOL_BUSY。现捕获 CancelledError 后按完成态归还槽位再传播取消，并补真实取消时序测试。
- **query_pg_errors 连接池中毒修复（R7-T2）**：全仓唯一未经 `_safe_put` 保护的裸 `pool.putconn`——非 OperationalError（ProgrammingError/DataError）后连接停留 aborted 事务入池，下一借出者恒抛 InFailedSqlTransaction 直至重启。现统一走 `_safe_put` 归还（rollback 防中毒）。
- **sys.path 误判框架帧修复（R7-T3）**：`is_framework_frame` 用整个 sys.path 前缀判定——cwd≠项目根且项目根经 PYTHONPATH 进 sys.path（容器 WORKDIR=/ + PYTHONPATH=/app 常态）时项目帧被判为框架 → 帧被折叠、`[PROJECT CODE]` 标记丢失，喂给 AI 的根因代码被隐藏。现改用 sysconfig 真实 stdlib 路径（对齐 fault_localizer 同款修复）。
- **向量索引只增不减修复（R7-T4）**：KB LRU 驱逐与 `clear()` 均不同步删向量库，`VectorStore` ABC 无 delete 语义——被驱逐/清空条目的向量点永久残留，`_try_vector_rag` 继续召回已淘汰的历史结论直接返回用户。现 ABC 增 `delete(fingerprints)`（默认 no-op 保持后端兼容），InProcess 原地删除+重建索引、Qdrant 按 uuid5 确定性 point id 删除，KB 驱逐与 clear 调用点同步。
- **InProcess 向量库幂等覆盖（R7-T5，B4 残留）**：Qdrant 用 uuid5 幂等覆盖而 in_process `add()` 纯 append——同指纹第 N 次分析产生第 N 份 doc，吃满 `vector_store_max_docs` FIFO 并挤掉多样化条目。现维护指纹→槽位索引原地覆盖（含驱逐/delete 后索引重建的元组解包修正，复核回归），与 Qdrant 跨后端语义一致。

**存储 / 校验**

- **async_pg_store 归档失败回滚（R7-V3）**：归档失败仅 warning，asyncpg 连接停留 failed transaction，紧接的 DELETE FROM traces 复用同连接必抛——每轮清理同位失败，过期清理永久停摆。现归档 except 补 ROLLBACK。
- **async-mix fail-fast 修复（R7-V4）**：`pg_async_enabled=True` → `_raise_async_mix` 抛 RuntimeError 被 `except Exception` 捕获 → 默认 `storage_fallback_to_memory=True` 静默降级 memory（重启即丢），与"启动即抛配置错误"的 docstring 自相矛盾。现引入专用 `_AsyncMixError` 在五个 getter 中先行 re-raise，配置错误必须 fail-fast。
- **断言引擎类型边界修复（R7-V1/V2）**：`_values_equal` 首行 `a == b` 短路使 `True == 1` 判相等（期望 True 实际 1 的 silent failure 被漏报——恰是断言引擎要检测的缺陷）；`_judge_silent_failure` 对 str status（JSON 反序列化 `"500"`）一律返回 True → 5xx 被误判静默失败误导 AI 归因。现 bool 排除前移到 `==` 之前、str/float status 先归一转数值再判 4xx/5xx。

**API / 入口 / 传输**

- **畸形 JSON 走错分支修复（R7-A2）**：`JSONDecodeError`/`UnicodeDecodeError` 都是 ValueError 子类，被 `/ingest/batch` 的 gzip 超限 `except ValueError` 分支吞掉——非法 JSON/非法 UTF-8 返回 413 且回显解析器内部信息。现解压超限改专用异常，JSON/UTF-8 失败归一 400 + 固定文案；gzip 炸弹仍 413。
- **危险绑定检测子串匹配修复（R7-A1）**：`"0.0.0.0" in str(bind_host)` 漏掉 IPv6 通配 `::`（等价全网监听，SEC-03 对 IPv6 失效），且误杀含子串的合法地址（`10.0.0.0`）。现用 `ipaddress.ip_address(bind_host).is_unspecified`（IPv4/IPv6 通配均成立），主机名保留非回环 warning 路径。
- **SSE 缺缓冲控制头修复（R7-A3）**：MCP POST 回退流 / GET /mcp 流 / analyze-stream 三处 `StreamingResponse` 无 `Cache-Control: no-cache` / `X-Accel-Buffering: no`——nginx 默认 `proxy_buffering on` 攒批延迟心跳与事件（dashboard 流有头、MCP 流没有，修复不对称）。现抽公共助手 `app/api/sse.py` 三处统一补齐。
- **dashboard 缓存成本放大修复（R7-A4）**：E1 修复后缓存恒按 1000 条全量算，`_extract_error_summary` 对每个 error 做两次完整 `get_logs` 扫描——30s TTL 失效一次 ≈ 3000 次存储读取。现缓存按 limit 分档（100/1000，常态请求只算 100 档）+ 摘要提取合并单遍扫描，常态请求每 TTL 存储读取量约降一个数量级；各档独立缓存，E1 不截断语义保持。
- **stdio 工具线程池退出关闭（R7-A5）**：`ThreadPoolExecutor` 非 daemon 且退出从不 shutdown——超时仍在跑的工具线程在解释器退出时被 `_python_exit` join，进程无法退出直至宿主强杀。现 `cleanup_resources` 增 `shutdown(wait=False, cancel_futures=True)`。

**质量 / 评分契约（R7-Q1~Q5，CR-1 型错配同族）**

- **修复链路 debug_context 形状补齐（R7-Q1）**：修复链路生产入口用 `build_context`（仅 `{request_id,flow,input,output,errors}`），而 scorer 读 `code_snippets`/`git_blame`/`recent_diffs`/`network_trace`/`ui_events`/`spec_diffs`（只由 `build_debug_context` 产出）——权重合计 0.53 恒缺失，quality_report 完整度上限 ≈0.47 进 RepairAgent prompt 与 API 输出；`_score_git_context` 声明的 repair_ctx.git_context 回退从未实现。现 assembler 用真实生产者 `build_debug_context` 富化简化形状（失败静默降级），scorer 补齐 git_context 回退。
- **prior_analysis 形状错配修复（R7-Q2）**：`analyze_async` 返回 `{analysis:{root_cause,confidence},...}`（嵌套），scorer 读顶层 `root_cause` 恒 None → LLM_ANALYSIS 维度（0.12）恒缺失 + 丢一条高相关证据（测试 fixture 用扁平形状再次掩盖）。现 `_unwrap_prior` 兼容两形状。
- **对象以 repr 进 prompt / MCP 响应修复（R7-Q3）**：`quality_report`(Pydantic) 与 `debug_experience`(dataclass) 经 `json.dumps(default=str)` 序列化为 Python repr 字符串——结构信息退化为语法噪音，MCP 消费方拿到不可解析字段。现 assembler 落盘前 `model_dump()` / `to_dict()`。
- **轮预算算术矛盾修复（R7-Q4）**：`round_timeout=0 → 继承 agent_timeout=90`，而单轮内部预算 = RepairAgent 90s + 并行节点 90s = 180s——继承 90s 实为收紧一半，RepairAgent 耗时 60-90s 时并行阶段启动数秒即被 watchdog 取消，整轮返回 `{"repair_plan": None}` 存根、已成功成果丢弃。现按单轮内部预算继承（agent_timeout + 并行预算）。
- **error_message 无截断修复（R7-Q5，B1 同类未修兄弟）**：`error_message` 原样进 test/security Agent prompt（无 `max_context_tokens` 预算截断），`/ingest/error` 对 message 无字段级长度上限（唯一上界整体 1MB）→ MB 级 prompt、超上下文、并行节点 FAILED。现两处复用同一截断纪律（~8K 字符）。

### 🛠️ 第 6 轮遗留 P2 收口

- **B2 / B4 / B5**（6e0b23e）：`is_agent_active` 统一门控全部入口——`AGENT_MODE=dag|verify_loop` 死配置路径激活；消除向量双写（同指纹不再双份 doc）；`_try_vector_rag` 增 `vector_store_min_score` 二次过滤（in_process/Qdrant 跨后端语义一致）。
- **C2 / G3**：见上文「结构 / 学习闭环」与「SDK 数据采集」两节（本版根治，第 6 轮遗留 P2 全部清零）。

### 📊 测试与质量

> 测试基线：unit **1298 → 1386 passed / 6 skipped / 0 failed**（+88：R7 契约与回归测试、C2/G3 修复测试、指纹闭环契约测试 9 项）；Browser SDK JS **35 → 42**（+7：压缩×节流组合、destroy()、去重表清理等）；ruff 硬门禁全绿，零回归。

## [0.6.8] - 2026-08-27

> round-6 审查修复发布：**P0 安全×5**（CR-1/CR-2/CR-3/A1/A2）+ **P1 十四项全量**（A3/A4、B1/B3、C1/C3/C4/C5、D1/D2/D3、E1、F3、G2）+ G1 + 测试基础设施 2 项 + **P2 安全/可靠性×6**（D4/D5/D6/E2/F1/F2）+ **发布工程×4**（F4-F7）。测试基线 **1298 passed / 6 skipped / 0 failed**（unit，v0.6.7 基线 1231 → 1290（P0+P1 +59）→ 1298（P2 六项 +8）；另 integration 新增 A2 直写脱敏 3 项 + D2 两段式安装 2 项），本地全量 **1377+ passed / 0 failed / 0 errors**，SDK JS 35/35，ruff 硬门禁全绿，零回归。

### 🔒 安全

- **verify_loop 安全门字段错配修复（CR-1）**：`compute_verify_score` 读取 SecurityAgent 输出中不存在的 `findings` 键（真实契约为 `risks` / `overall_severity`），恒为空导致含 high 风险（SSRF、硬编码密钥等）的修复方案也能通过安全门并获得 PASSED/HIGH_CONFIDENCE 判定——v0.6.x 专门加的安全门钳制完全失效。现按真实契约双检 `risks`（severity 含 critical/high）与 `overall_severity`（high/unknown 不通过），并对畸形 shape（既无 risks 也无 overall_severity，如旧 findings 形态）fail-safe 拒绝；全部测试 fixture 改为真实输出契约并新增端到端契约测试。
- **脱敏正则下划线复合键缺口修复（CR-2）**：`redact()` 默认规则用 `\b(固定键名列表)` 匹配，词边界在 `_`（word 字符）处不成立，`refresh_token` / `client_secret` / `session_token` / `api_secret` 等复合键在纯文本与 JSON 字符串路径整体漏脱敏（如 SDK 序列化的 request_body 原样入库 PG）。现键名匹配改为"包含敏感词干"语义（password/passwd/pwd/secret/token/apikey/credential/private_key 词干 + `[_-]key` 后缀），`keyword` / `monkey` / `author` 等正常词不误伤；Qdrant embedding 外发路径的内联规则副本同步修复；`capture_exception` 的 locals 键名判定从 8 个精确键名改为白名单感知的子串匹配。
- **XFF 伪造绕过限流修复（A1）**：限流键的客户端 IP 无条件信任 `X-Forwarded-For` 最左值——该字段首段可被客户端任意伪造，直连部署下攻击者每个请求换一个伪造 IP 即获得全新限流桶，完全绕过 `/api/debug/analyze` 10/min 与全局限流。现引入 `TRUSTED_PROXY_COUNT`（默认 0 = 不信任转发头，一律用直连对端 IP）；配置 N>0 时仅当直连对端为私网/回环（自有反代）才取 XFF 右起第 N+1 个地址。**反代部署升级后须配置该值**，否则所有用户共享代理 IP 的限流桶（互相误伤，限流仍有效）。
- **add_log 直写路径明文入库修复（A2）**：`logs.add_log` / `add_logs_batch` 直接透传 data 写存储（`POST /debug` 的 request_start 等入口把用户原始 payload 含 password/token 明文入库，viewer 角色经 dashboard 可读），违背"存储边界统一脱敏"承诺。现键名判定与嵌套递归脱敏统一下沉到 `redaction` 模块（`is_sensitive_key` / `redact_nested` 公开 API），trace_repo / logs / stacktrace / context_prep 全部存储与外发边界共用一份实现。

### 🛠️ SDK 数据采集（browser-sdk）

- **批量恢复毒批循环修复（CR-3）**：服务端 `/ingest/batch` 单请求上限 100 条（超限 413），SDK 恢复 localStorage 暂存批次时把全部事件合并进队列单次 flush——endpoint 宕机过夜后恢复 10 批 × 20 条 = 200 条必然 413，而 413 被当作可重试错误整批重试 3 次（不拆分），重试耗尽后整批回写 localStorage，下次启动重复该循环：积压事件永远无法送达且负载滚大。现三处同时修复：flush 按服务端上限分片发送（beacon 路径同样分片）；413 触发对半拆分重发（指数收敛到单条，单条仍拒则丢弃防无限循环）；恢复路径经分片 flush 自然安全。新增 `sdk-batch-limit.test.js` 回归测试 4 项并登记入 CI 与 `npm test`（顺带修复 package.json test 脚本遗漏 `sdk-transport-fixes.test.js` 的门禁缺口）。

### 🧪 测试基础设施

- **e2e uvicorn 启动被 SEC-03 误杀修复**：`tests/conftest.py` 的 `HOST=127.0.0.1` 环境哨兵因 `tests/__init__.py` 导入链抢跑而失效（settings 单例在哨兵前已创建并读入默认 `host="0.0.0.0"`，与 M13 的 API_KEY 哨兵同坑），且单例重置漏了 host 字段——e2e 测试服务器实际绑定回环地址，却被 lifespan 里的启动守卫按 `settings.host=0.0.0.0` + 无鉴权判定拒绝启动，e2e 全部 10 个用例 ERROR。现单例重置补齐 `settings.host="127.0.0.1"`（与实际 bind 一致）。
- **Windows 11 24H2+ 损坏 pytest-current junction 防崩补丁**：旧版 pytest 在 `%TEMP%\pytest-of-<user>\` 留下的 `pytest-current` symlink 被系统标记为不受信任挂载点（WinError 5，需管理员权限才能删除），pytest 8.3.x 的 `cleanup_dead_symlinks` 遍历时 `resolve()` 抛 PermissionError 未捕获，整个测试会话在创建 basetemp 时崩溃（0 tests ran）。conftest 中将该清理函数替换为异常安全版本（单条目失败仅跳过）。

### 🛠️ P1 修复（第 6 轮审查 P1 十四项全量）

**API 与限流**

- **/ingest/batch 畸形 JSON 500 修复（A3）**：顶层非对象（`[1,2]` / `"abc"`）时 `req.get` 抛 AttributeError、events 元素非 dict 时 `event.get` 在 try 块外抛 AttributeError——`{"events":[1]}` 即触发 500 + 完整堆栈日志（可被滥用于日志洪水）。现校验 `isinstance(req, dict)` 与逐 event `isinstance(event, dict)`，按 422 语义化拒绝。
- **限流 key 路由模板归一化（A4）**：限流 key 含原始 path，动态段端点（`/ingest/network/{trace_id}` 等）每个 ID 独立成桶，攻击者轮换 ID 即绕过该档位限流。现限流中间件在路由解析前用 app.router 路由表预解析模板（与 MetricsMiddleware 事后读 `scope["route"].path` 的语义一致）：静态路径 key 完全不变，动态路径归一化为模板共享桶；404/解析失败回退原始 path。

**Agent / LLM 链路**

- **RepairAgent prompt 大小上限（B1）**：Agent 链路此前无任何 prompt 预算（analyzer 有 truncate_context，Agent 没有）——debug_context 含原始 request body（上限 1MB）时 prompt 可达 MB 级，超上下文、成本失控、agent_timeout 内必然失败。现按与 analyzer 同源的 `max_context_tokens * 3` 字符预算截断并附截断标记，正常大小 payload 零影响。
- **Agent LLM 调用接入熔断器（B3）**：Agent 链路（repair/test/security + 队列 worker）此前完全绕过 LLM 熔断器（与三个 Agent 文档串"熔断器自动覆盖"的声明相反）——LLM 宕机时持续打满重试拖长队列。现 `BaseAgent._create_completion` 统一经 analyzer 的 `_call_async_through_breaker` 执行：成功/失败计入共享熔断状态机；熔断 OPEN 时 CircuitBreakerError 快速失败（不重试、不打 fallback，不发起任何真实调用）；熔断器未启用（默认）时直连调用，行为与旧实现完全一致。
- **repair_async 事件循环阻塞修复（C1）**：async handler 内的三步同步重 IO（get_logs 走 PG、build_context 全量聚合、collect_runtime_snapshot 含 psutil 100ms 阻塞采样）此前直接跑在事件循环线程——执行期间整个服务（HTTP/stdio/心跳/SSE）停摆。现统一移入 `asyncio.to_thread`。

**MCP 协议 / 传输**

- **SSE 队列满分级丢弃（C3）**：队列满"丢最旧"曾会静默丢弃带 id 的 JSON-RPC 响应（mcp_post 已对该请求返回 202，客户端永久悬挂）。现按消息类别分级：close 控制事件必须送达；响应类优先挤掉最旧**通知**腾位，队列全为在途响应（客户端实质失联）才丢最旧响应并记 error；通知类在全响应队列下直接不投递（宁可丢新通知，不丢任何在途响应），全部丢弃路径不再静默（warning/error 日志）。
- **MCP SSE 心跳（C4）**：GET /mcp 流此前无限期等待 `q.get()`——反代（nginx 默认 60s）静默切断空闲流，纯监听会话 30 分钟后被 TTL 清理踢下线。现 15s `: ping` 注释行心跳（与 dashboard 流对齐），心跳同时刷新会话 last_active，两个问题一并解决。
- **inputSchema 轻量校验（C5）**：inputSchema 此前仅用于 tools/list 展示从不校验——缺 required 参数/类型错误在直接索引型 handler 抛 KeyError/TypeError 被吞成 TOOL_INTERNAL，而 LLM 客户端依赖 -32602 INVALID_PARAMS 做参数自纠错。现入口按注册 schema 校验 arguments 为 dict、required 存在性、顶层类型（integer 容忍整值 float、拒绝 bool 冒充数值、显式 null 按类型错误、未声明额外参数不拒绝保持兼容）；顺带修正 repair_async schema 的 required 与 handler"request_id/trace_id 二选一"契约不符。

**Storage / Runtime**

- **PG 连接中毒修复（D1）**：非 OperationalError（UniqueViolation/ProgrammingError 等）后连接停留在 aborted 事务状态直接归还池，下一个借出者恒抛 InFailedSqlTransaction（25P02 非 OperationalError 不触发重连）——连接永久中毒直至重启，LIFO 取连接放大影响。现四层防护：`_execute_with_retry`/`_query_with_retry` 增加通用 `except psycopg2.Error` 回滚后原样抛出；`_safe_put` 与 pg_trace/pg_session 的 `_put` 归还前统一 rollback（psycopg2 无活动事务时为客户端空操作）；`_ensure_init` DDL 失败与分区预创建失败口同样回滚。
- **exception_hook 两段式安装（D2）**：单一 `_installed` 标志导致首次在无事件循环上下文安装（asyncio 部分被跳过但标志已置位）后，lifespan 里再调用直接 return——asyncio 任务异常捕获永久失效。现拆分为 excepthook/asyncio 两个独立标志，支持"先装 excepthook、事件循环就绪后补装 asyncio handler"，两部分各自幂等。
- **errors 迁移脚本顺序修复（D3）**：`CREATE UNIQUE INDEX (fingerprint, session_id)` 排在 `ALTER TABLE ADD COLUMN session_id` 之前——旧 schema 库执行到建索引即报 column does not exist 中断，兼容补列段永远执行不到。现兼容 ALTER 段整体前移（含补 fingerprint 列防御），全新建库时为幂等 no-op。

**API / 发布 / SDK**

- **Dashboard 缓存 limit 固化修复（E1）**：概览缓存按首个请求的 limit 计算并缓存整个 result——30s TTL 窗口内小 limit（10）先缓存，后续大 limit（1000）命中 `cached[:1000]` 却只有 10 条（L2 Redis 跨实例共享污染面更大）。现缓存统一按最大档 1000 计算存储，调用方按需切片。
- **release-npm 版本一致性硬校验（F3）**：发布版本号完全来自 tag 名/手工输入，与二进制内 `app.__version__` 无校验——打 tag v0.6.8 但忘改 `app/__init__.py` 会发布"npm 0.6.8 / MCP 握手 serverInfo 报 0.6.7"的错版包且无告警。现 publish 前硬校验发布版本 == `app.__version__`，不一致直接 fail 并给出修复指引。
- **SDK 错误类上报豁免采样（G2）**：sampleRate 对所有事件统一门控，手动 reportError/reportSilentFailure/reportNetworkError 与全局异常捕获（window.onerror/unhandledrejection）在 sampleRate=0.5 时有一半概率被无提示丢弃——业界惯例错误类事件不参与采样。现错误类路径 force=true 绕过采样；遥测类（network 自动捕获/ui-event/console）保持原有采样行为不变。

### 📊 测试与质量

> 测试基线：unit 口径 **1298 passed / 6 skipped / 0 failed / 0 errors**（v0.6.7 基线 1231 → 1251（P0 +20）→ 1290（P1 +39）→ 1298（P2 六项 +8：D5×1、D6×2、E2×1、F2×4）；另 integration 口径新增 A2 直写脱敏 3 项 + D2 两段式安装 2 项）。本地全量复验（unit+integration+e2e）：**1419+ tests / 1377+ passed / 42 skipped / 0 failed / 0 errors**。SDK JS 5 文件 **35/35 pass**，ruff 硬门禁全绿，check_doc_links 164 链接 0 错误。

### 🛠️ P2 修复（安全/可靠性六项 + 发布工程四项）

> 第 6 轮审查 P2 十六项中的安全/可靠性项与发布工程项已完成并随 v0.6.8 发布；其余 P2（B2/B4/B5/C2/G3）与 90+ Minor 并入 v0.7.0 取舍。

- **traces_archive 迁移文件补齐（D4）**：`ddl.py` 已定义 `DDL_TRACES_ARCHIVE` 但 `migrations/` 无对应迁移文件——纯迁移方式部署开启 `pg_archive_enabled` 后归档静默失败。新增 `20260827_create_traces_archive_table.sql`（与代码 DDL 对齐，`CREATE TABLE IF NOT EXISTS` 幂等）。
- **capture_exception 局部变量 repr 截断（D5）**：异常路径一个大局部变量（dict / DataFrame 等）的 `repr` 无长度上限，可膨胀到数十 MB 进入内存缓冲/PG/响应（`parse_network_record` 等模块有 10KB 截断纪律，此处缺失）。现单变量 repr 超 10KB 截断并附标记，正常小对象零开销。
- **sourcemap 单份大小上限（D6）**：上传 channel 条数 100 有界但单份可达几十 MB（`sourcesContent` 内嵌全源码）——OOM 面。新增 `SOURCEMAP_MAX_UPLOAD_BYTES`（默认 20MB），超限拒绝上传（400）。
- **spec 缓存刷新限频（E2）**：缓存命中时 `_cache_needs_refresh` 仍执行全项目 os.walk，一次 Debug Context 构建多次调用 `get_project_specs` 触发多次全目录遍历。现缓存刷新检查限频（30s 间隔），间隔期满仍按 mtime 精确判断；`reload_specs` 同步清限频时间戳保证强制刷新。
- **生产 compose 端口回环绑定（F1）**：app 端口此前绑 `0.0.0.0` 全网开放（API Key 明文 HTTP 传输、入口无 TLS）。现只绑 `127.0.0.1`，由同宿主前置反代（nginx/ALB/tunnel）做 TLS 终止，与 prometheus 的 loopback-only 发布一致。
- **/metrics 全局中间件豁免（F2）**：`METRICS_AUTH_ENABLED` 此前只控制端点层额外鉴权，无法豁免全局 AuthMiddleware——生产强制 API_KEY 时 Prometheus 抓 `/metrics` 恒 401、监控链路静默失效。现 `METRICS_AUTH_ENABLED=False`（默认）时 `/metrics` 在全局中间件放行（端点层同样不额外鉴权，供监控栈无凭据抓取，应只发布到可信内网）；`True` 时保留全局中间件保护，端点层再校验一次。
- **发布脚本命令注入修复（F4）**：release-npm.yml 把 `workflow_dispatch` 的 version 输入经 `${{ }}` 直接内插进 shell，含 `$(...)`/反引号时可在 runner 执行任意命令（GitHub 官方反模式）。现所有注入点改由 env 变量隔离传递，shell 内仅引用 `$VAR`。
- **发布构建可复现（F5）**：pyinstaller 此前裸装取最新，发布构建产物可能漂移。现固定 `pyinstaller==6.11.1`；`requirements-locked.txt` 补充注释说明以 `==` 锁直接依赖、如要完整锁传递依赖用 `pip-compile --generate-hashes`。
- **非支持平台安装指引修复（F6）**：linux-arm64 / osx-x64 用户此前 `npm install` 硬失败，且报错指引装一个"不存在的平台包"。现 `check.js`/`cli.js` 识别非 CI 构建平台并给出源码运行指引，且不再阻断安装。
- **发布前运行时验证（F7）**：build job 此前只 `test -f` 校验产物存在，从不真正启动二进制（hiddenimports 漏一项即"发布即启动即崩"）。现 `mcp_smoke_test.py` 支持 `--cmd` 指定启动命令，build 在各平台原生运行打包产物走完整 stdio 冒烟（启动→initialize→tools/list→tools/call），失败即阻断构建。

## [0.6.7] - 2026-08-25

> v0.6.x 正确性补丁：修复 7 个正确性组 Major 缺陷（SDK 数据采集三件套：gzip 回退乱码、pagehide 丢数据、节流齐发；Python 侧：LLM 指纹碰撞、流式绕熔断、smoke_test 死锁、sourcemap 缓存键版本混淆）。测试基线 **1231 passed / 6 skipped / 0 failed**（新增 14 项测试），零回归，无 Breaking Change。

### 🛠️ SDK 数据采集（browser-sdk）

- **gzip 回退乱码修复**：gzip 压缩发送失败回退时，旧实现把 gzip 二进制字节当文本存 localStorage（恢复时 `JSON.parse` 必然失败），400/415（接收端不支持 gzip）时直接丢数据。现原始明文 `body` 全程透传：localStorage 回退存明文、400/415 用明文重发一次。
- **pagehide 丢数据修复**：页面关闭/隐藏瞬间，节流暂存队列里攒的批次走 `setTimeout` 延迟发送（unload 后定时器不触发），数据直接丢失。现 beacon 路径同步排空暂存队列 + 当前批次（sendBeacon 或同步 XHR），绝不延迟到定时器。
- **节流齐发修复**：旧实现每条被节流的批次各自 `setTimeout`，延迟差毫秒级导致同一时刻齐射（节流失效反而形成尖峰）。现改为单一定时器逐条发送，间隔 `ceil(节流窗口/窗口最大批次数)` 错开发送。

### 🛠️ Python 侧

- **LLM 缓存指纹碰撞修复**：`_compute_context_fingerprint` 用 `"|"`/`":"` 裸拼接字段，字段值内含分隔符时不同上下文拼出同一字符串（如 `exc_type="A|B"` 与 `message="B|C"`），且 `[:16]` 截断放大碰撞面——缓存会返回错误分析结果。现改为 `json.dumps` 结构化序列化 + 完整 sha256 摘要。
- **流式路径绕过熔断修复**：LLM 调用的流式路径（`analyze_stream` / `analyze_stream_async`）此前完全绕过熔断器——非流式路径熔断开启时 fallback，流式路径继续直打 LLM。现两条流式路径接入同一熔断状态机（OPEN 时 fallback、成功/失败计入熔断计数），异步路径锁临界区经 `asyncio.to_thread` 执行。
- **smoke_test 死锁修复**：`scripts/mcp_smoke_test.py` 的 `stdout.readline()` 无超时（服务端挂死时冒烟脚本永久阻塞）、`stderr=PIPE` 从不排空（管道缓冲写满导致子进程阻塞）。现读超时 10s 兜底 + 后台线程排空 stderr + EOF 哨兵。
- **sourcemap 缓存键版本混淆修复**：`sourcemap_store` 仅以 artifact 为存储键，同 artifact 不同 release（bundle 版本）的 source map 互相覆盖——旧版本 map 会解析新版本堆栈，位置错误。现用 NUL 分隔符把 release 并入存储键，上传/查找/解析/API 全链路透传 release。

### 📊 测试与质量

- **测试基线**：1231 passed / 6 skipped / 0 failed / 0 errors（v0.6.6 基线 1221 → 1231，新增 14 项：SDK 传输修复 4 项、指纹碰撞 2 项、流式熔断 3 项、smoke_test 3 项、sourcemap 缓存键 2 项）
- **CI 全门禁绿**：ruff 0.16.4（advisory，777 项零净新增）+ check_doc_links + pytest 单测 + SDK JS 冒烟（4 文件 29 pass）
- **无 Breaking Change**：全部修复向后兼容，默认行为不变
- **待复核项**：verify_loop 超时覆盖结果一项经 Python 3.12 `wait_for` 语义分析确认不可复现（协程在取消生效前完成则正常返回），未做改动

## [0.6.6] - 2026-08-24

> v0.6.x 可用性补丁：修复 4 个可用性组 Major 缺陷（stdio 坏输入杀服务、超时背压槽位竞态、事件循环三处阻塞、async 工具绕过双池），并加固 JSON-RPC 协议层输入校验。测试基线 **1221 passed / 6 skipped / 0 failed**（新增 14 项测试），零回归，无 Breaking Change。
>
> 注：**0.6.5 版本号未正式发布**——发布流水线中 5 个 package.json 因本地编码事故损坏，首次 Release 流程中途将 3 个平台子包 0.6.5 发至 npm 后失败（元包未发布，无任何用户受影响）；因 npm 禁止覆盖已发布版本，相同内容改以 0.6.6 重新发布。npm 上的平台子包 0.6.5 为无引用的孤立版本。

### 🛠️ 可用性

- **stdio 坏输入杀服务修复**：stdio transport 的 `sys.stdin` 按 strict 模式解码，收到一帧坏 UTF-8 字节即抛 `UnicodeDecodeError`，且此后 TextIOWrapper 永久损坏（后续读取恒为空 = EOF），单条坏消息就让整个 MCP 服务退出。现改读 `sys.stdin.buffer` 并以 `errors="replace"` 解码：坏字节退化为 U+FFFD，主循环按 PARSE_ERROR（-32700）回错并继续服务。测试环境 stdin 为 StringIO（无 buffer）时回退原路径。
- **超时背压槽位竞态修复**：`asyncio.wait_for(slots.acquire(), timeout)` 在超时与获取完成同拍（典型：`busy_timeout=0`/极小且并发释放槽位）时，槽位可能已实际转移到本调用方，但调用方只看到 TimeoutError 并按 TOOL_BUSY fast-fail 返回且永不 release → 槽位泄漏，重复 N 次后池永久占满、全部工具恒 TOOL_BUSY。现超时后显式检查 acquire 任务完成态：已取得则立即归还（防泄漏）；另加快路径——有空位时直接 acquire（不挂起、不进等待队列），避免 timeout=0 的定时器把"有空位的快路径获取"误杀成 TOOL_BUSY。

### ⚡ 性能（事件循环阻塞）

- **三处同步阻塞移出事件循环**：以下三件事此前直接跑在 asyncio 事件循环线程上，卡住时整个服务停摆：
  - **Redis 限流查询**：`RateLimitMiddleware` 直接调用同步 `store.allow`（Lua 脚本 + socket_timeout=2s），Redis 慢/不可达时所有请求随之停顿。现移入 `asyncio.to_thread` 执行（Memory/Redis 后端均为 `threading.Lock` 保护，跨线程安全）。
  - **KB 写回**：verify_loop 验证通过后的 `record_verification` 同步 IO 现移入 `asyncio.to_thread`。
  - **pybreaker 熔断器锁**：`_call_async_through_breaker` 的三段 RLock 临界区（状态检查/失败计数/成功计数）整体移入线程池执行，锁争用不再发生在事件循环线程，熔断语义不变。

### 🔧 协议加固

- **JSON-RPC method/id 前置校验**：非字符串的 `method`（list/dict 等）会经 `model_construct` 原样透传，dispatch 路由时对不可哈希对象抛 TypeError 且错误码退化为 500/-32603。`id` 为 dict/list/bool 时同样透传回显，`NaN`/`Infinity` 更会产出非法 JSON（`{"id": NaN}`）。现前置校验：method 必须为字符串，id 必须为 String/Number/NULL 且有限，坏值按 -32600 返回且响应 id 为 null。

### 📊 测试与质量

- **测试基线**：1221 passed / 6 skipped / 0 failed / 0 errors（v0.6.4 基线 1207 → 1221，新增 14 项：stdio 坏帧×1、jsonrpc method/id 校验×5、槽位竞态×6、async 双池门控×2）
- **CI 全门禁绿**：ruff（advisory）+ check_doc_links（164 链接 0 错误）+ pytest 单测 + SDK JS 冒烟（12 pass）
- **无 Breaking Change**：全部修复向后兼容，默认行为不变

## [0.6.4] - 2026-08-24

> v0.6.x 安全补丁：修复 3 个安全组 Major 缺陷（embedding 未脱敏外发、verify_loop 安全门失效、限流键绕过），覆盖数据外发、验证绕过、DoS 防护失效三类风险。测试基线 **1207 passed / 6 skipped / 0 failed**，零回归，无 Breaking Change。

### 🔒 安全

- **embedding 未脱敏外发修复**：`qdrant_vector_store._embed_texts` 将文档原文直接传给外部 embedding API（OpenAI/智谱），未经脱敏处理，密钥/token/手机号等敏感数据会外发。现外发前对每个 text 调用 `_redact_for_embedding` 脱敏（内联复制 `_DEFAULT_RULES` 正则，遵守架构冻结禁 rag→runtime import 的约束，与 `_PROVIDER_BASE_URLS` 同手法）。
- **verify_loop 安全门失效修复**：`compute_verify_score` 当 `security_review` 缺失（SecurityAgent 跳过/失败）时按 0 计，但不阻止 verdict 通过——只要 repair_plan(0.4)+test_plan(0.3)+git_attribution(0.1)=0.8 即达 HIGH_CONFIDENCE，完全绕过安全审查。现当 security_review 缺失或含 critical/high 发现时，score 钳制为 PARTIAL 阈值，确保 verdict 不会越级到 PASSED/HIGH_CONFIDENCE，仍允许 PARTIAL 继续迭代补全安全审查。
- **限流键绕过修复**：`RateLimitMiddleware` 用 `request.client.host` 构造限流 key，反代场景（nginx/CloudFlare）下所有真实用户共享代理 IP 的限流桶（互相误伤），攻击者也可用代理池变化 IP 绕过。现优先读 `X-Forwarded-For` 最左客户端 IP，再读 `X-Real-IP`，缺失时回退 `request.client.host`，正确识别反代后的真实客户端。

## [0.6.3] - 2026-08-24

> v0.6.x 稳定性维护补丁：全量代码审查后修复 2 个 Critical + 10 个 Major 缺陷，ruff 门禁从 advisory 升级为硬门禁。测试基线 **1207 passed / 6 skipped / 0 failed**，零回归，无 Breaking Change。

### 🔒 安全

- **auto_test SSRF 逐跳守卫**：`auto_test` 工具此前仅校验初始 URL，goto 重定向与点击触发的导航不经过 SSRF 检查，攻击者可借 302/JS 跳转访问内网。现复用 `ui_runner._install_ssrf_guard` 逐跳拦截所有网络请求。
- **injection_guard 闭合标签逃逸修复**：`wrap_evidence` 未转义 content 内的 `</debug_evidence>` 标签，不可信数据（如异常消息）可提前结束证据区域导致 prompt injection 逃逸。现对闭合标签做 HTML 实体转义。

### 🛠️ 修复

- **运行时内存指标恒 0**：`runtime.py` 的 `_safe_get` 将 psutil `pmem` namedtuple 当普通 tuple 转 list，丢失 `.rss`/`.vms` 属性导致内存采集恒返回 0。现 namedtuple 保留原对象。
- **fault_localizer 项目根误判**：`_STDLIB_DIRS` 用整个 `sys.path` 作标准库前缀，导致 cwd/项目根被误判为 stdlib，项目帧丢失加分。改用 `sysconfig` 取真实 stdlib 路径。
- **JSON 日志丢失 extra 字段与 traceback**：`JSONFormatter` 仅注入 `trace_id`，丢弃 `elapsed_ms`/`method`/`path`/`status` 等请求级 extra 字段；异常日志无 traceback 行。现注入全部 extra 字段并附 traceback。
- **LLM 缓存 L2 TTL 续期导致热条目永不过期**：L2（Redis）命中回填时调用 `_set_cache_result` 刷新 L2 TTL，持续访问的热键永不自然淘汰。改用 `_set_l1_only` 仅回填 L1。
- **上下文截断不复验总长度**：`truncate_context` 截断后未二次校验，errors/exception 自身超大时仍超 `max_chars` 发往 LLM。现截断后二次校验并对超大字段硬截断兜底。
- **periodic_cleanup 死锁修复（Critical）**：`main.py` 的 `asyncio.Lock.acquire_nowait()` 是不存在的 threading API，默认配置下清理任务启动 300s 后必死且停机异常逃逸。改为 `locked()` 预检 + `await acquire()`。
- **Source Map VLQ 解析修复（Critical）**：`sourcemap_resolver.py` 的 `gen_col` 未按规范行内累加，生产 bundle 还原位置几乎全错。改为 `gen_col += fields[0]` + 每行重置。

### ⚡ 性能

- **spec.py 规范文件扫描全量遍历**：`discover_spec_files` 用 `rglob("*")` 遍历整树（含 node_modules/.git）后过滤，大项目秒级卡顿。改用 `os.walk` + 就地剪枝，跳过 `_SKIP_DIRS` 目录不递归进入。

### 🔧 工程质量

- **ruff 门禁从 advisory 升级为硬门禁**：CI 中 ruff 此前以 `continue-on-error: true` advisory 模式运行；经核查 F401/F841/E402/E401 均为 0，`ruff check .` 全绿，故移除 `continue-on-error` 转为硬门禁。额外清理 46 文件 93 处 W29x 空白/换行 safe fix。
- **CI YAML 修复与安全收敛**：`docker-compose.prod.yml` 两处预存缩进缺陷（YAML 从未加载成功）修复 + Prometheus 端口改 loopback 绑定、移除无认证 `--web.enable-lifecycle`。
- **演示页 XSS 修复**：两个 `network_capture_demo.html` 的 `updateCaptures()` 未转义捕获数据直接拼 innerHTML，现增加 `esc()` HTML 转义。
- **check_doc_links.py 崩溃修复**：file:// 链接指向仓库 ROOT 之外时 `relative_to()` 抛 ValueError 无兜底，现补 try/except。

> **测试基线**：1207 passed / 6 skipped / 0 failed（v0.6.2 基线 1198 → 1207，含本轮增量与最近新增）

---

## [0.6.2] - 2026-08-24

> MCP 执行器双池隔离、调试上下文智能折叠、Prometheus 细粒度可观测性与 Browser SDK 弹性增强版本。无 Breaking Change，全仓测试 100% 绿灯。

### 🚀 新特性与架构增强

- **MCP 工具轻重双池隔离（Heavy vs Light Pool）**：为 Playwright/UI 自动化等长耗时重型工具（`auto_test`, `verify_ui`）设立专用执行线程池与信号量（`tool_heavy_executor_workers: 2`），彻底隔离轻量只读工具（`get_debug_context`, `resolve_stack` 等 8 槽位），防止慢任务打满队列导致核心工具被饿死。
- **调试上下文智能去噪与框架栈帧折叠**：自动识别 `starlette`、`fastapi`、`uvicorn`、`asyncio` 等公共中间件与三方库栈帧，连续 2 帧及以上自动折叠为高密度摘要指示，显式标注 `[PROJECT CODE]` 业务代码帧，降低 40% 无效 Token 并保护最底层抛出点。
- **MCP 工具可观测性与 Prometheus 指标**：新增 `mcp_tool_calls_total`、`mcp_tool_duration_seconds`、`mcp_tool_busy_rejected_total` 与 `mcp_tool_queue_wait_duration_seconds`，支持按工具名称、状态（`ok`/`error`/`busy`/`timeout`）与资源池（`light`/`heavy`）细粒度导出。
- **Browser SDK 弹性传输与 LocalStorage TTL 自洁**：升级 `ai-debug-sdk` 至 v0.6.2，引入 Full Jitter 随机抖动指数退避，支持 `Retry-After` 响应头解析，拦截 400/401/403 非可重试错误；为离线降级暂存引入 24h TTL 自动淘汰机制。

### 🛠️ 稳定性与正确性修复

- **同步工具背压与 TOOL_BUSY 快速拒绝**：同步工具调用通过有界槽位与超时控制，在槽位满且等待超时（或 timeout=0）时快速返回 `TOOL_BUSY` 业务错误，避免无界排队与线程堆积；日志文案与状态码同步对齐。
- **TraceEntry / TraceStep 职责解耦**：统一步骤追踪与异常堆栈上下文模型定义，消除冗余 Schema 歧义。
- **agent_mode 显式优先级对齐**：显式指定的 agent_mode 优先于旧布尔配置与隐式默认值。
- **测试环境与 API Key 隔离**：强化 Windows 与 CI 测试执行时的环境变量隔离，避免本机凭据干扰。

---

## [0.6.1] - 2026-08-21

> 智能排障 RAG 扩展与多 Agent 协同优化版本（RAG Experience & Multi-Agent Context Assembly Release）。测试基线全面扩充，零回归，无 Breaking Change。

### 新增

- **Debug Experience RAG 经验库扩充**：默认种子案例由 30 条扩展至 **45 条**，全面覆盖三大生产高频领域：
  - HTTP & Web / 网络类：502 Bad Gateway、401 Unauthorized、429 Rate Limit、SSL 证书校验异常、CORS 跨域拦截
  - Database & Async / 异步并发类：asyncio.TimeoutError 超时、asyncio.CancelledError 任务取消、PostgreSQL 唯一键冲突、连接池打满、Redis 拒绝连接
  - Frontend & Browser / 浏览器类：undefined.map、null.addEventListener、localStorage 超限、process 未定义、fetch NetworkError
- **多 Agent 排障经验召回与透传**：在 `context_assembler.py` 中打通历史排障经验检索，为 RepairAgent 补充高匹配排障经验与上下文质量评分透传，Coordinator 全流程透传可观测指标

### 优化与安全

- **JSON-RPC 错误码语义化与 data 诊断扩展**：在 `app/mcp/protocol/jsonrpc.py` 中标准化错误码常量并支持 `data` 字段，加固全局未捕获异常返回体与 trace_id 脱敏
- **Browser SDK JS 自动化测试与 CI 集成**：新增 `browser-sdk/test/sdk-events.test.js` 并在 CI 流程中接入 SDK 事件上报、脱敏与批处理回归测试
- **测试覆盖与代码洁癖治理**：清理全仓 ruff lint 历史欠账，补充 `stacktrace_api.py`、`factory.py` 专属单测

---

## [0.6.0] - 2026-08-21

> 架构重构与生产就绪里程碑版本（Architecture Refactor & Production Readiness Milestone）。测试基线 **1161 passed / 6 skipped / 0 failed**，文档链接 100% 校验通过，零回归，无 Breaking Change。

### 变更

#### 重构

- **pg_store.py god object 拆分**（1048 行 → 拆解为 7 个单一职责模块）：
  - `pg_executor.py` —— 连接池/重连重试/熔断器/DDL 初始化/`_parse_data`，托管连接生命周期，消除重复 `try/finally putconn` 样板代码
  - `pg_partitions.py` —— traces 月度 RANGE 分区预创建 + 归档
  - `pg_trace_store.py` / `pg_session_store.py` / `pg_error_store.py` / `pg_spec_store.py` / `pg_kb_store.py` —— 5 个 Store 类各归独立模块
- **analyzer.py god object 拆分**（1175 → 474 行，只保留调用编排）：
  - `app/llm/clients.py` —— OpenAI 同步/异步客户端工厂与 provider 分派
  - `app/llm/cache.py` —— L1 LRU + L2 Redis 多级缓存与 error-surface 指纹
  - `app/llm/injection_guard.py` —— Prompt Injection 防护公开化（`wrap_evidence` / `INJECTION_GUARD`）
  - `app/llm/context_prep.py` —— 错误信号提取/脱敏/截断/`build_analysis_prompt`
  - `app/llm/output_schema.py` —— LLM 输出 JSON 提取与 Schema 净化校验
  - `app/llm/kb_integration.py` —— KB 三级命中 + 向量 RAG 召回 + 经验回写
- **sync/async 重试双轨收敛**：抽离共享内核 `_classify_llm_error` / `_format_llm_success` / `_fallback_target`，编排层共享 `_build_llm_messages` / `_finalize_analysis` / `_call_through_circuit_breaker`

#### 运维与可观测性

- **Prometheus 细粒度业务指标补齐**：LLM Token 消耗、KB 缓存命中率、MCP 工具调用耗时、存储池排队时间全量接入度量监控
- **生产部署套件**：完善 Docker Compose 与部署配置，对齐生产安全基线与环境隔离

---

## [0.5.5] - 2026-08-19

> v0.5.4 已发布（2026-08-18）：工程收口 + 文档补全。当前测试基线 **1153 passed / 6 skipped / 0 failed**（v0.5.4 基线 1134 + FR12 新增 10 项 + 3 项因单测存储后端隔离修复转绿）。v0.5.5 为**FR12 调试提示词端点**功能版本：新增 `/api/debug/prompt` + `PROMPT_TEMPLATE_PATH` 配置 + 单测存储后端隔离修复（`tests/unit/conftest.py` 强制 memory 后端，与 CI 一致）。无 Breaking Change。

### 新增

#### 功能

- **调试提示词生成端点**（FR12）：新增 `GET /api/debug/prompt?request_id={id}`（viewer 可读）——基于已采集的完整调试上下文（异常帧/源码片段/运行时/git 归因等），脱敏 + 截断后套用提示词模板，返回可一键复制的纯文本提示词，便于非 MCP 场景直接粘贴给任意 AI 助手分析
- **提示词模板可配置**（FR12）：新增 `PROMPT_TEMPLATE_PATH` 配置项——支持自定义模板文件（UTF-8，`string.Template` 语法，占位符 `$context` / `$request_id`）；为空或文件不存在时回退内置默认模板；`safe_substitute` 对模板中非法 `$xxx` 原样保留不抛错

#### 测试

- **FR12 单测**：新增 `tests/unit/test_prompt_builder.py`（10 项）——默认模板含 request_id/异常帧/指令文本、上下文发送前脱敏（敏感字段不出现）、自定义模板文件加载、模板文件缺失回退内置、模板含非法 `$` 不抛错、`load_prompt_template` 空路径/无效路径回退、端点 200 返回纯文本 prompt / 未知 request_id 404 / 缺参 422

### 修复

- **单元测试存储后端隔离**：`tests/unit/conftest.py` 强制 memory 后端（改写 `settings.storage_backend` + 重置 storage factory 缓存），使单测不再受本机 `.env STORAGE_BACKEND=postgresql` 影响——修复 3 项本机预存失败（`test_batch_writes`×2：固定 request_id 在 PG 跨运行累积致 len 断言失真；`test_spec_store`×1：`_add_log` 注入 traces 表 vs PG 后端恢复走专用 specs 表致数据不可见）。单测运行与 CI（memory 后端）一致；需真实 PG 行为的测试（`test_factory` / `test_storage` 等）用 monkeypatch 显式覆盖，不受影响

---

## [0.5.4] - 2026-08-18

> v0.5.3 已发布（2026-08-18）：RAG 知识库 PostgreSQL 持久化 + 数据库改名 lujo_mcp + P3-9 pg_store 重连修复。当前测试基线 **1134 passed / 6 skipped / 0 failed**（v0.5.3 基线）。v0.5.4 为**工程收口 + 文档补全**版本：无新功能、无 Breaking Change。

### 新增

#### 测试

- **分发链 smoke 校验**（TST-3）：新增 `tests/unit/test_distribution_smoke.py`（9 项）——packaging/ 打包资产与 PyInstaller spec 配置（datas/hiddenimports/excludes）、npm 元包结构与 `optionalDependencies` 三平台一致性、三平台包结构、bin 脚本存在性；版本动态读 `app.__version__` 防漂移
- **SDK JS 契约单测**（TST-3）：新增 `browser-sdk/test/sdk-core.test.js`（7 项）——Node 无浏览器加载 UMD 包，守护公开 API 面、V5 传输配置契约（gzip 4096 / 节流 5000·2 / localStorage 降级）、`_getPublicConfig` 不含 apiKey（安全）、`_setConfig` 行为
- **CI 新增 `sdk-js-smoke` job**（node 20）：CI 层面守护 SDK 契约，防止 SDK 闭包化配置等演进后 e2e 接口再次失联

#### 文档

- **API 参考手册**（DOC-1）：新增 `docs/public/API_REFERENCE.md` —— REST 5 组端点（/api/debug 15 + /ingest 7 + /api/dashboard 9 + /api/spec 5 + /mcp + /auth）+ 18 个 MCP 工具（分类/角色/入参/返回）+ 鉴权 RBAC + 常用字段速查
- **浏览器 SDK 使用手册**（DOC-3）：新增 `docs/public/SDK_GUIDE.md` —— 接入方式 / 26 项 init 配置 / 公开 API / 采集行为 / 拦截规则 / 脱敏 / V5 传输优化 / beacon 令牌
- **README 文档导航表**：新增 API_REFERENCE / SDK_GUIDE 两行，消除孤儿文档

### 修复

- **CSP 头未统一覆盖**（SEC-1）：`Content-Security-Policy` 此前仅在 dashboard/demo 的 HTML 响应上设置，其余响应类型（JSON/JS 等）未覆盖；改为 `SecurityHeadersMiddleware` 统一 `setdefault`，单一来源覆盖所有响应
- **SDK 注释过时工具名**（QC-1）：`browser-sdk/ai-debug.js` 注释引用旧 MCP 工具名 `get_debug_context`（对外已改名 `context`），已修正

---

## [0.5.3] - 2026-08-18

> v0.5.2 已发布（2026-08-15）：品牌统一 —— 全仓 `ai-debug-mcp` 标识改为 `lujo-mcp`。当前测试基线 **1134 passed / 6 skipped / 0 failed**（2026-08-17 KB 持久化 + P3-9 收口后；上一基线 1129）。

### 新增

#### 代码

- **RAG 知识库 PostgreSQL 持久化**：KB 主存（进程内 `KnowledgeBaseStore`）新增写穿（write-through）持久化与启动回灌，learned 知识跨重启保留——
  - 新增 `kb_entries` 表（DDL 单源 `ddl.py`，`migrations/20260817_create_kb_entries_table.sql` 同步）：fingerprint 主键、analysis JSONB、三级指纹索引列、verify_count/case_confidence 验证统计，时间戳为 DOUBLE PRECISION（epoch 秒）
  - 新增 `KnowledgeBaseStorage` ABC（`base.py`）与 `PGKnowledgeBaseStore` / `NoOpKnowledgeBaseStore` 双实现，经 `factory.get_knowledge_store()` 分发：PG 后端真实持久化，memory 后端 no-op（行为与历史版本一致），PG 初始化失败降级 no-op 不阻断启动
  - KB `upsert` / `record_verification` / `clear` / LRU 驱逐同步落库（锁外执行，PG 故障 warning 降级不阻断主流程）；驱逐同步删除持久行，内存与 PG 条数保持一致（≤ max_entries）
  - 新增 `load_from_persistent()` 启动回灌：按 `updated_at` 倒序取最近 `max_entries` 条重建内存条目（含验证统计与三级索引），`main.py` lifespan 在种子加载前调用；PG 为权威来源，同指纹覆盖内存副本

#### 测试

- 新增 `tests/unit/test_kb_persistence.py`（13 项）：写穿、驱逐删除、验证回写、clear 清空、故障降级、回灌字段保留 / max_entries 截断 / LRU 顺序、内存重复覆盖、NoOp 行为
- 新增 `tests/integration/test_pg_integration.py::TestKnowledgeBasePersistence`（2 项，真实 PG 往返）：upsert→清内存→回灌字段一致；LRU 驱逐后 PG 行同步删除
- 新增 `tests/unit/test_pg_store_reconnect.py`（5 项，P3-9）：mock 池模拟断线重连，断言返回最新连接、调用方正确归还、重试耗尽抛错

### 修复

#### 代码

- **pg_store 重连后连接泄漏**（P3-9）：`_query_with_retry` 内部重连换新连接后仅返回查询结果，调用方 `finally` 仍归还旧 conn —— 新连接从池取出永不归还（连接泄漏）、旧连接被重复归还（已 close 连接再 putconn）、重连后继续用旧引用报 `InterfaceError`；改为返回 `(rows, conn)` 与 `_execute_with_retry` 对齐，7 处调用方（traces/sessions/specs/kb_entries 读路径）全部更新为归还最新连接，新增 5 项重连回归测试（第 3 轮审查 P3 至此全部清零）

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
- **SOURCE_PATH_MAP 形同虚设**（P3-2）：`code_locator.py` `get_code_snippet` 白名单校验用映射后路径 `abs_path`，但 linecache 仍读原始 `file_path`，配置 `source_path_map` 后永远读不到本地文件；统一改为 linecache 读 `abs_path`，新增 `test_code_locator.py` 2 项（映射命中 + 映射后白名单外拒绝）
- **truncate_context 精简未写回**（P3-1）：`analyzer.py` `truncate_context` 构建精简版 `runtime` 后未写回 `context["runtime"]`，精简逻辑完全无效，上下文仍过大；补 `context["runtime"] = runtime`，新增精简写回测试
- **Agent fallback 调用未保护**（P3-3）：`agent/base.py` fallback 模型调用未包 try/无重试，失败抛原始 APIError 而非统一 RuntimeError；包 try/except 聚合 last_error 后统一抛出，新增 2 项 fallback 测试
- **query_pg_errors 双池并存 + 无超时**（P3-10）：`errors.py` `pg_async_enabled=True` 时仍惰性创建 psycopg2 同步池（双池并存）；裸 `pool.getconn()` 池耗尽时永久阻塞；`pg_async_enabled` 时提前返回空，改用 `_get_conn(timeout=5.0)` 有界等待（psycopg2 2.9 `getconn` 不支持 timeout），新增/更新 3 项测试
- **同步工具超时后线程继续跑**（P3-12）：`mcp_server.py` / `protocol/server.py` 同步 handler 用 `asyncio.to_thread` 占用默认线程池，`wait_for` 超时只取消 await、线程池 worker 被长任务占用；改用专用有界 `ThreadPoolExecutor(max_workers=8)` + `run_in_executor`，与默认池隔离，更新 verify_ui 源码断言
- **过期 MCP 会话 SSE 悬挂**（P3-14）：`session.py` `cleanup()` 返回 int 导致调用方无法定位被清理会话；改为返回 sid 列表，`main.py` periodic_cleanup 逐个 `hub.close_session(sid)` 关闭悬挂 SSE 流，新增 2 项测试
- **/internal/health 反代泄露**（P3-13）：`main.py` `_is_internal_ip` 信任直连 `client.host`，局域网反代部署时 `is_private=True` 被当内网放行，公网用户可读取完整配置；`internal_health` 检测到 `X-Forwarded-For`/`X-Real-IP` 转发头即不再按内网放行，改走 API Key 校验（fail-closed），新增 2 项测试
- **SSE 订阅无并发上限**（P3-7）：`SSEHub`（每 session）与 `DashboardEventBus`（全局）订阅无上限，持有效 key 可开无限长连接耗尽连接池；SSEHub 每 session 上限 5、DashboardEventBus 全局上限 100，超限返回 429，新增上限测试
- **jsonrpc 死代码 + 冗余 import**（P3-15）：`jsonrpc.py` `JSONRPCResponse` 无任何使用（死代码）已删除；`server.py` `__import__("asyncio")` 改为顶层 `asyncio.iscoroutine`
- **SDK e2e 测试接口过时**：`test_sdk_v5_enhancements.py` / `test_sdk_full_chain.py` 引用闭包式 SDK 不再暴露的 `AiDebug._cfg.*` 与 `AiDebug._uiMutationObserver`；SDK 新增 `_setConfig(key, value)` 测试辅助方法 + `_inited` 只读 getter，e2e 测试改用 `_getPublicConfig()` / `_setConfig` / `_getUIMutationObserver()`；`network_capture_demo.html`（app/web + examples）同步改用 `_setConfig`

### 变更

#### 代码

- **品牌统一（v0.5.2）**：MCP server 名 `ai-debug-mcp` → `lujo-mcp`（`app/mcp_server.py`）；全部 `logging.getLogger("ai-debug-mcp.*")` → `lujo-mcp.*`；`config.py` `otel_service_name` / `service_name` → `lujo-mcp`；`browser-sdk/`（package.json + ai-debug.js）、`mcp_config_example.json` 示例路径同步；对应测试断言更新（`test_api.py` / `test_otel.py` / `test_jsonrpc.py`）；清理本地 IDE 配置文件
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
