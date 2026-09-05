# Release Notes / 发布说明

> 最新版本：**v0.7.4（2026-09-05）**。主题「修复 npm 启动器 P0」：`lujo-mcp-server` 自 v0.6.8 起在所有平台 100% 启动失败（平台白名单前缀不匹配），npm stdio 用户的服务器从未启动过。本版修复并加双重防复发守卫。**v0.6.8~v0.7.3 的 npm 用户必须升级**；HTTP 模式用户不受影响。详见 CHANGELOG.md [0.7.4] 段。npm `latest` → `@lujoai/lujo-mcp@0.7.4`。
>
> **架构冻结（Architecture Frozen）**：Runtime / RAG / Agent 三层分界线已冻结。禁止 Agent 改 RAG；禁止 Runtime 调 RAG/Agent/LLM/MCP；禁止 RAG 调 Agent/Runtime/LLM/MCP。

**Version / 版本**: v0.7.4 ・ **Release Date / 发布日期**: 2026-09-05 ・ **Codename / 代号**: 点火成功 ｜ Launcher Fixed

---

## v0.7.4（2026-09-05）

> 主题「修复 npm 启动器 P0」。`bin/cli.js` / `bin/check.js` 的平台白名单写成无前缀后缀（`win32-x64`），与 `platformPackageName()` 产出的完整包名（`lujo-mcp-win32-x64`）永不相等——`lujo-mcp-server` 在所有平台 100% exit 1（报「暂无官方预编译二进制」），npm stdio 用户自 v0.6.8 起服务器从未启动过。CI 未发现的原因：发布冒烟只测裸二进制、从不测 npm 启动器。

### ⚠️ 升级注意

- **v0.6.8 ~ v0.7.3 的 npm stdio 用户必须升级**：升级后 `lujo-mcp-server` 即刻恢复，客户端配置零改动。
- HTTP 模式（`url` 接入）与源码运行不受此 bug 影响。

### 🔒 修复

- 白名单统一为完整包名 `['lujo-mcp-win32-x64', 'lujo-mcp-linux-x64', 'lujo-mcp-osx-arm64']`（cli.js + check.js 同步修复）。

### ✨ 防复发（双重守卫）

- 分发 smoke 新增**白名单一致性守卫**：启动器脚本的平台白名单必须与实际平台包目录逐名一致，前缀漂移即 CI 红。
- 发布工作流新增**启动器端到端冒烟**：publish 前按真实「npm pack → npm install → initialize 握手」链路验证，此类缺陷今后出不了门。

---

## v0.7.3（2026-09-05）

## v0.7.3（2026-09-05）

> 主题「让 AI 真正用起来工具」。修复宿主 AI（Claude / Cursor / Trae / Qoder 等）很少主动调用 Lujo-MCP 工具的问题——不是协议实现缺陷，而是 `tools/list` 货架对 AI 不友好：查询工具全要 AI 拿不到的 ID、stacktrace 空参是死路、文档教 AI 调不存在的工具名。**零 Breaking Change、零新增配置**。

### ⚠️ 升级注意

- **无 Breaking Change**：直接升级即可。唯一可见变化：`tools/list` 默认清单不再包含 4 个 SDK 上报工具（`ingest_network` / `ingest_error` / `ingest_console` / `ingest_silent_failure`）——它们仍可 `tools/call` 按名调用，Browser SDK 与 REST 上报链路完全不受影响。
- **数据边界不变**：stdio 纯 MCP 接入不接收浏览器 HTTP 上报；浏览器运行现场仍需「Browser SDK + HTTP 服务 + `/ingest`」。

### ✨ 新增：统一诊断入口 `diagnose_issue`（本次核心）

- 用户说「刚才页面报错了」「接口返回 500」「点击没反应」「测试失败帮我排查」时，宿主 AI 现在有一个明确的优先调用入口：**不需要任何 request_id**，自动定位最近一次真实错误并一次性返回完整调试上下文（异常堆栈+源码片段+网络链+UI 事件+git 归因）。
- 三种用法：无参数=最近错误；`query`=关键词匹配（如「登录失败」「500」）；`request_id`=精确查询；支持 `session_id` 会话隔离。
- 无数据时返回 `found=false` + `setup_hint` + `next_step`（引导 AI 向用户解释数据从哪来、下一步做什么），绝不返回空对象。

### ✨ 新增：近期错误查询工具

- `list_recent_traces` / `search_logs` 注册为只读 MCP 工具（此前仅为内部函数，文档与实际工具名脱节）；原 Python 函数签名不变；viewer 角色可读。
- 顺带修复两工具带 `session_id` 时泄漏全局存储摘要的会话隔离缺陷。

### 🔒 修复

- `stacktrace` 无 request_id 时从「读本进程异常（永远为空的死路）」改为回退读取 errors 存储最近一条错误；无错误时返回消息与字段结构与旧版一致。
- 13 个 Agent 工具 description 补齐触发条件（何时调用/是否需要 ID/是否先调 diagnose_issue/何时不调）。

### 📖 文档

- README / API_REFERENCE / DESIGN / SDK_GUIDE 全部与真实工具名对齐；API_REFERENCE 增加公开清单口径说明（工具清单以 `tools/list` 实际返回为准）。

---

## v0.7.2（2026-09-03）

> 主题「分发统一 + 前端现场信噪比」。坚守「MCP Server 调试上下文基础设施」定位：不加 Agent 能力，只把「喂现场」做得更准、接入门槛压到更低。**零 Breaking Change、零新增配置**。

### ⚠️ 升级注意

- **无 Breaking Change**：直接升级即可；npm 主包 `files` 扩展只增不减，既有 `bin` 引用不受影响。

### ✨ 新增：Browser SDK 随主包分发（单包交付 + CDN 直引）

- npm 主包 `files` 扩展为 `["bin", "browser-sdk"]`：装一个 `@lujoai/lujo-mcp`，后端 MCP Server 与前端探针全都有。
- 前端页面可直接 CDN 一行引用，无需手动拷贝文件：
  ```html
  <script src="https://cdn.jsdelivr.net/npm/@lujoai/lujo-mcp/browser-sdk/ai-debug.js"></script>
  <script>window.AiDebug.init({ endpoint: "http://localhost:8000" });</script>
  ```
- **防漂移双重守卫**：prepublish 脚本校验分发副本与仓库源逐字节一致；分发 smoke 测试哈希比对，漂移即 CI 红。

### ✨ 增强：unhandledrejection 非标准 reason 堆栈兜底

- 此前 `Promise.reject("string")` / `Promise.reject({code: 401})` 等非 Error 拒因没有 `stack`，上报的 `frames` 为空数组，AI 只见错误文字、丢失抛出位置。
- 现结构化解析对象载荷（`name`/`type`/`message`），无堆栈可解析时合成含页面 URL 的兜底帧；新增 JS 契约测试 `sdk-rejection.test.js`（4 项，全部经 `/ingest/batch` 真实批量通道验证）。

### 📖 文档：README 面向新用户全面重构

- **30 秒极速接入**：首推 `npx -y @lujoai/lujo-mcp` 免安装直跑（规避 Claude Desktop 等 GUI 客户端不继承终端 PATH 的 `command not found` 高频问题）。
- **5 分钟真实调试闭环**：启动服务 → 两行 SDK 引入 → 触发异常 → AI 一键诊断（对齐 DEMO 官方验证路径，修正此前文档中不可用的 CDN 假链接与 stdio 模式下走不通的流程）。
- **能力阶梯**：明确「零配置即可用」与「配置 1 个 API Key 解锁」的边界；新增三大高频排错 FAQ。
- 版本历史下沉 RELEASE_NOTES / CHANGELOG，首页回归极简。

---

## v0.7.1（2026-08-31）

> 主题「Minor 债务批量清理」。第 6/7 轮代码审查挂账的 Minor 问题分 15 个批次全部清零（78 项真 bug 修复，含外部独立复核验证闭环）。**零回归铁律：全部既有路径行为不变，仅修复此前出错/失真/盲区的路径；无 Breaking Change、无新增配置**。测试基线：unit **1479 / 0 failed / 6 skipped**（junit 权威计数）、integration **115 / 0 failed**、e2e **10 / 0 / 1**、Browser SDK JS **50/50**、ruff 硬门禁全绿。

### ⚠️ 升级注意

- **无 Breaking Change**：纯修复与防御性加固，零配置变更，直接升级即可。

---

## v0.7.0（2026-08-29）

> 主题「稳定 + 可观测 + 债务清理」。不含 P0/P1 缺陷修复（缺陷治理已在 v0.6.x 收口）——本版交付 KB 学习闭环可观测性（唯一面向用户的新能力）+ Minor 大扫除两批 20 项 + 工程卫生，并首次达成 integration 套件全绿。测试基线：unit **1409 tests / 0 failed / 6 skipped**、integration **113 tests / 0 failed**（96 passed / 17 skipped，首次全绿）、Browser SDK JS **47/47**、e2e 9 passed。**无 Breaking Change，配置全部向后兼容**。

### ⚠️ 升级注意

1. **npm 包要求 Node >=18**（`engines` 自 `>=16` 收紧）：Node 16 已 EOL；`npm install` 在旧 Node 上会收到 engine 警告。
2. **无 breaking change**：KB 可观测性指标/端点为纯新增，配置全部向后兼容。

### ✨ 新增：KB 学习闭环可观测性

- **Prometheus 指标**：`kb_hits_total{level}`（L1 精确指纹 / L1.5 归一化 / L2 类型级 / vector_rag / miss）、`kb_writeback_total{kind,status}`（分析回写 / verify 写回）、`kb_experience_recall_total{status}`（经验召回）——闭环"在学"的量化证明，miss 单独计数。
- **`GET /api/dashboard/kb-stats`**（viewer 可读）：条目总数 / seed vs llm 来源分布 / 学习占比 / 重复验证条数 + 指标快照；数据源失败静默降级零值。
- **Dashboard「KB Learning Loop」面板**：命中分布与回写计数一目了然。

### 🔒 修复与加固（精选用户可感知项）

- SDK 上报 extra 含**循环引用对象不再崩溃**（此前抛 RangeError 整条上报丢失）。
- **相似域名不再吞数据**：`http://endpoint.evil.com` 此前被误判为 SDK 自请求而静默跳过采集。
- **中文页面 beacon 上报不再超限丢数据**：64KB 门改按 UTF-8 字节判定（此前按字符数，3 万汉字 ≈ 9 万字节被误放行）。
- spec PATCH **字段白名单**：未知字段不再写入规范持久化。
- `.env.example` 补全安全配置样例（API_KEYS / RBAC / beacon 令牌），防样例部署漏配。

### 🧹 清理与工程

- Minor 大扫除第二批：零消费方模型（agent/schemas.py）、不可达 beacon 分支、`PROMETHEUS_ENABLED` 死配置删除；多处 docstring/文档口径与实现对齐（agent_mode 语义、种子计数、SDK 采样率说明）。
- 工程卫生：npm 5 包 `engines >=18` 统一；CI concurrency + pip cache；release 发布流程 concurrency（发布不可中途取消）；pytest `--strict-markers`。
- **integration 套件首次全绿**：修复 11 个历史盲区失败（agent 门控 patch 遗留 / C2 子进程化对测试基建的影响 / 断言适配 / 队列串扰）。

### 📊 测试与质量

- unit **1409 tests / 0 failed / 6 skipped**（junit 权威计数）；integration **113 tests / 0 failed**（96 passed / 17 skipped）；SDK JS 47/47；e2e 9 passed / 1 skipped；ruff 全绿。完整技术细节见 [CHANGELOG](./CHANGELOG.md) 的 0.7.0 章节。

---

## v0.6.9（2026-08-29）

> 第 7 轮全量代码审查修复发布。修复 **P1 安全×2**（XFF 反代限流绕过复活 / 异常指纹"算好被丢"致 KB 学习闭环全死）+ **Major·P2 22 项** + **第 6 轮遗留 P2 全部收口**（B2/B4/B5、C2 重活僵尸线程根治、G3 SDK destroy() 与泄漏）。测试基线 unit v0.6.8 基线 1298 → **1386**（+88 / 6 skipped / 0 failed）；Browser SDK JS 35 → **42**（+7）；ruff 硬门禁全绿，零回归。

### ⚠️ 升级注意事项（Upgrade Notes）

1. **反代部署仍须配置 `TRUSTED_PROXY_COUNT`**（v0.6.8 引入，语义不变）：本版修复该配置的 off-by-one 解析错误——v0.6.8 配置 N>0 后实际取到客户端可伪造的 XFF 前缀区，轮换伪造 IP 即可绕过限流（配置形同虚设）；现按标准 `$proxy_add_x_forwarded_for` 语义正确解析真实客户端 IP。已按 v0.6.8 发布说明配置的部署升级后无需改动配置值。
2. **heavy 工具（verify_ui / auto_test 等浏览器自动化）改为每次调用独立子进程执行**：僵尸线程打满 heavy 工具池（恒 TOOL_BUSY/TOOL_TIMEOUT、无自愈）的问题根治，超时强杀可靠回收。代价是每次调用有子进程冷启动开销（Windows 用 spawn，略增）；调用方接口不变。
3. **SDK 新增 `AiDebug.destroy()`**：页面卸载 / Vite HMR 热更新场景建议显式调用，避免监听器叠加与事件重复上报；未调用不影响现有功能（SDK 内部已自带去重表过期与上限清理）。

### 🔒 安全

- **XFF 可信代理解析 off-by-one（R7-P1-1）**：`TRUSTED_PROXY_COUNT` 候选取值多退一格落入客户端可伪造前缀区，反代配置形同虚设；现按标准 `$proxy_add_x_forwarded_for` 语义解析，并按真实代理语义重写测试 fixture + 伪造前缀攻击回归。
- **SecurityAgent severity 归一化 fail-open（R7-S1）**：`critical` / `High` 被静默降成 `low` 架空 verify_loop 安全门；现 fail-safe 归一（非法/缺失值保守按 high，宁可多拦不漏放）。
- **redaction 误伤 author（R7-S2）**：git blame 归因字段不再被 `"auth"` 子串脱敏掩码；`authorization` 头仍按敏感处理。

### 🏗️ 结构与可靠性

- **KB 学习闭环复活（R7-P1-2）**：异常指纹三断点修复（builder 注入 / 落库持久化+回读重算 / capture_exception 产指纹）——KB 三级命中、向量 RAG、分析回写、verify 写回、经验召回整条链路恢复，减少无效 LLM 调用。
- **重活僵尸线程根治（C2）**：heavy 工具改独立子进程隔离 + 超时强杀（`app/mcp/protocol/heavy_process.py`），不再有恒 TOOL_BUSY 的无自愈状态（运维注意见升级注意事项 2）。
- 其余：槽位取消泄漏（R7-T1）、连接池中毒（R7-T2）、sys.path 误判框架帧（R7-T3）、向量库 delete 同步（R7-T4）、InProcess 幂等覆盖（R7-T5）、畸形 JSON 400（R7-A2）、绑定检测 IPv6（R7-A1）、SSE 缓冲头（R7-A3）、dashboard 缓存分档（R7-A4）、stdio 线程池退出关闭（R7-A5）、断言引擎 bool/str status（R7-V1/V2）、归档失败回滚（R7-V3）、async-mix fail-fast（R7-V4）、评分契约修复（R7-Q1~Q5）、auto_test 浏览器必关。

### 🛠️ SDK

- 新增 `destroy()` 销毁接口 + 去重表过期/上限清理（G3）；压缩路径节流失效修复（R7-G1，`maxBatchesPerWindow` 配置此前在压缩开启时实际失效）。

### 📊 测试与质量

- unit 1298 → **1386**（+88，含真实生产链路契约测试：指纹闭环 / XFF 伪造前缀攻击 / 安全门端到端）；SDK JS 35 → **42**（+7）；ruff 全绿，零回归。完整技术细节见 [CHANGELOG](./CHANGELOG.md) 的 0.6.9 章节。

---

## v0.6.8（2026-08-27）

> 第 6 轮全量代码审查修复的发布版。修复 **P0 安全×5**（CR-1 verify_loop 安全门字段错配 / CR-2 脱敏复合键缺口 / CR-3 SDK 毒批循环 / A1 XFF 限流绕过 / A2 add_log 明文入库）+ 顺带 G1 + 测试基础设施 2 项 + **P1 十四项全量**（A3/A4、B1/B3、C1/C3/C4/C5、D1/D2/D3、E1、F3、G2）+ **P2 安全/可靠性六项**（D4/D5/D6/E2/F1/F2）+ **P2 发布工程四项**（F4-F7）。测试基线 v0.6.7 基线 1231 → 1290（P0+P1 +59）→ **1298**（P2 六项 +8 / 6 skipped）；Browser SDK JS **35 passed**；本地全量 **1377+ passed / 0 failed**。无 Breaking Change。

### ⚠️ 部署注意（反代必须配置）

- **新增 `TRUSTED_PROXY_COUNT`**：默认 `0` = 不信任 X-Forwarded-For / X-Real-IP（首段可被客户端伪造，历史上轮换伪造 IP 即可绕过限流），一律用直连对端 IP。**反代部署（nginx/ALB/Cloudflare tunnel 等）升级 v0.6.8 后必须按实际代理层数配置该值**；未配置则所有真实用户共享代理 IP 的限流桶（互相误伤，但限流仍有效、无安全回退）。配置见 `.env.example`。
- **`METRICS_AUTH_ENABLED=False`（默认）时 `/metrics` 解除全局鉴权**（供监控栈无凭据抓取，应只发布到可信内网）；`True` 时保留全局中间件保护。
- **生产 compose 端口已回环绑定 `127.0.0.1`**：TLS 终止改由同宿主前置反代（nginx/ALB/tunnel）完成，杜绝 API Key 明文 HTTP 全网暴露。

### 行为变更（Breaking Note）

1. **MCP SSE 流新增 15s 心跳**：GET /mcp 流空闲时每 15s 发送 `: ping` 注释行（对客户端透明），防止反代空闲断流并刷新会话活跃时间。纯解析 SSE data 的客户端不受影响。
2. **tools/call 参数校验收紧**：缺 required 参数 / 参数类型错误现返回 `-32602 INVALID_PARAMS`（此前落入 TOOL_INTERNAL）。LLM 客户端可据此自纠错重试；合法参数行为不变（未声明额外参数不拒绝）。
3. **SDK 错误类上报豁免采样**：`sampleRate` 不再作用于 `reportError` / `reportSilentFailure` / `reportNetworkError` 与全局异常捕获（window.onerror / unhandledrejection）——错误类事件必达；遥测类（network 自动捕获 / ui-event / console）采样行为不变。
4. **发布流程加固**：release-npm workflow_dispatch 输入不再直接内插 shell（F4）；pyinstaller 固定 `6.11.1`（F5）；非支持平台（linux-arm64 / osx-x64）安装不再硬失败且指引改进（F6）；build 各平台原生对打包产物跑 stdio 冒烟（F7）。

### 未完成项（并入 v0.7.0）

- **P2 待排期 5 项**：B2/B4/B5/C2/G3 —— 并入 v0.7.0 取舍。完整清单见内部 CODE_REVIEW 第 6 轮记录。
- **Minor 90+ 项**：并入 v0.7.0 清理（内部索引）。

---

## v0.6.7（2026-08-25）

### 中文版本

#### 📋 版本概述

v0.6.7 是 v0.6.x 线的**正确性补丁**版本：修复全量代码审查第三档 8 项中的 7 项 Major 正确性缺陷（Browser SDK 数据采集三件套 + Python 侧四项），第 8 项经 Python 3.12 `asyncio.wait_for` 语义分析确认不可复现，标注待复核未做改动。无 Breaking Change，零回归。测试基线 1221 → **1231 passed / 6 skipped / 0 failed**（Python 新增 10 项），Browser SDK JS 25 → **29 passed**（新增 4 项）。

#### 🛠️ Browser SDK 数据采集（`browser-sdk/ai-debug.js`）

- **gzip 回退乱码修复**：gzip 压缩发送失败回退时，旧实现把 gzip 二进制字节当文本存 localStorage（恢复时 `JSON.parse` 必然失败），400/415（接收端不支持 gzip）时直接丢数据。现原始明文 `body` 全程透传：localStorage 回退存明文、400/415 用明文重发一次。
- **pagehide 丢数据修复**：页面关闭/隐藏瞬间，节流暂存队列里攒的批次走 `setTimeout` 延迟发送（unload 后定时器不触发），数据直接丢失。现 beacon 路径同步排空暂存队列 + 当前批次（sendBeacon 或同步 XHR），绝不延迟到定时器。
- **节流齐发修复**：旧实现每条被节流的批次各自 `setTimeout`，延迟差毫秒级导致同一时刻齐射（节流失效反而形成请求尖峰）。现改为单一定时器逐条发送，间隔 `ceil(节流窗口 / 窗口最大批次数)` 错开发送。

#### 🛠️ Python 侧

- **LLM 缓存指纹碰撞修复**（`app/llm/cache.py`）：`_compute_context_fingerprint` 用 `"|"` / `":"` 裸拼接字段，字段值内含分隔符时不同上下文拼出同一字符串（如 `exc_type="A|B"` 与 `message="B|C"`），且 `[:16]` 截断放大碰撞面——缓存会返回错误分析结果。现改为 `json.dumps` 结构化序列化 + 完整 sha256 摘要。
- **流式路径绕过熔断修复**（`app/llm/analyzer.py`）：`analyze_stream` / `analyze_stream_async` 此前完全绕过熔断器——非流式路径熔断开启时 fallback，流式路径继续直打 LLM。现两条流式路径接入同一熔断状态机（OPEN 时 yield 结构化 fallback、成功/失败计入熔断计数），异步路径锁临界区经 `asyncio.to_thread` 执行避免阻塞事件循环。
- **smoke_test 死锁修复**（`scripts/mcp_smoke_test.py`）：`stdout.readline()` 无超时（服务端挂死时冒烟脚本永久阻塞）、`stderr=PIPE` 从不排空（管道缓冲写满导致子进程阻塞）。现读超时 10s 兜底 + 后台线程排空 stderr + EOF 哨兵。
- **sourcemap 缓存键版本混淆修复**（`app/runtime/collectors/sourcemap_store.py`）：仅以 artifact 为存储键，同 artifact 不同 release（bundle 版本）的 source map 互相覆盖——旧版本 map 会解析新版本堆栈，还原位置错误。现用 NUL 分隔符把 release 并入存储键，上传/查找/解析/API 全链路透传 release。

#### 📊 测试与质量

- **测试基线**：1231 passed / 6 skipped / 0 failed（Python 新增 10 项：指纹碰撞 2 + 流式熔断 3 + smoke_test 3 + sourcemap 2）；Browser SDK JS 新增 4 项（`browser-sdk/test/sdk-transport-fixes.test.js`），JS 合计 29 passed
- **待复核项**：verify_loop 单轮超时覆盖结果一项经 Python 3.12 `asyncio.wait_for` / `asyncio.timeout` 源码级分析确认不可复现（协程在取消生效前完成则正常返回，`except TimeoutError` 分支仅在确无结果时执行），未做改动

### English Version

#### 📋 Release Overview

v0.6.7 is the **correctness patch** release on the v0.6.x line: it fixes 7 of the 8 third-tier Major correctness defects (Browser SDK transport trio + 4 Python-side items). The 8th was confirmed non-reproducible under Python 3.12 `asyncio.wait_for` semantics and is flagged for re-review without code changes. No breaking changes, zero regression. Test baseline 1221 → **1231 passed / 6 skipped / 0 failed** (10 new Python tests); Browser SDK JS 25 → **29 passed** (4 new).

#### 🛠️ Browser SDK Data Collection

- **gzip fallback corruption fix**: on gzip send failure the old code stored raw gzip bytes as text in localStorage (guaranteeing `JSON.parse` failure on restore) and dropped data outright on 400/415. The original plaintext `body` is now threaded through: localStorage stores plaintext, and 400/415 retries once uncompressed.
- **pagehide data-loss fix**: on page hide/close, throttled batches were deferred via `setTimeout`, which never fires after unload. The beacon path now synchronously drains the pending queue plus the current batch (sendBeacon or sync XHR).
- **Throttle burst fix**: each throttled batch previously got its own `setTimeout` milliseconds apart, firing simultaneously and creating a request spike. Now a single pacer timer sends one batch at a time, spaced `ceil(window / maxBatchesPerWindow)`.

#### 🛠️ Python Side

- **LLM cache fingerprint collision fix** (`app/llm/cache.py`): bare `"|"` / `":"` concatenation let different contexts produce identical fingerprints when field values contained the separators, and `[:16]` truncation widened the collision surface — returning wrong cached analyses. Now uses `json.dumps` structured serialization plus a full sha256 digest.
- **Streaming bypassed circuit breaker fix** (`app/llm/analyzer.py`): `analyze_stream` / `analyze_stream_async` bypassed the breaker entirely while non-streaming paths fell back. Both streaming paths now share the same breaker state machine (fallback on OPEN, success/failure accounted), with async lock sections offloaded via `asyncio.to_thread`.
- **smoke_test deadlock fix** (`scripts/mcp_smoke_test.py`): `stdout.readline()` had no timeout and `stderr=PIPE` was never drained (pipe buffer saturation blocked the child). Now a 10s read timeout, a background stderr drainer, and an EOF sentinel.
- **sourcemap cache-key version confusion fix** (`app/runtime/collectors/sourcemap_store.py`): keying only by artifact let source maps of different releases overwrite each other, so a stale map resolved new stacks to wrong locations. `release` is now folded into the storage key (NUL-separated) and threaded through upload/lookup/resolve/API paths.

---

## v0.6.6（2026-08-24）

### 中文版本

#### 📋 版本概述

v0.6.6 是 v0.6.5 的**重发版本**（0.6.5 因 `package.json` 编码损坏导致发布中断，npm 上从未存在完整的 0.6.5，故跳版重发）。内容为 v0.6.x 可用性补丁：修复 4 个可用性组 Major 缺陷（stdio 坏输入杀服务、超时背压槽位竞态、事件循环三处阻塞、async 工具绕过双池），并加固 JSON-RPC 协议层输入校验。测试基线 **1221 passed / 6 skipped / 0 failed**（新增 14 项），零回归，无 Breaking Change。

#### 🛠️ 可用性

- **stdio 坏输入杀服务修复**：单条畸形输入即可终止 stdio 服务进程，MCP 客户端连接被整体切断；现坏帧被隔离处理，服务持续可用。
- **超时背压槽位竞态修复**：同步工具槽位在超时路径下存在竞态，可能导致槽位泄漏、并发能力逐步退化；现槽位释放与超时判定原子化。
- **事件循环阻塞修复（三处）**：KB 写回、熔断器锁临界区等同步 IO 直接在事件循环线程执行，阻塞全部并发请求；现统一移入线程池（`asyncio.to_thread`）。
- **async 工具绕过双池修复**：异步工具未经双池门控，可绕过 heavy/light 隔离打满资源；现纳入统一门控。

#### 🛡️ 协议加固

- **JSON-RPC 输入校验**：`method` / `id` 字段类型与取值校验前置，非法请求快速返回标准错误体而非进入业务链路。

### English Version

#### 📋 Release Overview

v0.6.6 is a **respin of v0.6.5** (0.6.5 aborted mid-publish due to `package.json` encoding corruption and never existed completely on npm, so the version was skipped). Content is the v0.6.x availability patch: 4 availability-group Major fixes (stdio bad input killing the service, timeout backpressure slot race, three event-loop blocking sites, async tools bypassing the dual pool) plus JSON-RPC input validation hardening. Test baseline **1221 passed / 6 skipped / 0 failed** (14 new tests), zero regression, no breaking changes.

---

## v0.6.4（2026-08-24）

### 中文版本

#### 📋 版本概述

v0.6.4 是 v0.6.x 线的**安全补丁**版本：修复 3 个安全组 Major 缺陷，覆盖数据外发、验证绕过、DoS 防护失效三类风险。测试基线 **1207 passed / 6 skipped / 0 failed**，零回归，无 Breaking Change。

#### 🔒 安全

- **embedding 未脱敏外发修复**：`qdrant_vector_store._embed_texts` 将文档原文直接传给外部 embedding API（OpenAI / 智谱），未经脱敏处理，密钥 / token / 手机号等敏感数据会外发。现外发前对每个 text 调用 `_redact_for_embedding` 脱敏（内联复制脱敏规则正则，遵守架构冻结禁 rag→runtime import 的约束）。
- **verify_loop 安全门失效修复**：`compute_verify_score` 当 `security_review` 缺失（SecurityAgent 跳过/失败）时按 0 计，但不阻止 verdict 通过——只要 repair_plan(0.4) + test_plan(0.3) + git_attribution(0.1) = 0.8 即达 HIGH_CONFIDENCE，完全绕过安全审查。现当 security_review 缺失或含 critical/high 发现时，score 钳制为 PARTIAL 阈值，确保 verdict 不会越级到 PASSED / HIGH_CONFIDENCE，仍允许 PARTIAL 继续迭代补全安全审查。
- **限流键绕过修复**：`RateLimitMiddleware` 用 `request.client.host` 构造限流 key，反代场景（nginx / CloudFlare）下所有真实用户共享代理 IP 的限流桶（互相误伤），攻击者也可用代理池变化 IP 绕过。现优先读 `X-Forwarded-For` 最左客户端 IP，再读 `X-Real-IP`，缺失时回退 `request.client.host`。

### English Version

#### 📋 Release Overview

v0.6.4 is the **security patch** release on the v0.6.x line: 3 security-group Major fixes covering data exfiltration, verification bypass, and DoS-protection failure. Test baseline **1207 passed / 6 skipped / 0 failed**, zero regression, no breaking changes.

- **Unredacted embedding exfiltration**: `qdrant_vector_store._embed_texts` sent raw document text to external embedding APIs; each text is now redacted before egress.
- **verify_loop security gate bypass**: a missing `security_review` scored 0 but did not block the verdict, so 0.8 from other dimensions still reached HIGH_CONFIDENCE. Scores are now clamped to the PARTIAL threshold when security review is missing or contains critical/high findings.
- **Rate-limit key bypass**: the limiter keyed on `request.client.host`, collapsing all users behind a reverse proxy into one bucket. It now prefers the leftmost `X-Forwarded-For` client IP, then `X-Real-IP`, falling back to `request.client.host`.

---

## v0.6.3（2026-08-24）

### 中文版本

#### 📋 版本概述

v0.6.3 是 v0.6.x 线的**稳定性维护补丁**：全量代码审查后修复 2 个 Critical + 10 个 Major 缺陷，ruff 门禁从 advisory 升级为硬门禁。测试基线 v0.6.2 的 1198 → **1207 passed / 6 skipped / 0 failed**，零回归，无 Breaking Change。

#### 🔒 安全

- **auto_test SSRF 逐跳守卫**：`auto_test` 工具此前仅校验初始 URL，goto 重定向与点击触发的导航不经过 SSRF 检查，攻击者可借 302 / JS 跳转访问内网。现复用 `ui_runner._install_ssrf_guard` 逐跳拦截所有网络请求。
- **injection_guard 闭合标签逃逸修复**：`wrap_evidence` 未转义 content 内的 `</debug_evidence>` 标签，不可信数据（如异常消息）可提前结束证据区域导致 prompt injection 逃逸。现对闭合标签做 HTML 实体转义。

#### 🛠️ 修复（含 2 个 Critical）

- **periodic_cleanup 死锁（Critical）**：`main.py` 使用了不存在的 `asyncio.Lock.acquire_nowait()`（threading API），默认配置下清理任务启动 300s 后必死且停机异常逃逸。改为 `locked()` 预检 + `await acquire()`。
- **Source Map VLQ 解析错误（Critical）**：`sourcemap_resolver.py` 的 `gen_col` 未按规范做行内累加，生产 bundle 还原位置几乎全错。改为 `gen_col += fields[0]` + 每行重置。
- **运行时内存指标恒 0**：`runtime.py` 的 `_safe_get` 把 psutil `pmem` namedtuple 当普通 tuple 转 list，丢失 `.rss` / `.vms` 属性。现保留原 namedtuple。
- **fault_localizer 项目根误判**：`_STDLIB_DIRS` 用整个 `sys.path` 作标准库前缀，导致 cwd / 项目根被误判为 stdlib、项目帧丢失加分。改用 `sysconfig` 取真实 stdlib 路径。
- **JSON 日志丢失 extra 字段与 traceback**：`JSONFormatter` 仅注入 `trace_id`，丢弃 `elapsed_ms` / `method` / `path` / `status` 等请求级字段，异常日志无 traceback。现注入全部 extra 字段并附 traceback。
- **LLM 缓存 L2 TTL 续期致热条目永不过期**：L2（Redis）命中回填时调用 `_set_cache_result` 刷新 L2 TTL，热键永不自然淘汰。改用 `_set_l1_only` 仅回填 L1。
- **上下文截断不复验总长度**：`truncate_context` 截断后未二次校验，errors / exception 自身超大时仍超 `max_chars` 发往 LLM。现二次校验并对超大字段硬截断兜底。

#### ⚡ 性能

- **spec 文件扫描全量遍历**：`discover_spec_files` 用 `rglob("*")` 遍历整树（含 node_modules / .git）后过滤，大项目秒级卡顿。改用 `os.walk` + 就地剪枝跳过 `_SKIP_DIRS`。

#### 🔧 工程质量

- **ruff 门禁从 advisory 升级为硬门禁**：核查 F401 / F841 / E402 / E401 均为 0、`ruff check .` 全绿后移除 `continue-on-error`；额外清理 46 文件 93 处空白/换行 safe fix。
- **CI YAML 修复与安全收敛**：`docker-compose.prod.yml` 两处预存缩进缺陷（YAML 从未加载成功）修复；Prometheus 端口改 loopback 绑定、移除无认证 `--web.enable-lifecycle`。
- **演示页 XSS 修复**：`network_capture_demo.html` 的 `updateCaptures()` 未转义捕获数据直接拼 innerHTML，现增加 `esc()` HTML 转义。
- **check_doc_links.py 崩溃修复**：file:// 链接指向仓库 ROOT 之外时 `relative_to()` 抛 ValueError 无兜底，现补 try/except。

### English Version

#### 📋 Release Overview

v0.6.3 is the **stability maintenance patch** on the v0.6.x line: 2 Critical + 10 Major fixes from a full code review, and the ruff gate was promoted from advisory to a hard gate. Test baseline 1198 → **1207 passed / 6 skipped / 0 failed**, zero regression, no breaking changes.

- **Critical — periodic_cleanup deadlock**: `main.py` called the nonexistent `asyncio.Lock.acquire_nowait()`; the cleanup task died 300s after startup under default config. Replaced with `locked()` pre-check + `await acquire()`.
- **Critical — Source Map VLQ parsing**: `gen_col` was not accumulated per line as the spec requires, making production bundle resolution almost always wrong. Fixed to `gen_col += fields[0]` with per-line reset.
- **Security**: per-hop SSRF guard for `auto_test` navigations; HTML-escaped closing tags in `wrap_evidence` to prevent prompt-injection escape.
- **Fixes**: psutil namedtuple preserved for memory metrics; real stdlib paths via `sysconfig` in fault_localizer; JSON logs now carry all extra fields plus traceback; L2 cache no longer refreshes TTL on read-through; context truncation re-validates total length.
- **Performance**: spec file discovery switched to `os.walk` with in-place pruning.
- **Engineering**: ruff promoted to a hard gate; prod compose YAML indentation fixed with Prometheus bound to loopback; demo page XSS escaped; doc-link checker no longer crashes on out-of-root `file://` links.

---

## v0.6.2（2026-08-24）

### 中文版本

v0.6.2 是 Lujo-MCP 聚焦于**高可用 MCP 工具执行体系与高信息密度调试上下文**的正式发布版本：

#### 🚀 核心新特性

1. **MCP 同步工具双池隔离（Heavy vs Light Pool）**：
   - 引入独立执行线程池与信号量（`tool_heavy_executor_workers: 2`），Playwright / UI 自动化测试长耗时任务与轻量级只读工具（`get_debug_context`、`resolve_stack` 等 8 槽位）物理隔离，杜绝慢工具打满队列导致的工具饥饿。
2. **调试上下文智能去噪与框架栈帧折叠**：
   - 智能识别并折叠 Starlette/FastAPI/Uvicorn/Asyncio 等公共中间件内部栈帧，显式标记 `[PROJECT CODE]` 业务代码帧，减少 40% 无效 Token，同时保全最内层抛出点。
3. **MCP 工具 Prometheus 与 OTel 可观测性**：
   - 细粒度导出 `mcp_tool_calls_total`、`mcp_tool_duration_seconds`、`mcp_tool_busy_rejected_total` 与 `mcp_tool_queue_wait_duration_seconds` 指标。
4. **Browser SDK 弹性增强与 LocalStorage TTL 自洁**：
   - 同步升级至 v0.6.2，引入 Full Jitter 随机抖动退避与 `Retry-After` 头感知，增加离线暂存 24h TTL 自动淘汰。

---

### English Version

v0.6.2 focuses on **high-availability MCP tool execution infrastructure and high-information-density debug context**:

#### 🚀 Key Features

1. **Heavy vs Light Tool Dual-Pool Isolation**:
   - Dedicated thread pool and semaphores for heavy Playwright/UI tools (`tool_heavy_executor_workers: 2`), completely isolating lightweight read-only tools (`get_debug_context`, `resolve_stack`) to eliminate tool starvation.
2. **Intelligent Framework Stack Frame Folding & Denoising**:
   - Automatically folds repetitive framework middleware frames (Starlette, FastAPI, Uvicorn, Asyncio), marks project code with `[PROJECT CODE]`, saves 40% tokens, and guarantees root cause frame preservation.
3. **MCP Tool Prometheus & OTel Observability**:
   - Exports `mcp_tool_calls_total`, `mcp_tool_duration_seconds`, `mcp_tool_busy_rejected_total`, and `mcp_tool_queue_wait_duration_seconds`.
4. **Browser SDK Resilience & LocalStorage TTL Hygiene**:
   - Upgraded to v0.6.2 with Full Jitter backoff, `Retry-After` header sensing, and 24h TTL expiration for offline fallback storage.

---

## v0.6.1（2026-08-21）

### 中文版本

#### 📋 版本概述

v0.6.1 是智能排障 RAG 经验库扩充与多 Agent 协同上下文优化版本。在 v0.6.0 架构重构基石之上，进一步扩充了 50% 的真实生产排障案例，打通历史经验与 Multi-Agent 修复提示词，增强了 JSON-RPC 2.0 诊断语义，全仓全绿无回归。

#### ✨ 核心特性

- **RAG 种子排障案例由 30 条扩展至 45 条**：覆盖 HTTP 502/401/429/SSL/CORS、数据库 asyncio 超时/任务取消/PostgreSQL 唯一冲突/连接池打满/Redis 拒绝连接、前端 undefined.map/DOM 存储超限/process 缺失/fetch 异常。
- **多 Agent 上下文装配与质量评分透传**：ContextAssembler 全面集成历史排障经验检索，RepairAgent 优先吸收验证过的历史修复策略，Coordinator 全流程透传可观测指标。
- **安全与协议语义升级**：JSON-RPC 2.0 错误码标准化定义与 data 字段诊断支持，未捕获异常全局脱敏与标准错误体。
- **测试与质量体系完善**：补充 Browser SDK JS 事件上报与脱敏单元测试，全仓 Ruff Lint 历史债务清零。

#### 🛠️ v0.6.1 稳定性补丁（未打新版本 tag）

v0.6.1 发布后又合入一组稳定性与正确性补丁，全部落在 `origin/main`，均不改变对外契约（无字段变更、无 Breaking Change），版本号仍为 **v0.6.1**。

- **同步工具背压与 TOOL_BUSY 快速拒绝**：同步工具调用通过有界槽位与超时控制，在槽位满且等待超时（或 timeout=0）时快速返回 `TOOL_BUSY` 业务错误，避免无界排队与线程堆积，日志文案同步修正
- **TraceEntry/TraceStep schema 去重**：统一入口数据模型定义，消除重复 schema，对外字段与结构零变更
- **agent_mode 显式优先语义修复**：显式配置的 agent 模式不再被隐式默认值覆盖，避免错误回退默认模式
- **测试环境与集成测试 API key 隔离加固**：测试执行不受本机/外部凭据干扰，缺失凭据时快速回退而非挂起等待，环境无关性更强
- **PG 知识库驱逐断言与日志修正**：驱逐路径断言与日志文案与实际行为对齐，便于排查

#### 🛠️ v0.6.1 Stability Patches (no new tag)

Additional stability and correctness patches landed on `origin/main` after the v0.6.1 release. None change the external contract (no field changes, no breaking changes) and the version stays at v0.6.1:

- **Sync-tool backpressure & fast `TOOL_BUSY` refusal**: bounded sync-tool slots and timeout controls fail fast with business-level `TOOL_BUSY` error when slots are saturated or timed out (immediate when timeout=0), avoiding unbounded queuing and thread starvation; log text aligned as well
- **TraceEntry/TraceStep schema dedup**: unified internal data-model definitions, zero external field changes
- **agent_mode explicit-priority fix**: an explicitly configured agent mode is no longer overridden by an implicit default
- **API-key isolation for test & integration environments**: tests no longer depend on ambient credentials and fall back fast when none are present instead of hanging
- **PG KB eviction assertion & log fixes**: eviction-path assertions and log messages now match actual behavior for easier diagnosis

---

## v0.6.0（2026-08-21）

### 中文版本

#### 📋 版本概述

v0.6.0 是 Lujo-MCP 的重大**架构重构与生产就绪里程碑版本**。在本版本中，彻底拆解了历史遗留的两个 God Object（`pg_store.py` 拆解为 `pg_executor` + 5 个分治 Store + 分区管理模块；`analyzer.py` 拆解为客户端工厂、缓存、注入防护、上下文准备等 6 个单一职责子模块），消除了所有隐式跨模块依赖和样板代码。同时补齐了 Prometheus 细粒度可观测性指标大盘，优化了多平台分发链与生产配置套件。无 Breaking Change，测试基线提升至 **1161 passed / 6 skipped / 0 failed**。

#### ✨ 核心重构与优化

- **PostgreSQL 存储分治重构**：
  - `pg_executor.py`：统一管理连接池生命周期、自动重试与熔断机制，封装 `execute_sql` 与 `query_sql`，彻底消除 5 个 Store 中重复的连接样板。
  - `pg_partitions.py`：标准化 traces 月度 RANGE 分区预创建与自动归档逻辑。
  - `pg_trace_store.py` / `pg_session_store.py` / `pg_error_store.py` / `pg_spec_store.py` / `pg_kb_store.py`：5 大 Store 模块独立解耦。
- **LLM 分析引擎模块化拆分**：
  - `app/llm/clients.py`：统一提供同步与异步 OpenAI 客户端分发工厂。
  - `app/llm/cache.py`：独立管理 L1 LRU + L2 Redis 多级缓存与错误特征指纹。
  - `app/llm/injection_guard.py`：规范化 Prompt Injection 防护（`wrap_evidence` / `INJECTION_GUARD`）。
  - `app/llm/context_prep.py`：收敛错误提取、脱敏、截断与 Prompt 构建。
  - `app/llm/output_schema.py` & `app/llm/kb_integration.py`：独立处理 Schema 净化校验与三级知识库/向量 RAG 召回。
- **可观测性与生产部署加固**：
  - 细化 Prometheus 监控度量（LLM Token 消耗、KB 缓存命中率、MCP 工具调用耗时、存储池排队时间）。
  - 完善 Dockerfile 与 Docker Compose 生产部署模板。

---

## v0.5.5（2026-08-19）

### 中文版本

#### 📋 版本概述

v0.5.5 是 Lujo-MCP 的 **FR12 调试提示词端点**功能版本：把「真实运行现场 → 可直接粘贴给 AI 助手的纯文本提示词」一键生成，补齐非 MCP 场景的使用闭环。同时修复了单元测试的存储后端隔离问题（此前本机 `.env STORAGE_BACKEND=postgresql` 会让 3 项单测受 PG 数据累积/路径错配影响而失败，现已强制 memory 后端，与 CI 一致）。无 Breaking Change，测试基线 1134 → **1153 passed / 6 skipped / 0 failed**。

#### ✨ 新增功能

- **调试提示词生成端点**（FR12）：`GET /api/debug/prompt?request_id={id}`（viewer 可读）——基于已采集的完整调试上下文（异常帧/源码片段/运行时/git 归因/网络链等），脱敏 + 截断后套用提示词模板，返回可一键复制的纯文本提示词，便于非 MCP 场景直接粘贴给任意 AI 助手分析
- **提示词模板可配置**（FR12）：`PROMPT_TEMPLATE_PATH` 配置项——支持自定义模板文件（UTF-8，`string.Template` 语法，占位符 `$context` / `$request_id`）；为空或文件不存在时回退内置默认模板；`safe_substitute` 对模板中非法 `$xxx` 原样保留不抛错

#### 🐛 Bug 修复

- **单元测试存储后端隔离**：`tests/unit/conftest.py` 强制 memory 后端（改写 `settings.storage_backend` + 重置 storage factory 缓存）——修复 3 项本机预存失败（`test_batch_writes`×2：固定 request_id 在 PG 跨运行累积致 len 断言失真；`test_spec_store`×1：`_add_log` 注入 traces 表 vs PG 后端恢复走专用 specs 表致数据不可见）。需真实 PG 行为的测试（`test_factory` / `test_storage` 等）用 monkeypatch 显式覆盖，不受影响

#### 🧪 测试

- 新增 FR12 单测 10 项（`tests/unit/test_prompt_builder.py`：模板渲染/脱敏/自定义模板/缺失回退/非法 `$`/端点 200·404·422）；Python 单元基线 1134 → **1153 / 6 / 0**

### English Version

#### 📋 Release Overview

v0.5.5 is Lujo-MCP's **FR12 debug prompt endpoint** release: it generates a copy-paste-ready plain-text debugging prompt from a captured runtime context, closing the loop for non-MCP workflows. It also fixes unit-test storage-backend isolation (local `.env STORAGE_BACKEND=postgresql` made 3 unit tests fail due to PG data accumulation / path mismatch; unit tests now force the memory backend, matching CI). No breaking changes; test baseline 1134 → **1153 passed / 6 skipped / 0 failed**.

#### ✨ New Features

- **Debug prompt endpoint** (FR12): `GET /api/debug/prompt?request_id={id}` (viewer) — renders the full captured debug context (exception frames / code snippets / runtime / git blame / network trace, etc.) after redaction + truncation into a copy-paste plain-text prompt via a template
- **Configurable prompt template** (FR12): `PROMPT_TEMPLATE_PATH` — custom UTF-8 template file with `$context` / `$request_id` placeholders; falls back to the built-in template when empty/missing; stray `$xxx` kept verbatim via `safe_substitute`

#### 🐛 Bug Fixes

- **Unit-test storage-backend isolation**: `tests/unit/conftest.py` forces the memory backend (mutates `settings.storage_backend` + resets the storage factory cache) — fixes 3 local pre-existing failures (`test_batch_writes`×2 from PG accumulation on fixed request IDs; `test_spec_store`×1 from `_add_log` seeding the traces table while the PG backend restores from the dedicated specs table). Tests that genuinely exercise PG (e.g. `test_factory` / `test_storage`) still override via monkeypatch.

#### 🧪 Testing

- 10 new FR12 unit tests (`tests/unit/test_prompt_builder.py`); Python unit baseline 1134 → **1153 / 6 / 0**

---

## v0.5.4（2026-08-18）

### 中文版本

#### 📋 版本概述

v0.5.4 是 Lujo-MCP 的**工程收口 + 文档补全**版本：把第 2 轮代码审查遗留的测试资产与文档欠账全部清零。无新功能、无 Breaking Change，测试基线不变（1134 passed / 6 skipped / 0 failed）。核心价值：**堵住"SDK 演进后接口失联"的监控盲区**（此前 SDK e2e 不在 CI 监控内、曾长期失联未被发现），并补齐长期缺失的公开 API 参考与 SDK 使用手册。

#### ✨ 新增功能

- **分发链 smoke 校验**（TST-3）：`tests/unit/test_distribution_smoke.py`（9 项）守护 PyInstaller 打包配置、npm 元包结构与三平台包一致性、bin 脚本存在性；版本动态读 `app.__version__` 防漂移
- **SDK JS 契约单测**（TST-3）：`browser-sdk/test/sdk-core.test.js`（7 项）——Node 无浏览器加载 UMD 包，守护公开 API 面、V5 传输配置契约（gzip 4096 / 节流 5000·2 / localStorage 降级）、`_getPublicConfig` 不含 apiKey（安全关键）
- **CI `sdk-js-smoke` job**（node 20）：SDK 契约测试纳入 CI，防止闭包化配置等演进再导致 e2e 接口失联
- **API 参考手册**（DOC-1）：`docs/public/API_REFERENCE.md` —— REST 5 组端点 + 18 个 MCP 工具（分类/角色/入参/返回）+ 鉴权 RBAC + 字段速查
- **浏览器 SDK 使用手册**（DOC-3）：`docs/public/SDK_GUIDE.md` —— 接入方式 / 26 项 init 配置 / 公开 API / 采集行为 / 拦截规则 / 脱敏 / V5 传输优化 / beacon 令牌
- **README 文档导航**：新增 API_REFERENCE / SDK_GUIDE 两行，消除孤儿文档

#### 🐛 Bug 修复

- **CSP 头未统一覆盖**（SEC-1）：`Content-Security-Policy` 此前仅在 dashboard/demo 的 HTML 响应设置，其余响应未覆盖；改为 `SecurityHeadersMiddleware` 统一 `setdefault`，单一来源覆盖所有响应
- **SDK 注释过时工具名**（QC-1）：`browser-sdk/ai-debug.js` 注释引用旧 MCP 工具名 `get_debug_context`（对外已改名 `context`），已修正

#### 🧪 测试

- 新增分发链 smoke 9 项 + SDK JS 契约 7 项（Node）；Python 单元基线不变 1134 / 6 / 0

### English Version

#### 📋 Release Overview

v0.5.4 is the **engineering consolidation** release of Lujo-MCP: it closes out all remaining test-asset and documentation debt from the second code-review round. No new features, no breaking changes; test baseline unchanged (1134 passed / 6 skipped / 0 failed). Core value: it plugs the monitoring blind spot where SDK evolution could silently break e2e tests outside CI, and adds the long-missing public API reference and SDK usage guide.

#### ✨ New Features

- **Distribution-chain smoke checks** (TST-3): `tests/unit/test_distribution_smoke.py` (9) guarding PyInstaller packaging config, npm meta-package structure and 3-platform consistency, bin script existence; version read dynamically from `app.__version__`
- **SDK JS contract tests** (TST-3): `browser-sdk/test/sdk-core.test.js` (7) — loads the UMD bundle in Node without a browser, guarding the public API surface, V5 transport config contracts (gzip 4096 / throttle 5000·2 / localStorage fallback), and `_getPublicConfig` excluding `apiKey` (security-critical)
- **CI `sdk-js-smoke` job** (node 20): SDK contract tests now run in CI to prevent silent e2e interface drift
- **API Reference** (DOC-1): `docs/public/API_REFERENCE.md` — REST endpoint groups + 18 MCP tools (category/role/inputs/outputs) + auth RBAC + field quick reference
- **Browser SDK Guide** (DOC-3): `docs/public/SDK_GUIDE.md` — setup, 26 init options, public API, capture behavior, interception rules, redaction, V5 transport, beacon tokens
- **README doc navigation**: added API_REFERENCE / SDK_GUIDE entries

#### 🐛 Bug Fixes

- **CSP not uniformly applied** (SEC-1): `Content-Security-Policy` was only set on dashboard/demo HTML responses; now applied uniformly via `SecurityHeadersMiddleware.setdefault` as a single source
- **Stale tool-name comment in SDK** (QC-1): `browser-sdk/ai-debug.js` comment referenced the old MCP tool name `get_debug_context` (renamed to `context`)

#### 🧪 Tests

- Added distribution smoke (9) + SDK JS contract (7, Node); Python unit baseline unchanged 1134 / 6 / 0

---

## v0.5.3（2026-08-18）

### 中文版本

#### 📋 版本概述

v0.5.3 是 Lujo-MCP 的 **RAG 知识库持久化** 版本：将进程内知识库（KB）同步写穿（write-through）到 PostgreSQL 并支持启动回灌，learned 调试经验跨重启保留；数据库从 `ai_debug_mcp` 改名 `lujo_mcp`；同时完成第 3 轮代码审查最后一个 P3 项（pg_store 重连后连接泄漏，P3-9）修复。无 Breaking Change：PG 未配置时自动降级为纯内存模式（行为与历史版本一致），升级无需迁移。测试基线 1105 → **1134 passed / 6 skipped / 0 failed**。

#### ✨ 新增功能

- **RAG 知识库 PostgreSQL 持久化**（`app/rag/knowledge_base.py`）：
  - 新增 `kb_entries` 表（DDL 单源 `ddl.py` + `migrations/20260817_create_kb_entries_table.sql` 同步）：fingerprint 主键、analysis JSONB、三级指纹索引列、verify_count/case_confidence 验证统计、DOUBLE PRECISION 时间戳
  - 新增 `KnowledgeBaseStorage` ABC（`base.py`）与 `PGKnowledgeBaseStore` / `NoOpKnowledgeBaseStore` 双实现，经 `factory.get_knowledge_store()` 分发：PG 后端真实持久化，memory 后端 no-op，PG 初始化失败降级 no-op 不阻断启动
  - KB `upsert` / `record_verification` / `clear` / LRU 驱逐同步落库（锁外执行，PG 故障 warning 降级）；驱逐同步删除持久行，内存与 PG 条数一致（≤ max_entries）
  - 新增 `load_from_persistent()` 启动回灌：按 `updated_at` 倒序取最近 `max_entries` 条重建内存条目（含验证统计与三级索引），PG 为权威来源，同指纹覆盖内存副本

#### 🐛 Bug 修复

- **pg_store 重连后连接泄漏**（P3-9）：`_query_with_retry` 内部重连换新连接后仅返回查询结果，调用方 `finally` 仍归还旧 conn —— 新连接从池取出永不归还（连接泄漏）、旧连接被重复归还（已 close 连接再 putconn）、重连后继续用旧引用报 `InterfaceError`；改为返回 `(rows, conn)` 与 `_execute_with_retry` 对齐，7 处调用方（traces/sessions/specs/kb_entries 读路径）全部更新为归还最新连接（第 3 轮审查 P3 至此全部清零）

#### 🧪 测试

- 新增 `tests/unit/test_kb_persistence.py`（13 项）+ `tests/unit/test_pg_store_reconnect.py`（5 项）+ 集成 2 项，基线 1105 → **1134 passed / 6 skipped / 0 failed**

### English Version

#### 📋 Release Overview

v0.5.3 is the **Knowledge Base persistence** release of Lujo-MCP: the in-process knowledge base (KB) now write-throughs to PostgreSQL and reloads on startup, so learned debugging experience survives restarts; the database was renamed from `ai_debug_mcp` to `lujo_mcp`; and the last P3 item from the third review round (post-reconnect connection leak in `pg_store`, P3-9) is fixed. No breaking changes: without PG configured it degrades to pure in-memory mode (same behavior as prior versions) and upgrades need no migration. Test baseline improved to **1134 passed / 6 skipped / 0 failed**.

#### ✨ New Features

- **RAG knowledge base PostgreSQL persistence** (`app/rag/knowledge_base.py`):
  - New `kb_entries` table (single DDL source `ddl.py` + `migrations/20260817_create_kb_entries_table.sql`): fingerprint PK, analysis JSONB, three-level fingerprint index columns, verify_count/case_confidence, DOUBLE PRECISION timestamps
  - New `KnowledgeBaseStorage` ABC (`base.py`) with `PGKnowledgeBaseStore` / `NoOpKnowledgeBaseStore` implementations via `factory.get_knowledge_store()`: PG backend persists, memory backend no-ops, PG init failure degrades to no-op without blocking startup
  - KB `upsert` / `record_verification` / `clear` / LRU eviction sync to PG (outside locks; PG failures degrade with a warning); eviction deletes the persistent row so memory and PG counts stay consistent (≤ max_entries)
  - New `load_from_persistent()` startup reload: rebuilds in-memory entries (verification stats + three-level index) from the most recent `max_entries` by `updated_at`; PG is authoritative and overwrites in-memory copies on fingerprint match

#### 🐛 Bug Fixes

- **Post-reconnect connection leak in `pg_store`** (P3-9): `_query_with_retry` returned only query results after an internal reconnect, so callers' `finally` still returned the stale conn — the new connection was never returned to the pool (leak), the old one was double-returned (putconn after close), and continued use of the stale reference raised `InterfaceError`; now returns `(rows, conn)` aligned with `_execute_with_retry`, and all 7 callers (traces/sessions/specs/kb_entries read paths) return the latest connection (the third review round's P3 items are now all closed)

#### 🧪 Tests

- New `tests/unit/test_kb_persistence.py` (13) + `tests/unit/test_pg_store_reconnect.py` (5) + 2 integration tests; baseline 1105 → **1134 passed / 6 skipped / 0 failed**

---

## v0.5.2（2026-08-15）

### 中文版本

#### 📋 版本概述

v0.5.2 是 Lujo-MCP 的 **品牌统一** 版本：将全仓 `ai-debug-mcp` 标识统一为 `lujo-mcp`（MCP server 名、logger、OTel service name、配置示例、Browser SDK description、测试断言），LICENSE 版权署名改为 LujoAI。无功能变更、无 Breaking Change，测试基线不变（1087 passed / 6 skipped / 0 failed）。

#### ✨ 变更

- **MCP server 名**：`ai-debug-mcp` → `lujo-mcp`（initialize 握手 serverInfo）
- **日志 logger 名**：全部 `logging.getLogger("ai-debug-mcp.*")` → `lujo-mcp.*`
- **OTel**：`otel_service_name` / `service_name` → `lujo-mcp`
- **配置示例 / SDK**：`mcp_config_example.json`、`browser-sdk/`（package.json + ai-debug.js）同步
- **LICENSE**：`Copyright (c) 2026 LujoAI`
- 测试断言同步更新（`test_api.py` / `test_otel.py` / `test_jsonrpc.py`）；清理本地 IDE 配置文件

### English Version

#### 📋 Release Overview

v0.5.2 is the **brand unification** release: all `ai-debug-mcp` identifiers are renamed to `lujo-mcp` (MCP server name, logger names, OTel service name, config samples, Browser SDK description, test assertions), and the LICENSE copyright now reads LujoAI. No feature changes, no breaking changes; test baseline unchanged (1087 passed / 6 skipped / 0 failed).

---

## v0.5.1（2026-08-15）

> 状态：已发布（npm `latest` → `@lujoai/lujo-mcp@0.5.1`，2026-08-15）。

### 中文版本

#### 📋 版本概述

v0.5.1 是 Lujo-MCP 的 **Source Map 堆栈还原** 版本：将前端 minified JS 堆栈帧还原为原始源码位置，补齐 Debug Context 前端盲区（此前 code_locator / static_analyzer / fault_localizer 三条证据链对 minified 帧全部失效）。新增 `resolve_stack` MCP 工具（18/18），Browser SDK 保留 column 并支持 `release` 透传；同时修复 deepseek provider base_url 缺失（此前 `LLM_PROVIDER=deepseek` 时 LLM 分析链 401 不可用）。全部新能力默认关闭、失败静默降级，无 Breaking Change。测试基线 992 → **1087 passed / 6 skipped / 0 failed**。

#### ✨ 新增功能

- **SM1 Source Map 解析核心**（`app/runtime/collectors/sourcemap_resolver.py`）：纯 Python base64-VLQ 解码 mappings（零新依赖）；`SourceMapParser` 按 (line, column) 二分查询最近段；`resolve_frame(s)` 产出 StackFrame 兼容还原帧（original 原位置 + resolved 标记）+ 源码片段（sourcesContent 优先，code_locator 白名单兜底）；LRU 解析缓存（mtime/token 指纹失效）；任何失败静默降级保留原始帧
- **SM2 获取通道**（`sourcemap_store.py`，均默认关闭）：上传通道 `POST /api/debug/sourcemap`（TTL + LRU 容量驱逐）+ 磁盘约定通道（`SOURCEMAP_PATH_PREFIX` 白名单防 LFI）；自动选路：显式 artifact > 上传按帧文件名 > 磁盘
- **SM3 集成与工具**：`DebugContext` 新增 `resolved_frames`（21 字段，向后兼容）；builder 还原命中后 code_snippets / fault_localization / git 归因 / 相关规范均改用还原帧，exception.frames 保留 minified 原帧；新 MCP 工具 `resolve_stack`（category=agent，experimental，RBAC 只读三级）；工具数 17 → **18 / 18**
- **SM4 质量联动**：QualityScorer TRACE 维度还原加成（+0.3 封顶 1.0）+ sourcemap_resolver 证据项；Benchmark Case 6 `frontend_minified_sourcemap` + `frontend_sourcemap_ab()` A/B 对照（验证还原后 Quality 评分提升）
- **Browser SDK 增强**（`ai-debug.js`）：`_parseStack` 保留 column（source map 精确定位必需）；新增可选 `release` 配置随错误 extra 透传（空 = 不发送，向后兼容）
- **配置项**：`sourcemap_enabled`（默认 False）/ `sourcemap_path_prefix` / `sourcemap_upload_ttl_seconds`（3600）/ `sourcemap_max_uploads`（100）

#### 🐛 Bug 修复

- **deepseek provider base_url 缺失**：`_PROVIDER_BASE_URLS`（analyzer + qdrant_vector_store）缺 deepseek 映射，`LLM_PROVIDER=deepseek` 且 `LLM_BASE_URL` 为空时回落 OpenAI 官方端点 → DeepSeek key 必然 401，LLM 分析链不可用。已补 `https://api.deepseek.com` + 新增 `test_resolve_base_url_deepseek`；实测真实调用返回结构化分析 JSON
- **LLM 集成 e2e 测试配置隔离**（`tests/integration/test_agent_repair_e2e.py`）：本地 `.env` 打开 `AGENT_MULTI_AGENT_ENABLED` / `AGENT_VERIFY_LOOP_ENABLED` 会污染 e2e（误走 Verify Loop，30s 轮询超时）；fixture 隔离两开关走 Phase 1 单 Agent 链路，轮询超时对齐 `agent_timeout`（90s）。DeepSeek key 有效后 2 项真实 e2e 全绿
- **git 子进程输出编码**（`app/runtime/core/git.py`）：Windows 上 `subprocess.run(text=True)` 默认按本地 gbk 解码 git 的 UTF-8 输出会抛 `UnicodeDecodeError`，导致 diff/blame 静默失败；显式 `encoding="utf-8"` + `errors="replace"` 兜底非法字节

#### 🧪 测试

- 新增 95 项（Source Map 解析 94 项 + deepseek base_url 1 项），基线 992 → **1087 passed / 6 skipped / 0 failed**；工具数 / 字段数 / Case 数断言同步更新

### English Version

#### 📋 Release Overview

v0.5.1 is the **Source Map stack resolution** release of Lujo-MCP: it maps minified frontend JS stack frames back to original source locations, closing the frontend blind spot in Debug Context (previously all three evidence chains — code_locator / static_analyzer / fault_localizer — failed on minified frames). It adds the `resolve_stack` MCP tool (18/18), Browser SDK column preservation and optional `release` passthrough, and fixes the missing deepseek provider base_url (which made the LLM analysis chain fail with 401 when `LLM_PROVIDER=deepseek`). All new capabilities default off and degrade silently — no breaking changes. Test baseline improved to **1087 passed / 6 skipped / 0 failed**.

#### ✨ New Features

- **SM1 Source Map parser core** (`app/runtime/collectors/sourcemap_resolver.py`): pure-Python base64-VLQ mappings decoding (zero new deps); `SourceMapParser` binary-search nearest segment by (line, column); `resolve_frame(s)` yields StackFrame-compatible resolved frames (original position + resolved flag) with source snippets (sourcesContent first, code_locator whitelist fallback); LRU parse cache (mtime/token fingerprint invalidation); any failure silently degrades to original frames
- **SM2 Acquisition channels** (`sourcemap_store.py`, all default off): upload channel `POST /api/debug/sourcemap` (TTL + LRU eviction) + on-disk convention channel (`SOURCEMAP_PATH_PREFIX` whitelist against LFI); auto-routing: explicit artifact > uploaded by frame filename > disk
- **SM3 Integration & tool**: `DebugContext` adds `resolved_frames` (21 fields, backward compatible); builder switches code_snippets / fault_localization / git attribution / related specs to resolved frames on hit, keeping minified originals in `exception.frames`; new MCP tool `resolve_stack` (category=agent, experimental, read-only RBAC); tool count 17 → **18 / 18**
- **SM4 Quality linkage**: QualityScorer TRACE dimension resolution bonus (+0.3 capped at 1.0) + sourcemap_resolver evidence item; Benchmark Case 6 `frontend_minified_sourcemap` + `frontend_sourcemap_ab()` A/B comparison (asserts Quality score improves after resolution)
- **Browser SDK enhancement** (`ai-debug.js`): `_parseStack` keeps column (required for source-map precision); optional `release` config passthrough in error extra (empty = not sent, backward compatible)
- **Config**: `sourcemap_enabled` (default False) / `sourcemap_path_prefix` / `sourcemap_upload_ttl_seconds` (3600) / `sourcemap_max_uploads` (100)

#### 🐛 Bug Fixes

- **Missing deepseek provider base_url**: `_PROVIDER_BASE_URLS` (analyzer + qdrant_vector_store) lacked a deepseek mapping, so with `LLM_PROVIDER=deepseek` and empty `LLM_BASE_URL` the request fell back to the OpenAI endpoint and DeepSeek keys 401'd, breaking the LLM analysis chain. Added `https://api.deepseek.com` + `test_resolve_base_url_deepseek`; a real call now returns structured analysis JSON.
- **LLM e2e test config isolation** (`tests/integration/test_agent_repair_e2e.py`): a local `.env` with `AGENT_MULTI_AGENT_ENABLED` / `AGENT_VERIFY_LOOP_ENABLED` set would pollute the e2e (routing into Verify Loop and timing out); the fixture now isolates both flags to the Phase 1 single-agent path and aligns the poll timeout to `agent_timeout` (90s). Both e2e tests pass with a valid DeepSeek key.
- **git subprocess output encoding** (`app/runtime/core/git.py`): on Windows `subprocess.run(text=True)` decodes git's UTF-8 output as local gbk, raising `UnicodeDecodeError` and silently breaking diff/blame; explicit `encoding="utf-8"` + `errors="replace"`.

#### 🧪 Tests

- 95 new tests (Source Map 94 + deepseek base_url 1), baseline 992 → **1087 passed / 6 skipped / 0 failed**; tool-count / field-count / case-count assertions updated.

---

## v0.5.0（2026-08-13）

### 中文版本

#### 📋 版本概述

v0.5.0 是 Lujo-MCP 的工程质量和 Runtime 数据契约对齐版本：`DebugContext` Pydantic model 从 7 字段扩展至 20 字段并对齐 `build_debug_context()` 实际输出，MCP `tools/list` 新增工具分类元数据，LLM 分析链路引入 Prompt Injection 防护，关键端点完成 Pydantic Schema 验证，MCP 会话表增加安全上限。无 Breaking Change：所有新增字段 Optional + default，外部 JSON 结构不变。测试基线 927 → **992 passed / 6 skipped / 0 failed**。

#### ✨ 新增功能

- **DebugContext Schema Alignment**：`DebugContext` 7→20 字段，对齐实际输出；全部 Optional + default + `extra="allow"` 支持未来扩展
- **DebugContext Runtime Integration**：`build_debug_context()` 返回类型 `dict | None` → `DebugContext | None`；MCP tools / Dashboard API 通过 `.model_dump()` 适配，外部 JSON 结构不变
- **MCP Tool Category Metadata**：`tools/list` 为每个工具新增 `category`（agent / sdk）与 `experimental`（bool）；HTTP 与 stdio 均支持，旧客户端可忽略额外字段
- **Prompt Injection 防护（P2-1）**：LLM analyzer 与 Agent 层 `_INJECTION_GUARD` 安全边界声明 + `_wrap_evidence()` XML 标签隔离，防止 Debug Context 中恶意指令文本诱导 LLM
- **API Schema Validation（P2-2）**：`/verify` 和 `/verify/ui` 端点改 Pydantic 模型（`VerifyRequest` / `VerifyUiRequest`），`extra="ignore"` 兼容旧客户端
- **Session 安全加固**：MCP 会话表 `_MAX_SESSIONS` 上限（10,000）+ LRU 驱逐 + `SessionLimitExceeded` 503；`/internal/health` 内网 IP 鉴权
- **测试**：新增 45 项（`test_debug_context_schema.py` 14 + `test_debug_context_integration.py` 14 + `test_tool_category_metadata.py` 17）
- **ruff 清理**：26 处存量 lint 清零，`ruff check .` All checks passed

### English Version

#### 📋 Release Overview

v0.5.0 is the engineering hardening and Runtime contract alignment release of Lujo-MCP: the `DebugContext` Pydantic model expands from 7 to 20 fields aligned with actual `build_debug_context()` output, MCP `tools/list` gains tool category metadata, the LLM analysis chain gains prompt-injection guarding, key endpoints get Pydantic schema validation, and the MCP session table gains security limits. No breaking changes: all new fields are Optional with defaults and external JSON structures are unchanged. Test baseline improved to **992 passed / 6 skipped / 0 failed**.

#### ✨ New Features

- **DebugContext Schema Alignment**: 7→20 fields aligned with actual output; all Optional + default + `extra="allow"` for future extension
- **DebugContext Runtime Integration**: `build_debug_context()` return type `dict | None` → `DebugContext | None`; MCP tools / Dashboard API adapted via `.model_dump()` with unchanged external JSON
- **MCP Tool Category Metadata**: `tools/list` adds `category` (agent / sdk) and `experimental` (bool) per tool; both HTTP and stdio transports; old clients can ignore extra fields
- **Prompt Injection Guard (P2-1)**: `_INJECTION_GUARD` security boundary + `_wrap_evidence()` XML-tag isolation in LLM analyzer and Agent layer
- **API Schema Validation (P2-2)**: `/verify` and `/verify/ui` endpoints switch to Pydantic models (`VerifyRequest` / `VerifyUiRequest`) with `extra="ignore"` backward compatibility
- **Session Hardening**: MCP session table `_MAX_SESSIONS` cap (10,000) + LRU eviction + `SessionLimitExceeded` 503; `/internal/health` intranet-IP auth
- **Tests**: 45 new tests (schema 14 + integration 14 + tool category metadata 17)
- **ruff cleanup**: 26 legacy lint issues resolved, `ruff check .` all checks passed

---

## v0.4.0（2026-08-09）

### 中文版本

#### 📋 版本概述

v0.4.0-beta 是 Lujo-MCP 的 **P1 Debug Experience RAG** 里程碑版本。在 v0.4.0（Debug Context Quality）主干之上，通过 Debug Experience 数据链路（D1）、三层检索 Retriever（D2）、Context Assembler 解耦（D3）、全量验证（D4）与文档冻结（D5），让 AI Agent 不仅能读取代码，还能复用历史调试经验理解真实 Bug 运行现场。测试基线提升至 **927 passed / 6 skipped / 0 failed**（含 CODE_REVIEW_FIX_PROMPT 修复与回归测试 + stacktrace 工具与存储工厂边界测试 17 项 + D5 MCP 可观测性 16 项 + D6 Benchmark 框架 19 项），无回归，并完成架构依赖方向冻结（Architecture Frozen）。

#### ✨ 新增功能

##### M1 Quality System（质量评分系统）
- **QualityScorer 规则引擎**（`app/quality/scorer.py`）：9 维度加权评分 + 证据提取 + 承载度评分 + 改进建议，纯函数 + 静默降级
- **质量注入**：`context_assembler.py` 返回 `quality_report` 字段（feature flag 控制）
- **LLM 分析增强**：`analyzer.py` 输出 `reasoning_chain` + `evidence_items`
- **Dashboard 质量报告**：`GET /api/dashboard/trace/{tid}/quality` 独立端点 + Quality 卡片（综合评分 + 9 维度网格 + 证据 + 建议）

##### M2 Knowledge Base 三级 fallback（知识库）
- **DebugCase 标准 Schema**（`app/rag/debug_case.py`）：异常调试案例结构化记录 + 三级指纹计算（归一化消息 / 类型指纹）
- **知识库三级 fallback 匹配**（`app/rag/knowledge_base.py`）：L1 精确指纹 → L1.5 归一化指纹 → L2 类型级 Jaccard；向量索引双写同步
- **种子知识库**（`app/rag/seed_data.py`）：30 条覆盖常见异常的种子案例，启动时加载

##### M3 Fault Localization 2.0（无堆栈定位）
- **URL Resolver**（`app/runtime/collectors/url_resolver.py`）：无堆栈场景下按 HTTP 方法+路径反查 FastAPI 路由表定位 handler
- **无堆栈静态分析**（`app/runtime/context/builder.py`）：静默失败无异常堆栈时，基于网络请求反查 handler 并做函数级静态分析（`ast` 标准库），注入 `static_analysis` 字段

##### M4 Agent Verify Loop（验证闭环）
- **Verify Loop**（`app/agent/verify_loop.py`）：迭代修复闭环——三层开关（agent→multi→verify）+ 四级判定（high_confidence/passed/partial/failed）+ 验证通过后 KB 写回
- **KB 验证写回**：`record_verification()` 递增 `verify_count` / 提升 `case_confidence`，写入后同步向量库

##### P1 Debug Experience RAG（v0.4.0-beta 新增，2026-08-07）
- **Debug Experience 数据链路（D1）**（`app/rag/experience.py`）：`DebugExperienceRecord` dataclass（纯 View DTO，不建存储、不替代 DebugCase）+ `from_kb_entry()` / `from_debug_context()`；字段含 fingerprint / exception_type / message_pattern / debug_context_summary / fault_location / analysis / solution / verification_result / confidence / source
- **三层检索 Retriever（D2）**（`app/rag/retriever.py`）：`retrieve_debug_experience()` — L1 fingerprint 精确（score 1.0）/ L2 message normalize（score 0.95 + Jaccard）/ L3 vector（仅 `vector_store_enabled=True`）；合并去重 + score 排序 + top_k；任何异常禁止 raise，返回 `[]` 或已有成功结果
- **Context Assembly 解耦（D3）**（`app/agent/context_assembler.py` + `app/config.py`）：新增 `_safe_debug_experience_recall()`（开关短路零调用 + 异常降级 + `asyncio.to_thread` 并发），`assemble()` 输出新增可选字段 `debug_experience`（默认 None）；`debug_experience_enabled` 默认 `False`，关闭状态零调用、零耗时
- **Architecture Frozen（D5）**：六层架构（MCP → Transport/API → Runtime Context → Agent → RAG → Storage）与依赖规则冻结；允许 Agent → RAG，禁止 Runtime → RAG/Agent/LLM/MCP、禁止 RAG → Agent/Runtime/LLM/MCP

##### npm 开箱即用分发（2026-08-09）
- **PyInstaller 单文件打包**（`packaging/lujo-mcp-server.spec` + `packaging/entry_stdio.py`）：将 Python 服务打包为单文件二进制（修复 `__file__` NameError、补充 hiddenimports、Windows 启用 UPX）
- **npm 元包 + 平台二进制包**（`npm/packages/lujo-mcp` + 3 平台包）：`npm install -g @lujoai/lujo-mcp` 开箱即用，无需配置 Python 环境；元包通过 optionalDependencies 按系统自动安装对应平台二进制（win32-x64 / linux-x64 / osx-arm64）
- **GitHub Actions 自动构建发布**（`.github/workflows/release-npm.yml`）：三平台矩阵并行 PyInstaller 打包 + 自动发布 npm（先平台包后元包）

#### 🔧 功能优化

- **知识库召回率提升**：三级 fallback 显著提升相似错误模式的命中率（归一化指纹消除路径/UUID/数字噪声，类型级 Jaccard 处理跨类型相似）
- **静默失败定位**：无堆栈场景不再无法定位，通过 URL Resolver + 函数级静态分析推断故障函数
- **长期经验沉淀**：Agent Verify Loop 使调试经验随系统运行持续积累，`verify_count` / `case_confidence` 反哺知识库质量

#### 🐛 问题修复

##### CODE_REVIEW_FIX_PROMPT 代码审查修复（2026-08-08）

按内部代码审查修复清单完成 P0×5 + P1×20 + P2 全部修复（commit `8089525`），含：

- **P0 安全/崩溃**：`debug.py` 未导入 `time`（端点 500）；`static_analyzer` LFI 路径白名单；`ui_runner` SSRF 重定向逐跳守卫；`dashboard.html` 存储型 XSS（转义 + 事件委托）+ CSP 响应头；DDL 双源分叉收敛（`ddl.py` 共享常量，pg_store / async_pg_store / migrations 三处一致）
- **P1 数据丢失/安全**：SDK 离线重试数据全丢、beacon 压缩失败、repair/analysis 队列残留、`pg_async_enabled` 混合行为 fail-fast、redact 递归脱敏全边界、RBAC 默认角色 fail-closed、analyzer 指纹去 request_id、fault_localizer 帧索引错位、`_get_conn` 无限递归 bug、SSE 有界队列、指标 key 归一化等
- **P2**：spec_store 缓存/LIKE/delete/get 回源、assert_engine 值类型归一、死配置收敛（`cb_*`/`qdrant_connect_timeout` 移除）、版本号 `0.4.0-beta` 对齐、Dockerfile 非 root + 锁定依赖
- **回归测试**：新增 `test_state_store.py` / `test_ddl_consistency.py` / `test_debug_endpoints.py` + 扩充 jsonrpc / otel / sse_hub / static_analyzer / url_resolver 用例
- **验证**：`pytest tests/unit/` = **891 passed / 6 skipped / 0 failed**（零回归）

##### 合入 main 后测试回归（M5）
- **`test_static_analyzer.py`**：移除已删除的 `analyze_source_code` / 旧版 `analyze_handler(module_path=...)` API 用例，仅保留当前 `analyze()` 用例（无堆栈入口由 `test_url_resolver.py` 覆盖）
  - **根因**：main 分支的测试文件对应旧版 StaticAnalyzer API，与 M3 合入的新 API 不兼容
  - **验证**：修复后该文件 7 用例全部通过
- **`test_security_agent_severity.py`**：`VALID_SEVERITY` 不含 `unknown`（其为哨兵值），改为断言无效值映射为 `unknown`
  - **根因**：测试断言 `VALID_SEVERITY` 含 `unknown`，与实现中「`unknown` 为无效值哨兵、不在规范集合内」的设计相悖
  - **验证**：修复后该文件 6 用例全部通过

#### ⚠️ 已知限制

1. **LLM 依赖用例**：`test_coordinator.py`、`test_agent_repair_e2e.py` 依赖真实 LLM API Key，无有效 Key 时自动 skip（本地需配置有效 `OPENAI_API_KEY`）
2. **e2e 测试需实时服务器**：Browser SDK 端到端用例需先启动 uvicorn（`python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`）
3. **本地 `.env` 覆盖默认值**：若本地 `.env` 设置了 `AGENT_ENABLED=true` / `LLM_PROVIDER=deepseek`，会覆盖默认值（CI 无此配置，使用默认值）
4. **M4 长期价值需持续观测**：Verify Loop 的长期收益依赖知识库积累，需通过后续运行持续验证
5. **P1 未实现（v0.4.0-beta 明确不含）**：自动修复（Repair Loop）未实现、Patch 生成未实现、自动代码修改/自动提交未实现、多 Agent Repair 未实现、新增 LLM 调用链未引入；`debug_experience_enabled` 默认关闭，需显式启用

#### 🔄 兼容性说明

- **向后兼容**: v0.4.0 完全兼容 v0.3.x 的 API 与配置
- **配置迁移**: 新增配置项均有合理默认值（`kb_*`、`agent_verify_loop_*` 等），无需强制迁移
- **数据格式**: 存储格式无破坏性变更；知识库新增三级指纹索引与验证统计字段，历史数据自动补全默认值

#### 📖 升级指引

1. **拉取新版本**
   ```bash
   git pull origin main
   ```
2. **安装依赖**
   ```bash
   pip install -r requirements.txt
   ```
3. **检查配置（可选）**
   - 新增 `kb_vector_index_autosync`（默认 True）、`kb_type_level_fallback`（默认 True）、`agent_verify_loop_enabled`（默认 False）等
   - 如需启用 Agent Verify Loop，在 `.env` 设 `AGENT_VERIFY_LOOP_ENABLED=true`
4. **启动服务**
   ```bash
   python -m app.main
   ```
5. **验证**
   ```bash
   pytest tests/unit/ -q   # 单元 891 passed / 6 skipped / 0 failed
   python -m uvicorn app.main:app --host 127.0.0.1 --port 8000  # 启动后跑 e2e
   ```

---

### English Version

#### 📋 Release Overview

v0.4.0-beta is the **P1 Debug Experience RAG** milestone of Lujo-MCP. Building on the v0.4.0 (Debug Context Quality) trunk, it delivers the Debug Experience data pipeline (D1), three-layer retrieval Retriever (D2), Context Assembler decoupling (D3), full validation (D4), and document freeze (D5), enabling AI Agents to reuse historical debugging experience and understand real bug runtime context. Test baseline improved to **908 passed / 6 skipped / 0 failed** (including CODE_REVIEW_FIX_PROMPT fixes and regression tests plus 18 stacktrace/storage-factory boundary tests) with no regression, and the architectural dependency directions are now frozen (Architecture Frozen).

#### ✨ New Features

##### M1 Quality System
- **QualityScorer rule engine** (`app/quality/scorer.py`): 9-dimension weighted scoring + evidence extraction + confidence scoring + improvement suggestions; pure functions + silent degradation
- **Quality injection**: `context_assembler.py` returns `quality_report` field (feature-flag controlled)
- **LLM analysis enhancement**: `analyzer.py` outputs `reasoning_chain` + `evidence_items`
- **Dashboard quality report**: `GET /api/dashboard/trace/{tid}/quality` endpoint + Quality card

##### M2 Knowledge Base Three-level Fallback
- **DebugCase standard schema** (`app/rag/debug_case.py`): structured exception debugging cases + three-level fingerprint computation
- **Three-level fallback matching** (`app/rag/knowledge_base.py`): L1 exact fingerprint → L1.5 normalized fingerprint → L2 type-level Jaccard; vector index dual-write sync
- **Seed knowledge base** (`app/rag/seed_data.py`): 30 seed cases, loaded at startup

##### M3 Fault Localization 2.0
- **URL Resolver** (`app/runtime/collectors/url_resolver.py`): reverse-lookup FastAPI route table by HTTP method + path for stackless scenarios
- **Stackless static analysis** (`app/runtime/context/builder.py`): inject `static_analysis` field via function-level static analysis (`ast` stdlib)

##### M4 Agent Verify Loop
- **Verify Loop** (`app/agent/verify_loop.py`): iterative repair closed-loop — three-level switches (agent→multi→verify) + four-level verdict (high_confidence/passed/partial/failed) + KB writeback after verification
- **KB verification writeback**: `record_verification()` increments `verify_count` / improves `case_confidence`

##### P1 Debug Experience RAG (new in v0.4.0-beta, 2026-08-07)
- **Debug Experience data pipeline (D1)** (`app/rag/experience.py`): `DebugExperienceRecord` dataclass (pure View DTO, no storage, does not replace DebugCase) + `from_kb_entry()` / `from_debug_context()`
- **Three-layer retrieval Retriever (D2)** (`app/rag/retriever.py`): `retrieve_debug_experience()` — L1 fingerprint exact (score 1.0) / L2 message normalize (score 0.95 + Jaccard) / L3 vector (only when `vector_store_enabled=True`); merge dedup + score sort + top_k; never raises, returns `[]` or prior successful results on any failure
- **Context Assembly decoupling (D3)** (`app/agent/context_assembler.py` + `app/config.py`): new `_safe_debug_experience_recall()` (flag short-circuit zero-call + degradation + `asyncio.to_thread` concurrency); `assemble()` adds optional `debug_experience` field (default None); `debug_experience_enabled` defaults to `False` (zero call / zero overhead when off)
- **Architecture Frozen (D5)**: six-layer architecture (MCP → Transport/API → Runtime Context → Agent → RAG → Storage) and dependency rules frozen; allows Agent → RAG, forbids Runtime → RAG/Agent/LLM/MCP and RAG → Agent/Runtime/LLM/MCP

##### npm Out-of-the-Box Distribution (2026-08-09)
- **PyInstaller single-file packaging** (`packaging/lujo-mcp-server.spec` + `packaging/entry_stdio.py`): packages the Python service into a single-file binary (fixes `__file__` NameError, expands hiddenimports, UPX on Windows)
- **npm meta-package + platform binary packages** (`npm/packages/lujo-mcp` + 3 platform packages): `npm install -g @lujoai/lujo-mcp` works out of the box with no Python environment setup; the meta-package auto-selects the matching platform binary via optionalDependencies (win32-x64 / linux-x64 / osx-arm64)
- **GitHub Actions auto build & publish** (`.github/workflows/release-npm.yml`): 3-platform matrix parallel PyInstaller build + auto-publish to npm (platform packages first, then meta-package)

#### 🐛 Bug Fixes

- **`test_static_analyzer.py`**: removed stale `analyze_source_code` / legacy `analyze_handler(module_path=...)` cases; kept current `analyze()` cases (stackless entry covered by `test_url_resolver.py`)
- **`test_security_agent_severity.py`**: `VALID_SEVERITY` does not include `unknown` (it is a sentinel); assertions updated to verify invalid values map to `unknown`

#### ⚠️ Known Limitations

1. LLM-dependent tests (`test_coordinator.py`, `test_agent_repair_e2e.py`) require a valid `OPENAI_API_KEY`; they auto-skip otherwise
2. Browser SDK e2e tests require a live server (`uvicorn app.main:app --port 8000`)
3. Local `.env` overrides (`AGENT_ENABLED=true`, `LLM_PROVIDER=deepseek`) affect default-value tests; CI uses defaults
4. M4 long-term value depends on ongoing knowledge base accumulation
5. **Not implemented in v0.4.0-beta**: automatic repair (Repair Loop), Patch generation, automatic code modification/commit, multi-agent repair, and new LLM call chains; `debug_experience_enabled` defaults to off (must be explicitly enabled)

#### 🔄 Compatibility

- **Backward compatible** with v0.3.x APIs and configurations
- **Configuration migration**: new config items have sensible defaults; no mandatory migration
- **Data format**: no breaking changes; KB adds new index fields with default backfill

#### 📖 Upgrade Guide

1. `git pull origin main`
2. `pip install -r requirements.txt`
3. Optional: enable `AGENT_VERIFY_LOOP_ENABLED=true` in `.env`
4. `python -m app.main`
5. Verify: `pytest tests/unit/ -q` (891 passed / 6 skipped / 0 failed)

---

## v0.3.0（2026-07-25）

### 中文版本

**Version / 版本**: v0.3.0  
**Release Date / 发布日期**: 2026-07-25  
**Codename / 代号**: Stability & Production Ready

#### 📋 版本概述

v0.3.0 是 Lujo-MCP 项目的稳定性与生产就绪版本。本次发布重点完成了 MCP HTTP 流式通信闭环、稳定性验证收口、以及业务级 UI 验证能力增强，使项目从"代码已开发"阶段正式进入"可交付启用"状态。

#### ✨ 新增功能

##### MCP 协议增强
- **MCP Streamable HTTP SSE 长连接** (`GET /mcp`)
  - 支持会话化订阅与消息推送消费
  - 实现 `notifications/session/ready` 推送
  - POST SSE 结果桥接到 GET 队列
  - DELETE 会话清理语义
  - 代码位置: `app/api/mcp_routes.py`, `app/mcp/transports/sse.py`

##### UI 验证能力增强
- **业务级 UI 场景验证**
  - 表单填写与提交验证 (`form` 断言)
  - 数据表格结构验证 (`data_table` 断言)
  - 数值范围验证 (`numeric_range` 断言)
  - 登录流程验证（组合现有功能）
  - 代码位置: `app/runtime/verifier/ui_runner.py`

##### 存储与数据优化
- **PostgreSQL 高级特性**
  - traces 表按月分区 (`PG_PARTITION_ENABLED=true`)
  - 数据归档策略 (`PG_ARCHIVE_ENABLED=true`)
  - asyncpg 异步存储 (`PG_ASYNC_ENABLED=true`)
  - 批量写入优化
  - 代码位置: `app/runtime/core/storage/pg_store.py`, `app/runtime/core/storage/async_pg_store.py`

##### 可观测性
- **OpenTelemetry 集成**
  - OTLP gRPC 指标导出
  - Prometheus `/metrics` 向后兼容端点
  - 代码位置: `app/observability.py`

- **熔断器机制**
  - LLM 调用熔断保护
  - PostgreSQL 连接熔断保护
  - 代码位置: `app/llm/analyzer.py`, `app/runtime/core/storage/pg_store.py`

##### 缓存优化
- **多级缓存架构**
  - L1 进程内 LRU 缓存（默认启用）
  - L2 Redis 分布式缓存（可选）
  - Dashboard 查询缓存
  - 代码位置: `app/llm/analyzer.py`, `app/api/dashboard.py`

#### 🔧 功能优化

- **JSON-RPC 错误码规范化**
  - 区分 Parse Error (-32700) / Invalid Request (-32600) / Method Not Found (-32601)
  - 代码位置: `app/mcp/protocol/jsonrpc.py`

- **存储降级机制**
  - PostgreSQL 不可用时自动降级到 Memory Store
  - 由 `storage_fallback_to_memory` 配置控制
  - 代码位置: `app/runtime/core/storage/factory.py`

- **安全增强**
  - fail-closed 鉴权机制
  - 请求体大小限制（Content-Length + chunked）
  - IP / 端点级限流
  - 安全响应头默认启用
  - LFI / SSRF / URL 白名单防护
  - 代码位置: `app/middleware.py`, `app/runtime/verifier/ui_runner.py`

#### 🐛 问题修复

- **M9 .env 未知键崩溃**
  - 修复 `pydantic-settings` 的 `extra_forbidden` 导致启动失败
  - 允许 `.env` 中存在多余键而不崩溃
  - 代码位置: `app/config.py`

- **SEC-13 非原子写入**
  - `spec_store.update()` 改为 crash-safe append
  - `trace_repo.save_trace()` 写入顺序优化
  - 代码位置: `app/runtime/verifier/spec_store.py`, `app/runtime/core/trace_repo.py`

- **M7 API_KEY 空串鉴权**
  - 空串/纯空白 `api_key` 归一化为 `None`
  - 代码位置: `app/config.py`

- **N3 stdio 关闭资源回收**
  - PG 连接池关闭
  - 后台任务取消
  - excepthook 卸载
  - 代码位置: `app/mcp_server.py`, `app/mcp/transports/stdio.py`

#### ⚠️ 已知限制

1. **MCP server->client notifications**
   - 当前仅支持 `session/ready` 与 POST SSE 结果桥接
   - 更丰富的通知类型待扩展

2. **UI 验证环境依赖**
   - `verify_ui` 和 `auto_test` 需要 Playwright + Chromium
   - 需要目标页面环境可达

3. **分布式部署**
   - Redis 状态后端需要手动配置
   - 多实例限流共享需要 Redis 环境

4. **Docker 容器化**
   - Docker Compose 配置已提供
   - 容器化验证待环境支持（`STAB-007`）

#### 🔄 兼容性说明

- **向后兼容**: v0.3.0 完全兼容 v0.2.x 的 API 与配置
- **配置迁移**: 无需迁移，新增配置项均有合理默认值
- **数据格式**: 存储格式无变化，可直接升级

#### 📦 依赖版本要求

##### 核心依赖
```
fastapi>=0.115.0
uvicorn>=0.49.0
python-dotenv>=1.0.0
pydantic-settings>=2.0.0
openai>=1.0.0
psutil>=5.9.0
mcp>=1.0.0
httpx>=0.27.0
```

##### 存储依赖（可选）
```
psycopg2-binary>=2.9.0      # PostgreSQL 同步存储
asyncpg>=0.29.0             # PostgreSQL 异步存储
redis>=5.0.0                # Redis 缓存与状态后端
```

##### 可观测性依赖（可选）
```
pybreaker>=1.0.0            # 熔断器
opentelemetry-api>=1.20.0
opentelemetry-sdk>=1.20.0
opentelemetry-exporter-otlp-proto-grpc>=1.20.0
```

##### 开发依赖
```
pytest>=8.0.0
pytest-asyncio>=0.24.0
pytest-cov>=5.0.0
ruff>=0.8.0
```

#### 📖 升级指引

##### 从 v0.2.x 升级到 v0.3.0

1. **备份现有配置**
   ```bash
   cp .env .env.backup
   ```

2. **拉取新版本**
   ```bash
   git pull origin main
   ```

3. **安装依赖**
   ```bash
   pip install -r requirements.txt
   ```

4. **检查配置（可选）**
   - 新增配置项均有默认值，无需手动配置
   - 如需启用高级功能，参考 `.env.example`

5. **启动服务**
   ```bash
   python -m app.main
   ```

6. **验证升级**
   ```bash
   # 运行单元测试
   pytest tests/unit/ -q
   
   # 检查服务健康状态
   curl http://localhost:8000/health
   ```

---

### English Version

#### 📋 Release Overview

v0.3.0 is the Stability & Production Ready release of Lujo-MCP. This release focuses on completing the MCP HTTP streaming loop, stability verification convergence, and business-level UI verification capabilities, transitioning the project from "code developed" to "delivery-ready" status.

#### ✨ New Features

##### MCP Protocol Enhancements
- **MCP Streamable HTTP SSE Long Connection** (`GET /mcp`)
  - Session-based subscription and message push consumption
  - `notifications/session/ready` push implementation
  - POST SSE result bridging to GET queue
  - DELETE session cleanup semantics
  - Location: `app/api/mcp_routes.py`, `app/mcp/transports/sse.py`

##### UI Verification Capabilities
- **Business-Level UI Scenario Verification**
  - Form filling and submission verification (`form` assertion)
  - Data table structure verification (`data_table` assertion)
  - Numeric range verification (`numeric_range` assertion)
  - Login flow verification (combining existing features)
  - Location: `app/runtime/verifier/ui_runner.py`

##### Storage & Data Optimization
- **PostgreSQL Advanced Features**
  - Monthly partitioning for traces table (`PG_PARTITION_ENABLED=true`)
  - Data archival strategy (`PG_ARCHIVE_ENABLED=true`)
  - asyncpg async storage (`PG_ASYNC_ENABLED=true`)
  - Batch write optimization
  - Location: `app/runtime/core/storage/pg_store.py`, `app/runtime/core/storage/async_pg_store.py`

##### Observability
- **OpenTelemetry Integration**
  - OTLP gRPC metrics export
  - Prometheus `/metrics` backward-compatible endpoint
  - Location: `app/observability.py`

- **Circuit Breaker Mechanism**
  - LLM call circuit breaker protection
  - PostgreSQL connection circuit breaker protection
  - Location: `app/llm/analyzer.py`, `app/runtime/core/storage/pg_store.py`

##### Cache Optimization
- **Multi-Level Cache Architecture**
  - L1 in-process LRU cache (enabled by default)
  - L2 Redis distributed cache (optional)
  - Dashboard query cache
  - Location: `app/llm/analyzer.py`, `app/api/dashboard.py`

#### 🔧 Improvements

- **JSON-RPC Error Code Standardization**
  - Distinguish Parse Error (-32700) / Invalid Request (-32600) / Method Not Found (-32601)
  - Location: `app/mcp/protocol/jsonrpc.py`

- **Storage Fallback Mechanism**
  - Automatic fallback to Memory Store when PostgreSQL is unavailable
  - Controlled by `storage_fallback_to_memory` configuration
  - Location: `app/runtime/core/storage/factory.py`

- **Security Enhancements**
  - fail-closed authentication mechanism
  - Request body size limits (Content-Length + chunked)
  - IP / endpoint-level rate limiting
  - Security response headers enabled by default
  - LFI / SSRF / URL whitelist protection
  - Location: `app/middleware.py`, `app/runtime/verifier/ui_runner.py`

#### 🐛 Bug Fixes

- **M9 .env Unknown Key Crash**
  - Fixed `pydantic-settings` `extra_forbidden` causing startup failure
  - Allows extra keys in `.env` without crashing
  - Location: `app/config.py`

- **SEC-13 Non-Atomic Writes**
  - `spec_store.update()` changed to crash-safe append
  - `trace_repo.save_trace()` write order optimization
  - Location: `app/runtime/verifier/spec_store.py`, `app/runtime/core/trace_repo.py`

- **M7 API_KEY Empty String Authentication**
  - Empty/whitespace-only `api_key` normalized to `None`
  - Location: `app/config.py`

- **N3 stdio Shutdown Resource Cleanup**
  - PG connection pool closure
  - Background task cancellation
  - excepthook uninstallation
  - Location: `app/mcp_server.py`, `app/mcp/transports/stdio.py`

#### ⚠️ Known Limitations

1. **MCP server->client notifications**
   - Currently only supports `session/ready` and POST SSE result bridging
   - Richer notification types pending expansion

2. **UI Verification Environment Dependencies**
   - `verify_ui` and `auto_test` require Playwright + Chromium
   - Target page environment must be reachable

3. **Distributed Deployment**
   - Redis state backend requires manual configuration
   - Multi-instance rate limiting sharing requires Redis environment

4. **Docker Containerization**
   - Docker Compose configuration provided
   - Containerization verification pending environment support

#### 🔄 Compatibility

- **Backward Compatible**: v0.3.0 is fully compatible with v0.2.x APIs and configurations
- **Configuration Migration**: No migration needed; new configuration items have reasonable defaults
- **Data Format**: Storage format unchanged; can upgrade directly

#### 📦 Dependency Requirements

##### Core Dependencies
```
fastapi>=0.115.0
uvicorn>=0.49.0
python-dotenv>=1.0.0
pydantic-settings>=2.0.0
openai>=1.0.0
psutil>=5.9.0
mcp>=1.0.0
httpx>=0.27.0
```

##### Storage Dependencies (Optional)
```
psycopg2-binary>=2.9.0      # PostgreSQL sync storage
asyncpg>=0.29.0             # PostgreSQL async storage
redis>=5.0.0                # Redis cache and state backend
```

##### Observability Dependencies (Optional)
```
pybreaker>=1.0.0            # Circuit breaker
opentelemetry-api>=1.20.0
opentelemetry-sdk>=1.20.0
opentelemetry-exporter-otlp-proto-grpc>=1.20.0
```

##### Development Dependencies
```
pytest>=8.0.0
pytest-asyncio>=0.24.0
pytest-cov>=5.0.0
ruff>=0.8.0
```

#### 📖 Upgrade Guide

##### Upgrading from v0.2.x to v0.3.0

1. **Backup Existing Configuration**
   ```bash
   cp .env .env.backup
   ```

2. **Pull New Version**
   ```bash
   git pull origin main
   ```

3. **Install Dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Check Configuration (Optional)**
   - New configuration items have defaults; no manual configuration needed
   - For advanced features, refer to `.env.example`

5. **Start Service**
   ```bash
   python -m app.main
   ```

6. **Verify Upgrade**
   ```bash
   # Run unit tests
   pytest tests/unit/ -q
   
   # Check service health
   curl http://localhost:8000/health
   ```

---

## 相关链接 / Related Links

- [启动前检查清单 / Pre-flight Checklist](./PREFLIGHT_CHECKLIST.md)
- [异常排查指南 / Troubleshooting Guide](./TROUBLESHOOTING.md)
