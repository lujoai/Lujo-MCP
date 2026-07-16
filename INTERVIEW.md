# ai-debug-mcp 面试准备文档

> 定位：面试资料，帮助作者讲清楚项目价值、技术难点、架构决策。
> 本文件不参与日常 AI 开发，AI 开发请见 [AI_RULES.md](./AI_RULES.md)。
>
> 岗位：AI Native 应用开发工程师
> 项目：基于 MCP 协议的 AI 智能调试服务（规范驱动 + 静默失败检测 + 多 Agent 协同 + UI 自动验收）
> 技术栈：Python / FastAPI / MCP / Playwright / pytest / PostgreSQL / Redis / Trae / Qoder
> 测试覆盖：当前测试状态以 [README.md](./README.md) 项目状态表为准
> 版本：v0.3.0 已交付 | MCP 工具 HTTP 15 / stdio 14
> 路线图：Phase 0 标准化 ✅ → Phase 1.x 工程化增强（进行中）→ Phase 2 分布式链路追踪 → Phase 4 RAG 知识库

---

## 零、项目背景与设计动机

### 0.1 项目背景

在实际开发中，开发者面临两个核心痛点：

1. **静默失败难以发现**：接口返回 200、无异常日志，但功能实际不对（如按钮没反应、字段缺失）。现有监控工具（Sentry、ELK）只能监控"有异常"的情况，对"无报错但行为不符预期"无能为力。

2. **多 Agent 协同调试成本高**：代码报错后需要手动查日志、翻代码、拼提示词再丢给 AI，每次耗时 5–15 分钟。Trae/Claude/Codex 等 AI 编码助手各自为战，无法共享调试上下文。

### 0.2 为什么设计 MCP

评估了三个方案：

| 方案 | 优点 | 缺点 | 选择 |
|------|------|------|------|
| 自定义 JSON API | 灵活 | 每个 Agent 都要适配，维护成本高 | ❌ |
| Google A2A 协议 | 新标准 | 太新，生态不成熟 | ❌ |
| MCP（Model Context Protocol） | Claude/Trae/Codex 原生支持，零适配成本 | 绑定规范版本，未来升级需考虑兼容性 | ✅ |

**核心决策**：选择 MCP，因为主流 AI 编码助手原生支持，零适配成本。代价是绑定了 2024-11-05 规范版本，未来升级需要考虑兼容性。

### 0.3 为什么使用 PostgreSQL

评估了三个方案：

| 方案 | 优点 | 缺点 | 选择 |
|------|------|------|------|
| SQLite | 零依赖，单文件 | 不支持多进程并发共享，容量受限 | ❌ |
| memory（内存） | 延迟 0ms，开发零依赖 | 重启即丢，无法持久化 | 开发用 |
| PostgreSQL | JSONB 类型匹配 trace data 灵活 schema，MVCC 高并发，成熟查询优化器 | 需要 Docker 部署 | ✅ 生产用 |

**核心决策**：采用工厂模式，memory（开发）+ PostgreSQL（生产）双后端一行配置切换。PostgreSQL 的 JSONB 类型完美匹配 trace data 的灵活 schema 需求。

### 0.4 为什么规范驱动开发

**问题**：LLM 判断"这个接口返回 200，看起来正常"可能误判；LLM 给的错误结论会扩散到其他 Agent。

**解决方案**：不信任模型的记忆，用可审计的 Ground Truth 替代。

1. **spec_store** — 规范是唯一的真相来源，可追溯、可审计
2. **assert_engine** — 确定性纯函数比对，相同输入 = 相同输出，逻辑透明
3. **verify 闭环** — 每次请求自动校验，偏离即告警

**对比 LLM 做同样的事**：
```
LLM 判断: "这个接口返回了 200，看起来正常" → 可能误判
纯函数:   actual.status=200, spec.expect.body.user_id=null
         → diff: {field: "body.user_id", expected: "string", actual: null}
         → silent_failure=true  ← 确定性的结论
```

### 0.5 架构设计取舍

| 决策 | 选了什么 | 替代方案 | 取舍原因 |
|------|----------|----------|----------|
| 协议 | MCP JSON-RPC 2.0 | 自定义 REST / gRPC | Claude/Trae 原生支持，零适配成本 |
| 断言 | 纯函数 assert_behavior | LLM 语义判断 | 确定性 > 灵活性；<1ms > 500ms |
| 存储 | 工厂模式 memory↔PG | 直接硬编码 PG | 开发用内存秒启，生产切 PG 一行配置 |
| 传输 | HTTP + stdio 双模式 | 单一传输 | 运维需要 Web，IDE Agent 需要本地 |
| LLM | 宿主机推理优先，内置可选 | 每条都调 LLM | 省钱（不重复推理）、防幻觉传播 |
| 前端验证 | Playwright 可选依赖 | 硬依赖 / Selenium | headless 稳定、不安装不影响 API 功能 |
| 安全 | fail-closed（默认拒绝） | fail-open | 线上安全第一，不存侥幸 |
| 规范存储 | dict+Lock（量小） | 直接 PG | 规范 <100 条，读写比极高，延迟 0ms |

### 0.6 遇到问题如何解决

#### 问题 1：Starlette 1.3 中间件 body 重放失效

**现象**：服务启动正常，但所有 POST 请求都返回 422 `{"detail":[{"loc":["body"],"msg":"Field required","input":null}]}`

**定位过程**：
1. 先怀疑 PowerShell/curl 发 body 的问题，换 `curl.exe` + 文件 body 仍 422
2. 再用 Python `urllib`（完全绕过 curl）发请求，还是 422 → 确认是服务端问题
3. 422 的 `loc:["body"], input:null` 说明 body 在到达路由前就丢了，锁定中间件层
4. 逐个看中间件，`MaxBodySizeMiddleware` 嫌疑最大——它在 `BaseHTTPMiddleware` 里用 `request.stream()` 消费 body 后，靠 `request._receive = receive` 重放

**根因**：`BaseHTTPMiddleware` 中读取 body 并通过私有属性 `request._receive` 重放的写法，在 Starlette 1.3 新版下失效。单元测试用 TestClient 没暴露，因为 TestClient 的请求执行路径与真实 HTTP 不同。

**修复**：中间件改为只做 `Content-Length` 硬检查（超限直接 413），不再在中间件层流式消费 body。

**价值**：单测过 ≠ 真能跑，TestClient 与真实 HTTP 路径有差异，必须实跑验证。

#### 问题 2：PGStore conn.execute() API 误用

**现象**：直接使用 `conn.execute()` 在 psycopg2 中报错。

**根因**：psycopg2 的 connection 对象没有 `execute()` 方法，必须通过 cursor 对象执行 SQL。

**修复**：改为 `cur = conn.cursor(); cur.execute()` 模式。

**价值**：psycopg2 connection 使用必须遵循 cursor 模式。

#### 问题 3：data 字段非 dict 时崩溃

**现象**：`json.loads` 失败 / `.get()` 报错。

**根因**：trace data 可能是 str/int/float/bool/list/dict 等多种类型，直接 `json.loads` 或 `.get()` 会崩溃。

**修复**：`_parse_data` 安全解析 + 类型检查，`save_entry` 用 `json.dumps` 统一序列化。

#### 问题 4：stdio 模式在真实用户场景下启动即崩（ENV-001，配置加载的 CWD 陷阱）

**现象**：本项目根目录下一切正常；但 MCP 客户端从其他项目的工作区拉起 `python -m app.mcp_server` 时，`Settings()` 初始化抛 pydantic `ValidationError`，8 个 `extra_forbidden` 字段全是陌生的 Django/MySQL 配置键，服务无法启动。

**定位过程**：
1. 报错字段（`secret_key=django-insecure-...`、`database_port=3306`）明显不属于本项目 → 排查配置来源
2. 本项目 `.env` 干净、系统环境变量干净 → 锁定"pydantic-settings 读到了别的 `.env` 文件"
3. `config.py` 里 `env_file=".env"` 是相对路径，按**进程 CWD** 解析；MCP 客户端拉起 stdio 子进程时 CWD 是它打开的工作区
4. 在目标项目目录下用本项目 venv 复现 → 逐字节还原同一份报错，根因坐实

**根因**：`env_file` 相对路径 + pydantic-settings 2.x 对 dotenv 未知键默认 `extra='forbid'`。而"从别人项目的目录被拉起"正是本产品 stdio 模式的标准使用姿势——目标项目根目录几乎必然有自己的 `.env`，等于核心场景必现崩溃。

**修复**：`env_file` 锚定为基于 `__file__` 的项目根绝对路径，任意 CWD 启动行为一致。约 3 行改动。

**价值**：(1) "自己目录能跑 ≠ 用户场景能跑"——所有自测都在项目根目录做，第一次以真实用户姿势启动就崩，和问题 1 的"TestClient 过 ≠ 实跑过"是同一类教训的升级版；(2) 面向"被第三方进程拉起"的服务，一切路径解析都不能依赖 CWD。

---

## 一、项目数据流骨架（背这个就懂项目）

> 一条 `POST /api/debug/run` 请求，从进来到返回，经过 4 步。记住这 4 步 = 记住整个项目骨架。

### 四步 × 文件位置

| 步 | 文件 | 函数 | 行号 | 干啥 | 为什么在这层 |
|---|---|---|---|---|---|
| ① 写 trace | `app/api/debug.py` | `debug_run` | 28–37 | `add_log` 写 3 条（request_start / processing / response_ready） | 路由层只记"发生啥"，不关心存哪 |
| ② 存 trace | `app/mcp/core/logs.py` → `storage/memory_store.py` | `add_log` → `save_entry` | logs.py:13 / memory_store.py:16 | 存进内存 dict（key=request_id） | 存储单独一层，换 PG 只改这里 |
| ③ 拼 context | `app/mcp/builders/context.py` | `build_context` | 8–41 | trace 拼成摘要（flow/input/output/errors） | 构建器单独一层，转换逻辑隔离 |
| ④ 返回 | `app/api/debug.py` | `debug_run` | 67–72 | 打包 `DebugResponse` | 回路由层组装响应 |

### 一行数据流串（背这句）
```
请求 → debug.py 写3条trace → logs.py 转发 → memory_store.py 存dict
     → logs.py 取回 → context.py 拼摘要 → debug.py 打包返回
```

### 为什么分这四步（分层意义）
每层只干一件事：路由记日志 / 存储存取 / 构建器拼摘要 / 路由组装返回。
好处：①能单独测 ②换存储后端只动一层 ③构建逻辑不污染路由。

### 实战 bug 定位 3 步法（面试讲"遇到过什么坑"用）
```
1. 换客户端（curl.exe / Python urllib）都不行  → 确认是服务端问题，不是客户端
2. 报错 loc:["body"], input:null              → body 到路由前就丢了 → 锁定中间件层
3. 逐个看中间件 → MaxBodySize 读 body 后重放  → Starlette 新版失效 → 改成只查 Content-Length
```

---

## 二、STAR 法则项目经历

### 项目一：ai-debug-mcp — AI 智能调试中台（规范驱动 + 多 Agent 协同）

**S — Situation 背景**

在实际开发中，开发者面临两个痛点：（1）代码报错后需要手动查日志、翻代码、拼提示词再丢给 AI，每次耗时 5–15 分钟；（2）更隐蔽的问题是"静默失败"——接口返回 200、无异常日志，但功能实际不对，现有监控工具完全查不出来。

现有方案（Sentry、ELK）只能监控"有异常"的情况，对"无报错但行为不符预期"无能为力。需要一个以规范为基准、自动断言、支持多 Agent 协同的调试系统。

**T — Task 任务**

独立设计并实现一个基于 MCP 协议的 AI 调试服务，覆盖三个核心能力：

1. **自动异常捕获 + 代码定位**：任何未处理异常自动入库，附带源码片段和 IDE 可跳转链接，开发者零翻找
2. **规范驱动验证**：用期望规范（Spec）作为"地面真相"，系统自动比对实际结果 vs 预期，偏离即告警
3. **多 Agent 协同**：同时支持 HTTP 远程调用和 stdio 本地集成，Claude Desktop / Trae / Codex 等 AI 编码助手可直接发现并调用工具
4. **前端自动化**：浏览器 SDK 自动上报 + Playwright 自动遍历 UI，解决"按钮没反应但无报错"的静默失败

**A — Action 行动**

**架构设计：** 采用五层分层架构（传输层 → 中间件层 → 路由分发 → 调试引擎 → 存储层），在三个核心决策上做了深入取舍：

1. **断言引擎：纯函数 vs LLM 判断** — 评估了 LLM 语义判断方案，发现延迟高（>500ms）且结果不确定，最终选择纯函数 `assert_behavior(actual, spec)` → `{matched, diffs, silent_failure}`，确定性强、延迟 <1ms、可解释性好。单元测试覆盖 API/UI/Rule 三种 spec kind。

2. **双传输：HTTP + stdio 共存** — 不是"多此一举"，而是产品必须覆盖两个渠道：HTTP 供运维远程调试（需要中间件安全栈），stdio 供 IDE Agent 本地集成（Claude Desktop 原生支持）。核心技术约束：两套传输在 handler 层复用同一批工具业务函数（HTTP 注册表 15 个工具，stdio 清单 14 个），业务逻辑不重复。

3. **规范存储：dict+Lock vs PostgreSQL** — 规范量级小（<100 条）、读写比极高（低频写入、高频读取），`dict + threading.Lock` 主存方案延迟 0ms，`add_log` 做持久化备份。预留了工厂模式（一行切换到 PG），但不在不需要时过早优化。

**多 Agent 协同实现：** 基于 MCP JSON-RPC 2.0 协议，全局 `_tool_registry` 注册 15 个工具。每个 Agent 有独立 `Mcp-Session-Id`（TTL 30 分钟），Agent A 采集的数据（异常堆栈、源码片段、运行时快照）通过 `build_debug_context` 统一打包，Agent B 可直接消费。

**UI 自动验收闭环：** 在已有的 `verify_ui`（按选择器精确测试）基础上扩展 `auto_test` 工具，自动扫描页面所有可交互元素（按钮/链接/输入框），依次执行交互并实时监听浏览器控制台错误 + 网络 4xx/5xx，无需手动指定选择器，形成 AI 生成代码后的"部署→自动遍历→缺陷捕获→反馈修复"闭环。

**防幻觉机制：** 用 `spec_store` 维护规范（可审计、可追溯），`verify` 工具每次调用都做确定性比对。即使 LLM 判断"没问题"，如果规范断言发现偏离，系统自动标记 `silent_failure=true` 并写入 `spec_diffs`，Dashboard 可视化差异。

**安全与可靠性：** 中间件链 fail-closed 设计（Auth → BodySize 限制 → RateLimit → SecurityHeaders），LLM/Playwright/runtime 采集全部降级不阻断主流程，`exception_hook` 全局覆盖 sync+asyncio 未捕获异常。

**R — Result 成果**

- 从零到交付完整产品，**171 个单元测试**全部通过（覆盖断言引擎、规范存储、verify 工具、API 端点、Spec CRUD、Dashboard、多 LLM provider、UI runner、工具注册等模块）
- 支持 **15 个 REST 端点 + 15 个 MCP 工具**（HTTP 侧注册表；stdio 侧 14 个，含 auto_test 页面自动遍历），同时服务 HTTP 远程调用和 stdio 本地 Agent 集成，**已在 Trae 和 Qoder 中实际集成验证**
- 断言引擎 **< 1ms 判定静默失败**，前端自动遍历 **< 30s/页**
- **auto_test** 工具自动扫描页面所有可交互元素，依次执行交互并监听控制台错误 + 网络 4xx/5xx，形成 AI 生成代码后的自助验收闭环
- 规范驱动闭环（写规范 → 自动比对 → 偏离告警）完整可用，**Web 控制台 Dashboard** 可视化 trace 与 spec_diffs
- 多 LLM provider（openai/zhipu/custom）一行配置切换，存储层 memory/PG 工厂模式一键切换，零代码改动
- 实战定位并修复 **Starlette 1.3 中间件 body 重放失效** 导致的生产级 422 bug（TestClient 未能暴露，通过逐层排查 + 多客户端交叉验证定位根因）
- **Phase 0 标准化完成**：Docker Compose 一键启动（PostgreSQL + Redis + App），scripts/ 目录（run_tests.sh / lint.sh / init_db.sh），migrations/ 目录（6 个 SQL 文件管理 Schema 变更），README 项目状态表格作为唯一真相来源

**后续路线图（Phase 1 进行中）**：

| Phase | 目标 | 时间 | 核心任务 |
|-------|------|------|---------|
| **Phase 1** | 生产级数据采集系统 | 2-3 周 | Repository 层拆分、spec_store 迁移到 PG、批量插入优化 |
| Phase 2 | 分布式链路追踪平台 | 3 周 | OpenTelemetry SDK、异步写入（asyncpg）、跨服务链路 |
| Phase 3 | 智能错误分析引擎 | 1 个月 | 错误指纹分类、根因排序算法 |
| Phase 4 | RAG 知识库系统 | 1 个月 | Qdrant 向量数据库、Bug embedding、语义检索 |

---

## 三、技术决策深度问答

### Q1：多 Agent 协同需要注意什么？

**一、协议标准化 — 不自己造轮子**

评估了三个方案：
- 自定义 JSON API：灵活但需要每个 Agent 适配，维护成本高
- Google A2A 协议：太新，生态不成熟
- MCP（Model Context Protocol）：Claude Desktop / Trae / Codex 原生支持，**零适配成本**

选择 MCP 的代价：绑定了 2024-11-05 规范版本，未来升级需要考虑兼容性。

**二、工具发现机制 — 统一注册，双传输共用**

```python
# tools/__init__.py — 全局注册一次
register_all_tools()
  → _tool_registry["verify"] = {name, description, inputSchema, handler}
  → _tool_registry["verify_ui"] = ...
  → 共 15 个工具

# HTTP 传输：mcp_routes.py → POST /mcp (tools/list + tools/call)
# stdio 传输：mcp_server.py → stdin/stdout (独立 14 工具清单，handler 复用业务函数)
```

**三、会话隔离 — 每个 Agent 独立，互不污染**

```
Agent A → Mcp-Session-Id: aaa → registry.create() → TTL 30min
Agent B → Mcp-Session-Id: bbb → 独立 session
```

Agent A 写入的 trace 不污染 Agent B 的上下文。共享数据通过 `build_debug_context` 显式传递。

**四、防止幻觉传播 — Ground Truth 机制**

```
Agent A 上报: "这段代码没问题"
  ↓
verify(actual, spec) → {matched: false, silent_failure: true}
  ↓
spec_diffs 注入 build_debug_context
  ↓
Agent B 拿到的是客观差异数据，不是 Agent A 的主观判断
```

**五、错误边界 — 单点失败不扩散**

```
LLM 分析失败  → 降级（不阻断主流程）
Playwright 未安装 → 跳过 UI 遍历（不影响 API verify）
中间件鉴权失败 → fail-closed（直接 401，不降级放行）
```

---

### Q2：新工具从定义到被调用的完整流程？

```
步骤               代码位置                    核心操作
─────────────────────────────────────────────────────────
1. 定义           verify_api.py              VERIFY_DEF = {name, description,
                                              inputSchema: {actual, spec, spec_id}}
                                              def verify_handler(arguments) → result

2. 注册           tools/__init__.py          from .verify_api import VERIFY_DEF, handler
                                              register_tool(**VERIFY_DEF, handler=handler)

3. 入注册表        protocol/server.py         _tool_registry = {}
                                              _tool_registry["verify"] = {
                                                  name, description, inputSchema,
                                                  handler: verify_handler
                                              }

4. Agent 握手     mcp_routes.py              POST /mcp → initialize → capabilities: {tools:{}}

5. Agent 发现     protocol/server.py         POST /mcp → tools/list
                                              _handle_tools_list() → 遍历 _tool_registry
                                              → 返回全部 15 个工具定义

6. Agent 调用     protocol/server.py         POST /mcp → tools/call
                                              _handle_tools_call():
                                                tool = _tool_registry["verify"]
                                                result = tool["handler"](arguments)
                                                → 包装为 MCP content 数组返回

7. 结果持久化     verify_api.py              trace_id 存在时 add_log(trace_id, "verify", result)
                                              → build_debug_context 下次自动注入 spec_diffs
                                              → Dashboard 可视化差异
```

**关键设计**：工具 handler 是纯函数，不依赖传输层。同一个 handler 可以被 HTTP JSON-RPC、stdio MCP Server、REST endpoint 三种方式调用。

---

### Q3：模型把错误结论写进了记忆，怎么纠错？

**核心思路：不信任模型的记忆，用可审计的 Ground Truth 替代。**

**第一层：spec_store — 规范是唯一的真相来源**

```python
# 所有期望规范都存这里，可追溯、可审计
spec_store.create({"kind": "api", "target": "POST /login",
                   "expect": {"status": 200, "body_rules": {"success": true}}})
# → spec-abc123

# 任何人（或 Agent）改过规范都能查
GET /api/spec → [{id, kind, target, expect, updated_at}, ...]
```

**第二层：assert_engine — 确定性比对，不依赖模型判断**

```python
# 纯函数，相同输入 = 相同输出，逻辑透明
def assert_behavior(actual, spec):
    diffs = []
    for field, expected in spec["expect"].items():
        if actual.get(field) != expected:
            diffs.append({"field": field, "expected": expected, "actual": actual.get(field)})

    silent_failure = (len(diffs) > 0) and not is_error(actual)
    # 关键：无异常 + 无 4xx/5xx + 比对不匹配 = 静默失败
    return {"matched": len(diffs) == 0, "diffs": diffs, "silent_failure": silent_failure}
```

对比 LLM 做同样的事：
```
LLM 判断: "这个接口返回了 200，看起来正常" → 可能误判
纯函数:   actual.status=200, spec.expect.body.user_id=null
         → diff: {field: "body.user_id", expected: "string", actual: null}
         → silent_failure=true  ← 确定性的结论
```

**第三层：verify 闭环 — 每次请求自动校验**

```
请求进来 → 处理完毕 → verify(actual, spec) 
  → 如果 silent_failure: add_log(trace_id, "verify", result)
  → build_debug_context 自动注入 spec_diffs
  → Dashboard 展示: expected vs actual
```

**纠错流程**：人 review spec_diffs → 发现规范定义有问题 → PATCH /api/spec/{id} 修正 → 下次 verify 自动以新规范为准。

---

### Q4：接入真实业务系统，架构怎么设计？

**一、整体架构 — 五层分层，每层可独立演化**

五层分层架构：客户端层 → 传输层（stdio / Streamable HTTP）→ 中间件层（CORS → Auth → BodySize → RateLimit → SecurityHeaders → Trace）→ 调试引擎（exception_hook / code_locator / assert_engine / verify / LLM analyzer / ui_runner / spec_store / runtime_snapshot）→ 存储层（trace_store / session_store / spec_store，memory/PG 工厂模式）。

> 完整架构图和模块设计请查看 [DESIGN.md](./DESIGN.md)。

**二、关键取舍决策**

| 决策 | 选了什么 | 替代方案 | 取舍原因 |
|------|----------|----------|----------|
| 协议 | MCP JSON-RPC 2.0 | 自定义 REST / gRPC | Claude/Trae 原生支持，零适配成本 |
| 断言 | 纯函数 assert_behavior | LLM 语义判断 | 确定性 > 灵活性；<1ms > 500ms |
| 存储 | 工厂模式 memory↔PG | 直接硬编码 PG | 开发用内存秒启，生产切 PG 一行配置 |
| 传输 | HTTP + stdio 双模式 | 单一传输 | 运维需要 Web，IDE Agent 需要本地 |
| LLM | 宿主机推理优先，内置可选 | 每条都调 LLM | 省钱（不重复推理）、防幻觉传播 |
| 前端验证 | Playwright 可选依赖 | 硬依赖 / Selenium | headless 稳定、不安装不影响 API 功能 |
| 安全 | fail-closed（默认拒绝） | fail-open | 线上安全第一，不存侥幸 |

**三、线上踩过的坑与解决**

1. **"静默失败"检测不到** → spec_store + assert_engine 做 ground truth，不在依赖"有没有报错"
2. **LLM 给错误结论扩散** → verify 结果写入 spec_diffs，Dashboard 可视化覆盖模型判断
3. **环境差异导致故障** → 存储工厂一行切换，中间件 fail-closed 不因环境降权
4. **工具注册重复代码** → `register_all_tools()` 统一入口，HTTP/stdio 双传输共用，零重复

**四、从开发到生产只需要改什么**

```
STORAGE_BACKEND=postgresql   # 一行切 PG
STATE_BACKEND=redis          # 一行切 Redis（限流计数、多实例共享）
API_KEY=your-secret          # 开启鉴权
LLM_PROVIDER=zhipu           # 国内环境切换（openai 兼容协议，只改 base_url）
```

业务代码零改动，171 个测试保证回归安全。

**Docker Compose 一键部署**：

```bash
git clone <repo>
cp .env.example .env
# 编辑 .env 填入 API Key
docker-compose up -d
# → PostgreSQL + Redis + App 全部就绪
```

---

## 四、常见追问 & 防守回答

### "规范谁来写？写错了怎么办？"

规范有三个来源：
1. 开发者根据需求/OpenAPI 文档手动写
2. `spec.py` 自动扫描项目里的 CONVENTION/README/.cursorrules 文件
3. 浏览器 SDK 自动采集前端交互行为作为规范模板

如果写错了：`verify` 输出 diffs 列表，人 review 后通过 `PATCH /api/spec/{id}` 修正。系统本身只做比对，不自己改规范。规范受 API Key 鉴权保护。

### "171 个单测，你做了集成测试吗？"

分层测试策略：
- 单测：纯函数逻辑（断言引擎、规范存储、上下文构建）— 171 个
- 契约测试：TestClient + FastAPI router 测试端点 → handler 的完整链路
- 集成：`/health` 验证 PG 连通性（生产环境）；Playwright 通过 `is_available()` 标志自动跳过（未安装不影响测试）

集成测试未纳入 CI 的原因：真实 Playwright 启动 headless Chromium 耗时长、不稳定；PG 需要容器化环境。决策：模块级别保证独立性验证，端到端留给灰度发布验证。

### "规范量涨到 10 万条怎么办？"

dict+Lock 在这个量级确实扛不住。但存储层预留了工厂模式：

```python
# 当前
from app.mcp.verifier import spec_store  # dict+Lock

# 升级后
from app.mcp.core.storage.factory import get_spec_store
spec_store = get_spec_store()  # memory ↔ postgresql 一键切换
```

需要加的分页、索引、缓存都已有架构预留，业务逻辑层（assert_engine、verify_handler）不依赖存储实现。

### "为什么不用 LangChain / LangGraph？"

评估过。LangChain 的价值在于：把 LLM 调用链编排成 DAG。但我们的核心场景不是"编排 LLM 调用链"，而是：
1. 确定性比对（不需要 LLM）
2. 结构化数据采集和传递（不需要 Chain）
3. 工具暴露给宿主 AI（MCP 协议更适合）

LangChain 引入的抽象层反而增加了延迟和不确定性。我们的选择：用最少的依赖（FastAPI + MCP + Playwright）解决最核心的问题。

### "实际跑项目遇到过什么真实 bug？"（实战故事，强烈推荐讲）

**现象**：服务启动正常，但所有 POST 请求（`/api/debug/run`、`/api/spec` 等）都返回 422 `{"detail":[{"loc":["body"],"msg":"Field required","input":null}]}`，即路由收到空 body。

**定位过程**：
1. 先怀疑 PowerShell/curl 发 body 的问题，换 `curl.exe` + 文件 body 仍 422；
2. 再用 Python `urllib`（完全绕过 curl）发请求，还是 422 → **确认是服务端问题**，不是客户端；
3. 422 的 `loc:["body"], input:null` 说明 body 在到达路由前就丢了，锁定中间件层；
4. 逐个看中间件，`MaxBodySizeMiddleware` 嫌疑最大——它在 `BaseHTTPMiddleware` 里用 `request.stream()` 消费 body 后，靠 `request._receive = receive` 重放。

**根因**：`BaseHTTPMiddleware` 中读取 body 并通过私有属性 `request._receive` 重放的写法，在 Starlette 1.3 新版下失效，下游路由读到空 body。单元测试用 TestClient 没暴露，因为 TestClient 的请求执行路径与真实 HTTP 不同，没触发这个重放失效。

**修复**：中间件改为只做 `Content-Length` 硬检查（超限直接 413），不再在中间件层流式消费 body，body 读取交给路由层。修复后 POST 请求正常返回 200。

**价值**：(1) 单测过 ≠ 真能跑，TestClient 与真实 HTTP 路径有差异，必须实跑验证；(2) `BaseHTTPMiddleware` 读 body 重放是脆弱模式，应避免在中间件层消费 body。

**追问预判**：
- Q：为什么 TestClient 没发现？→ TestClient 直接调 ASGI app，body 传递路径与真实 HTTP 不同，没触发中间件重放失效。教训：关键链路要实跑 HTTP 验证。
- Q：改成 Content-Length 检查会不会降级安全？→ 对带 Content-Length 的请求（绝大多数）防护不变；仅对 chunked 不带 CL 的请求失去流式拦截，可接受，后续可用纯 ASGI 中间件补。
- Q：为什么 BaseHTTPMiddleware 重放会失效？→ 它内部用 spawned task 转发请求，request 在 call_next 链中重新构造，私有 `_receive` 重设在下游未必生效，Starlette 新版对此更严格。

### "你的 API Key 鉴权有重放攻击漏洞，怎么处理？"（安全分析题）

**分析过程**：

先确认攻击面是否存在——重放攻击的前提是"攻击者能截获网络请求"。我的项目有三条路径：

| 路径 | 传输方式 | 能否被截获 | 重放风险 |
|------|---------|-----------|---------|
| Trae/Qoder/Codex → MCP Server | stdio 进程管道 | ❌ 不走网络 | 无 |
| demo.py → HTTP localhost | 本地回环 | ❌ 本机内 | 极低 |
| 未来公网部署 → HTTP 远程 | TCP 网络 | ✅ 可截获 | 有 |

**结论**：当前部署模式下重放风险不存在。三个客户端全是 stdio 本地子进程，API Key 通过环境变量传入，不走网络，攻击者无法截获。

**为什么不加 HMAC 签名/nonce/时间戳**：

1. 会破坏三端集成——Trae/Qoder/Codex 的 MCP 配置只传 API Key，加签名要求客户端改代码
2. 违反 MCP 官方规范——规范明确："stdio 传输不应遵循 HTTP 授权标准，应从环境获取凭据"
3. 风险驱动安全原则——攻击面概率为 0 时，任何安全方案的价值为负（实施成本 > 收益）

**未来公网部署的方案**：

按 MCP 官方规范上 OAuth 2.1——短期 access_token（防重放）+ PKCE（防授权码拦截）+ 受众验证 RFC 8707（防令牌透传）。不在非标 HMAC 方案上浪费时间。

**一句话标准答**：
> "我分析过重放攻击风险。当前三端都是 stdio 本地通信，API Key 通过 env 传入不走网络，重放攻击前提不成立。如果未来公网部署，按 MCP 官方规范上 OAuth 2.1 + PKCE，天然防重放。核心原则：风险驱动安全，不为不存在的攻击面写代码。"

---

## 五、AI-Native 开发方法论与我的关键取舍决策

> 本节用于回答"你这项目是自己写的吗 / 你怎么用 AI / 遇到分歧怎么决策"类问题。
> 核心定位：**我是唯一开发者与决策层，AI 是并行产能工具。**

### 4.1 我的真实开发模式：双模型并行生成 + 人工择优整合

工作流：
1. **并行**：让两个大模型针对同一需求/同一份参考代码，各自产出方案与实现；
2. **择优**：取各家长处（如 A 架构清晰、B 边界处理稳），合并成最优解；
3. **加约束**：我额外补安全/架构纪律（白名单、超时、脱敏、不推倒重写）；
4. **拍板验收**：看测试是否跑通、复查关键安全逻辑（鉴权默认拒绝、参数化查询防注入）。

为什么这比"单模型写代码"强：
- 两个模型互为交叉验证，降低单模型盲区与幻觉；
- 人把住架构与安全，AI 易过度实现（如照搬不适用依赖），由我砍掉；
- 方案可追溯（MIGRATION.md 这类"计划先行"文档即是产出物）。

简历写"独立开发"如何解释：我是唯一对结果负责的开发者，做选型、取舍、拍板；AI 提供并行产能，我做决策与验收。这恰恰是 AI-native 工程师的核心能力，而非减分项。

**30 秒标准陈述（可直接背）：**
> "这个项目我用多模型协同开发：让两个大模型并行读需求和参考实现、各自出方案，我负责择优整合——比如参考项目用 SQLite，我评估后没照搬，选了 memory+PG 双后端；AI 想引入 tenacity 重试和 Playwright，我判断是多余依赖就砍了；同时我额外加了 git 白名单+超时、入库前统一脱敏这些安全约束。AI 提供并行产能，我做架构纪律、安全把关和验收。最终 171 个测试全绿。"

### 4.2 我做的关键取舍决策（口述稿，均出自实际迁移记录）

**决策 1：存储不选 SQLite，选 memory / PostgreSQL 双后端**
- 背景：参考项目用 SQLite 做 trace 存储。
- 我的决策：不采用，改在现有存储抽象（TraceStorage）之上实现等价 API，后端用 memory（开发零依赖）+ PostgreSQL（生产持久化+并发）。
- 为什么否 SQLite：SQLite 是单文件库，不支持多进程并发共享、容量受文件限制；本项目要支持多 worker / 多副本，PG 的连接池+事务更合适。
- 价值：一行配置（`STORAGE_BACKEND`）切换，业务零改动。

**决策 2：不搬参考项目的 tenacity 重试**
- 背景：参考项目用 tenacity 做重试。
- 我的决策：评估后不引入。
- 为什么否：本项目已有等效的重试+fallback 机制（analyzer 指数退避+fallback model），再引 tenacity 是多余依赖、增加复杂度。
- 价值：依赖更精简，维护面更小。

**决策 3：不搬浏览器 SDK 的 TS 文件**
- 背景：参考项目带一份前端 TS SDK。
- 我的决策：后端服务不内嵌前端制品；后端 `/ingest/*` 已就绪，前端要用再单独引。
- 为什么否：关注点分离，后端是 Python 服务，混进 TS 制品破坏边界。
- 价值：后端纯净，前端可独立演进。

**决策 4：git 操作必须白名单 + 超时（我额外加的安全约束）**
- 背景：git blame / diff 要执行外部命令，有任意路径探测风险。
- 我的决策：要求加 `git_path_whitelist` + `git_timeout`，非白名单路径拒绝、超时返回 None。
- 价值：把"能执行 shell 命令"的能力收敛成受控、超时、不阻断的安全采集。

**决策 5：脱敏在入库前统一执行（我定规则）**
- 背景：trace/network/ui 数据可能含密码、token、手机号。
- 我的决策：设定"入库前统一脱敏"，新建 redaction 模块在各采集器入库前调用。
- 价值：敏感信息不出服务边界，降数据泄露风险。

**决策 6：不推倒重写，保留核心抽象**
- 背景：参考项目结构不同。
- 我的决策：严格保留本项目的 TraceStorage/SessionStorage 抽象、安全中间件栈、协议/传输层，只按原有架构"重新实现"好特性，不复制粘贴。
- 价值：架构纪律，避免引入未知 bug、保证存量测试仍绿。

**决策 7：Playwright 设为可选依赖**
- 背景：前端自动遍历需 Playwright。
- 我的决策：设为可选，未安装时 `is_available()` 自动跳过，不影响 API/verify 核心功能。
- 价值：核心功能零外部强依赖，部署更轻。

---

> 简历内容请见 [RESUME.md](./RESUME.md)。
