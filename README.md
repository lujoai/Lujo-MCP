# Lujo-MCP

**Lujo-MCP is an MCP Runtime Debugging Context Server for AI coding agents.**

让 Claude、Cursor、Trae 等 AI coding agents 获得**真实运行的 Debug Context** —— 不是只读你的代码，而是看到真实 Bug 运行现场。

> 一个 **Model Context Protocol (MCP) Server**，面向 AI debugging / Runtime debugging / Browser debugging 场景的 **Developer Tool & Observability** 基础设施。

## Why Lujo-MCP?

传统 AI 调试的问题：**AI 可以看代码，但看不到真实运行现场。**

- **Browser runtime errors** — 控制台异常
- **Console logs** — 控制台日志
- **Network failures** — 网络请求失败（请求体 / 响应体 / 耗时）
- **User interaction traces** — 用户点击 / 交互轨迹
- **Session context** — 会话上下文
- **Debugging workflow** — 调试工作流

这些对 AI 来说通常是黑盒。Lujo-MCP 作为 **Runtime Debug Context Layer**，把上述信息采集、整理成 AI 可直接理解的结构化 Debug Context，通过 MCP 标准协议提供给宿主 AI Agent。

> Lujo-MCP 是 AI coding assistant 的「眼睛」与 Debug Context Infrastructure —— **不是另一个 Agent**，不替代 LLM 推理，而是把真实运行现场喂给宿主 AI。

![Lujo-MCP Runtime Context Architecture](./docs/public/images/lujo-runtime-context-architecture.svg)

> Lujo-MCP = **Context Provider**，不是 Agent：为 AI coding agent 提供 Runtime Debug Context，推理与修复决策由宿主 AI（Claude / Cursor / Trae）完成。

## Features

- **Runtime error capture** — 捕获 browser runtime errors / exceptions / stacktrace / 源码行号
- **Browser context collection** — 浏览器 SDK 采集 UI events、console logs、用户交互
- **Network tracing** — 拦截 XHR / fetch，捕获请求体、响应体、耗时与网络错误
- **User action tracking** — 记录点击 / 提交后的 DOM、路由、网络变化（UI 静默失败检测）
- **AI debugging context** — 把以上信息组装为 AI 可理解的结构化 Debug Context
- **Debug Experience Retrieval（RAG-based）** — 通过历史 Debug Experience 检索（fingerprint recall / message normalization / vector fallback）增强 AI 分析

## Supported Clients

- **Claude Desktop**
- **Cursor**
- **Trae**
- **任何兼容 MCP 的客户端**（stdio / Streamable HTTP）

## Installation

```bash
npm install -g @lujoai/lujo-mcp
```

一条命令安装即用，无需配置 Python 环境；三平台（Windows / macOS / Linux）二进制自动选择。详见下方「快速启动方式」。

## Quick Start

在 MCP 客户端（Claude Desktop / Cursor / Trae）配置：

```json
{
  "mcpServers": {
    "lujo": {
      "command": "lujo-mcp-server",
      "args": []
    }
  }
}
```

接入后即可通过 MCP 工具枚举获得 `get_debug_context` / `get_stacktrace` / `get_runtime_snapshot` 等 18 个工具。详见下方「MCP Client 接入」。

***

## 当前状态（Current Status）

**Lujo-MCP v0.5.0**（npm `@lujoai/lujo-mcp@0.5.0`，开箱即用）

> 版本统一：app / npm / README / CHANGELOG / MCP serverInfo / git tag 均为 `0.5.0`。
> 架构冻结（Architecture Frozen）：允许 Agent → RAG；禁止 Runtime → RAG/Agent/LLM/MCP、RAG → Agent/Runtime/LLM/MCP。

## 能力分层（Capability Tiers）

> Lujo-MCP 的能力按成熟度分为三层：**稳定能力**（开箱即用、默认可用）、**实验能力**（需环境或开关启用）、**未来规划**（Roadmap）。功能不变，仅按可交付状态归层。

### 🟢 稳定能力（Stable — 默认可用，开箱即用）

无需额外配置即可使用：

**后端调试**
- **请求追踪** — 自动记录每个请求的完整执行链路（时间、步骤、数据）
- **调试上下文构建** — 将原始追踪日志转换为 AI 可理解的结构化上下文
- **异常堆栈捕获** — 捕获异常调用栈、局部变量、源码行号
- **运行时快照** — 采集系统/进程/解释器状态（CPU、内存、线程等）
- **指纹知识库** — 基于错误指纹复用历史分析结论，命中时优先返回，并在 LLM 成功后自动沉淀
- **向量检索 RAG（in-process）** — 零依赖 Jaccard 相似度召回，精确指纹 miss 后 fallback
- **规范驱动 + verify 自动断言** — 定义期望规范，系统自动比对实际结果，检测"返回正常但不符合规范"的静默失败
- **errors 持久化聚合** — 异常自动入库 errors 表，支持指纹去重与聚合统计
- **spec_store 独立表** — 规范持久化到独立表，支持 CRUD 与审计追溯

**浏览器 SDK（V2-V6）**
- **网络请求拦截** — 同时支持 XMLHttpRequest 和 fetch 请求
- **请求体安全序列化** — 支持 String、FormData、Blob、ArrayBuffer、URLSearchParams
- **响应体捕获** — 自动截取响应体前 2000 字符
- **批量上报** — V2 批量上报 + sendBeacon 兜底，减少请求次数
- **网络错误自动标记** — V3 自动把 fetch / XHR 失败转为静默失败，并支持 `reportNetworkError()`
- **SDK trace_id 关联** — V4 初始化即生成 trace_id，并贯穿上报链路
- **增强 ingest** — V5 支持分类型批量入库，便于服务端按事件类别处理
- **UI 静默失败自动检测** — V6 对点击 / 提交后的 DOM、路由、网络变化做观察窗口判定
- **采样 / 节流控制、SDK 自排除、敏感信息脱敏** — 采样率、节流间隔、自排除、password/token/secret/authorization 字段脱敏

**传输与安全**
- **MCP 双传输** — stdio + Streamable HTTP，18 个工具
- **安全中间件** — fail-closed 鉴权 + 多 key 恒定时间比较轮换 + RBAC 角色分级（admin/developer/viewer）+ LFI/SSRF 防护
- **Prometheus `/metrics`** — 指标暴露

> 验证：已通过完整回归测试（测试基线详见「项目状态」）。

### 🟡 实验能力（Experimental — 需环境或开关启用）

默认关闭或需外部依赖，按需启用：

- **LLM 智能分析** — 对接智谱 / OpenAI（AsyncOpenAI 异步调用），默认免费模型 GLM-4.7-Flash；需配置 `OPENAI_API_KEY`
- **异步分析削峰队列** — 有界 `asyncio.Queue` + K 常驻消费协程 + 信号量对齐 LLM RPM/TPM；队列满返回 429；优雅停机 drain
- **多级缓存** — L1(LRU) + L2(Redis) 多级缓存，减少重复 LLM 调用（需 Redis）
- **Debug Experience Retrieval（RAG-based）** — Debug Experience 历史检索，`debug_experience_enabled` 默认 False；三层检索（L1 fingerprint recall / L2 message normalization / L3 vector fallback），关闭状态零调用零耗时
- **Qdrant 向量检索（语义召回）** — 需 Qdrant；不可用时静默降级
- **Agent-assisted Debug Workflow（Experimental）** — Lujo-MCP **本身不是 Agent**，不负责自主推理或决定修复方案；该能力仅作为未来 AI-assisted debugging workflow 扩展，`agent_enabled` 默认 False：`RepairAgent`（先行，产出 `repair_plan`）+ Multi-Agent Review DAG（`GitAgent` / `TestAgent` / `SecurityAgent` 并行审查，`AGENT-002`）
- **PostgreSQL / asyncpg 存储** — `STORAGE_BACKEND=postgresql`，需外部数据库
- **Playwright UI verify / auto_test** — 自动遍历页面可交互元素，捕获控制台错误和网络 4xx/5xx；需 Playwright 环境
- **熔断器、OpenTelemetry 导出** — 需对应依赖启用
- **Dashboard 实时 SSE 推送** — `DASH-SSE-001`，`DashboardEventBus` + SSE 端点
- **MCP 可观测性（D5）** — `DebugContextTrace`（request_id / Context 可用性 / Debug Experience 命中数 / 构建耗时 / Tool 响应耗时）
- **Benchmark 框架（D6）** — `benchmark/` 5 个标准 Debug Case + EvaluationMetrics

> 部分完成：MCP HTTP server→client notifications 已具备基础推送闭环，更丰富的通知类型仍待补充（见「未来规划」）。

### 🔵 未来规划（Roadmap）

> 只做规划，不包含 Auto Repair / Agent 自主修复 / 自动 Patch 等能力承诺。

- **More Debug Experience** — 扩充种子知识库与检索策略，提升 Debug Experience Retrieval 覆盖率
- **Better Context Collection** — 增强 UI Events / Network Trace 采集精度与采样控制
- **Enterprise Integration** — 认证（SSO/API Key 轮换）、审计、多实例观测集成
- **More MCP Protocol Capabilities** — 持续跟进 MCP 协议新能力（notifications / sampling）

## 系统架构

采用五层分层架构：

```
┌─────────────────────────────────────────────────────────────┐
│                      传输层 (Transport)                      │
│  MCP (JSON-RPC 2.0) / HTTP REST + stdio (WebSocket 规划中)  │
├─────────────────────────────────────────────────────────────┤
│                     中间件层 (Middleware)                    │
│  Auth / RateLimit / RequestID / ErrorHandler                │
├─────────────────────────────────────────────────────────────┤
│                    路由/分发层 (Router)                      │
│  MCP Tools / REST API / Ingest Endpoints                   │
├─────────────────────────────────────────────────────────────┤
│                      调试引擎 (Engine)                      │
│  Trace / Context / Collector / Verifier / Analyzer         │
├─────────────────────────────────────────────────────────────┤
│                    存储/状态层 (Storage)                     │
│  PostgreSQL / Memory / Redis                               │
└─────────────────────────────────────────────────────────────┘
```

> 详细架构设计（含架构图、模块关系、数据流）请查看 [DESIGN.md](./docs/public/DESIGN.md)。

> 📌 **SSE / Notifications**：当前 MCP Streamable HTTP 已支持 `GET /mcp` 长连接订阅、`POST` 单次 SSE 响应，以及 `POST Accept: text/event-stream` 到 `GET /mcp` 队列的结果桥接。当前已落地的 server→client 推送主要是 `session ready` 和请求结果下发，更丰富的 notifications 事件类型仍在后续迭代中。

## 快速启动方式

### 方式零：npm 全局安装（开箱即用）

无需配置 Python 环境，一条命令安装：

```bash
# 国内用户如遇 404，可使用官方源：
npm install -g @lujoai/lujo-mcp --registry=https://registry.npmjs.org/

# 或直接使用（镜像同步完成后）：
npm install -g @lujoai/lujo-mcp
```

安装完成后，在 MCP 客户端（Claude Desktop / Cursor / Trae 等）中配置：

```json
{
  "mcpServers": {
    "lujo-mcp": {
      "command": "lujo-mcp-server",
      "args": []
    }
  }
}
```

> ⚠️ stdio 模式需在 MCP 客户端配置 `LLM_PROVIDER`、`OPENAI_API_KEY` 等环境变量。

### 方式一：Docker Compose（推荐）

一键拉起 PostgreSQL、Redis 和 App：

```bash
git clone https://github.com/lujoai/Lujo-MCP.git
cd Lujo-MCP

# 复制环境变量模板
cp .env.example .env

# 编辑 .env，填入你的 API Key
# 最小配置只需设置 OPENAI_API_KEY 或使用智谱
# LLM_PROVIDER=zhipu
# OPENAI_API_KEY=your-zhipu-api-key

# 启动所有服务
docker compose up -d
```

服务启动在 `http://localhost:8000`，包含：

- PostgreSQL 16（仅 Docker 内部网络可达）
- Redis 7（仅 Docker 内部网络可达）
- AI Debug MCP Server（端口 8000，映射到宿主机）

### 方式二：本地开发

```bash
# 生产部署：仅安装运行时依赖
pip install -r requirements.txt

# 本地开发：安装运行时 + 测试/lint 工具（pytest / ruff / pytest-asyncio）
pip install -r requirements-dev.txt

cp .env.example .env
# 编辑 .env 配置
python -m app.main
```

### 环境变量配置

环境固化约定：

- 应用本身以 `PG_HOST` / `PG_PORT` / `PG_DATABASE` / `PG_USER` / `PG_PASSWORD` 为权威来源
- `POSTGRES_PASSWORD` 仅供 `docker compose` 初始化 PostgreSQL 服务使用，建议与 `PG_PASSWORD` 保持一致
- `DATABASE_URL` 仅作外部工具兼容，应用本身不会读取；若密码含 `@` 等特殊字符，必须先 URL 编码
- 出现本地 PG 连接问题时，先核对 `.env` 中的 `PG_PASSWORD`，再排查服务端配置

开发最小配置：

```
LLM_PROVIDER=zhipu                          # openai | zhipu | custom
OPENAI_API_KEY=your-zhipu-or-openai-key
LLM_MODEL=glm-4.7-flash                     # 智谱免费模型；也可换 gpt-4o 等
LLM_FALLBACK_MODEL=glm-4-flash
```

> **自定义你自己的 API（开箱即用，零代码改动）**
> 项目通过环境变量解耦 LLM provider，任何人都能填自己的 Key 和模型：
>
> | 变量               | 说明                               | 示例                                |
> | ---------------- | -------------------------------- | --------------------------------- |
> | `LLM_PROVIDER`   | 厂商：`openai` / `zhipu` / `custom` | `zhipu`                           |
> | `OPENAI_API_KEY` | 你的 API Key（变量名沿用 OpenAI SDK 约定）  | `your-key`                        |
> | `LLM_MODEL`      | 模型名，任意兼容端点支持的模型                  | `glm-4.7-flash`                   |
> | `LLM_BASE_URL`   | 自定义端点（留空则按 provider 自动选）         | `https://my-proxy.example.com/v1` |
>
> - **智谱（免费）**：`LLM_PROVIDER=zhipu` 时 base\_url 自动设为 `https://open.bigmodel.cn/api/paas/v4/`，模型填 `glm-4.7-flash`（免费纯文本）即可，无需付费。
> - **自建 / 第三方兼容端点**：`LLM_PROVIDER=custom` 并填 `LLM_BASE_URL` + `LLM_MODEL`，即可接入任意 OpenAI 兼容服务（如本地 Ollama、vLLM、代理网关）。
> - **OpenAI**：`LLM_PROVIDER=openai`，模型填 `gpt-4o` 等。

生产部署额外配置（业务代码零改动）：

```
STORAGE_BACKEND=postgresql   # memory | postgresql
STATE_BACKEND=redis          # memory | redis（限流计数）
API_KEY=your-secret          # 开启 fail-closed 鉴权
LLM_PROVIDER=zhipu           # openai | zhipu | custom（智谱免 VPN）
```

### 健康检查

```bash
curl http://localhost:8000/
# → {"status":"ok","service":"Lujo-MCP","version":"0.5.0"}
```

## MCP Client 接入（MCP Client Setup）

Lujo-MCP 作为 MCP Server，通过 **stdio**（进程管道）或 **Streamable HTTP**（`/mcp` 端点）为 AI Agent（Claude / Cursor / Trae 等）提供真实运行现场。两种模式配置模板如下：

**stdio 配置模板**（默认推荐，进程内通信）

```json
{
  "mcpServers": {
    "lujo": {
      "command": "lujo-mcp-server",
      "args": []
    }
  }
}
```

> 未安装 npm 包时，也可改用源码方式：`"command": "python", "args": ["-m", "app.mcp_server"]`，并把工作目录指向仓库根目录。

**HTTP 配置模板**（先启动服务 `python -m app.main` 或 `docker compose up -d`）

```json
{
  "mcpServers": {
    "lujo": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

### Claude Desktop

- 配置位置：`claude_desktop_config.json`（应用菜单 Settings → Developer → Edit Config）
- 填入上述 stdio 或 HTTP 配置模板中的 `mcpServers` 段

### Cursor

- 配置位置：项目根 `.cursor/mcp.json` 或全局 `~/.cursor/mcp.json`
- 填入 `mcpServers` 段；HTTP 模式需先启动 Lujo-MCP 服务

### Trae

- 配置位置：MCP 管理面板（模型配置 → MCP Server → 添加）
- 填入 stdio 或 HTTP 配置

> ⚠️ stdio 模式需在 MCP 客户端环境变量中配置 `LLM_PROVIDER`、`OPENAI_API_KEY` 等（见下方「环境变量配置」）。接入后即可通过工具枚举获得 `get_debug_context` / `get_stacktrace` / `get_runtime_snapshot` / `search_logs` / `list_recent_traces` 等 18 个工具。

## Demo 演示流程

1. **启动服务**：`docker compose up -d` 或 `python -m app.main`
2. **访问网络捕获 Demo**：打开 `http://localhost:8000/demo`
3. **点击测试按钮**：测试 XHR/fetch 请求捕获、网络错误自动上报、FormData/Blob 请求、采样率控制等
4. **按需验证静默失败 Demo**：当前仓库提供 `app/web/silent_failure_demo.html` 作为本地演示页，用于手动验证 UI 静默失败自动检测
5. **查看 AI 调试**：打开 `http://localhost:8000/dashboard` 查看追踪记录和 AI 分析结果

### AI 调用 MCP 工具获取 Debug Context

接入后，宿主 AI（Claude / Cursor / Trae）可在调试对话中直接调用 MCP 工具，拿到真实运行现场辅助定位：

```text
你（AI Agent）：调用 lujo.get_debug_context
参数：{ "trace_id": "t_20260811_...", "include_stacktrace": true }

返回（Debug Context）：
- exception_type / message        # 异常类型与信息
- stacktrace                       # 调用栈（含源码行号）
- runtime_snapshot                 # 系统/进程/解释器状态
- git_context                      # 当前提交 / 分支 / 变更
- network_trace / ui_events        # 前端请求与交互事件
- debug_experience                 # 历史调试经验（Debug Experience Retrieval）
```

宿主 AI Agent 基于 Lujo-MCP 提供的结构化 Runtime Debug Context 进行根因分析与修复建议生成，无需人工手动整理日志和运行现场信息。

## Benchmark（Phase 3 D6）

Lujo-MCP 内置 Benchmark 框架（`benchmark/`），用 5 个标准 Debug Case（api\_500 / frontend\_blank / db\_error / auth\_403 / perf\_slow）对比「无上下文」与「带 Lujo Context」两类输入下的 AI Debug 能力（EvaluationMetrics：命中率 / 定位精度 / 修复质量 / 耗时）。

```bash
python -m benchmark.runner list   # 列出用例
python -m benchmark.runner show api_500   # 查看单个用例
python -m benchmark.runner quality        # QualityScorer 旁证评估
```

> Benchmark 属于 Experimental Capability，用于评估 Runtime Debug Context 对 AI Debug 能力提升的影响。Benchmark 与 QualityScorer 是两个独立体系：前者度量外部 AI 在 Debug Context 加持下的能力提升，后者度量 Lujo-MCP 自身 Context 的完整度/可信度。

## 项目状态

| 指标      | 状态                                                                                                                                                                                                                                                                                                                             |
| ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| MCP 工具数 | HTTP 18 / stdio 18（含 `repair_async` / `repair_result` / `resolve_stack`）                                                                                                                                                                                                                                                                   |
| 测试基线    | 单元 `1087 passed / 6 skipped / 0 failed`（含 AI Debug Agent Phase 1 63 项 + Phase 2 53 项 + Dashboard SSE 18 项 + Quality System 86 项 + Verify Loop 38 项 + M3 Fault Localization 2.0 48 项 + P1 Debug Experience RAG 26 项 + CODE\_REVIEW\_FIX\_PROMPT 回归测试 17 项 + stacktrace 工具与存储工厂边界 17 项 + D5 MCP 可观测性 16 项 + D6 Benchmark 框架 19 项 + v0.5.0 DebugContext Schema/Runtime Integration 与 Tool Category Metadata 45 项 + v0.5.1 Source Map 解析 94 项 + deepseek provider base_url 1 项） |
| 存储后端    | memory 默认可用；PostgreSQL / asyncpg 需依赖外部数据库环境                                                                                                                                                                                                                                                                                    |
| 稳定性能力   | 分区、归档、Redis L2、L3 缓存预热、熔断器、OTel、异步分析削峰队列均有真实代码，但需按环境启用并单独验证                                                                                                                                                                                                                                                                    |
| 安全能力    | fail-closed 鉴权 + 多 key 恒定时间比较轮换 + RBAC 角色分级（admin/developer/viewer）+ LFI/SSRF 防护                                                                                                                                                                                                                                               |
| 当前阶段    | Phase 0-6 全部完成；Phase 7 智能化（指纹知识库 + 向量检索 RAG in-process + Qdrant 语义召回 + AI Debug Agent Phase 1 单 Agent + Phase 2 多 Agent DAG）+ Phase 8 实时观测增强（Dashboard 实时 SSE 推送 `DASH-SSE-001`）均已落地；**v0.5.0 已发布**（2026-08-13：DebugContext 7→20 字段 Schema 对齐、MCP Tool Category Metadata、Prompt Injection Guard、API Schema Validation、Session 安全加固）；下一步为 v0.5.1 收口（**Source Map 解析已落地**：纯 Python VLQ 解码 + 上传/磁盘双通道 + `resolve_stack` 工具 + Quality/Benchmark A/B 实证，默认关闭）与 Browser SDK 压缩 e2e CI 验证                                                                                                 |
| 权威口径    | 项目功能状态与启用验证以内部文档为准                                                                                                                                                                                                                                                                                                             |
| 安全审查    | 安全加固代码已落地，实际启用边界与前提条件以运行环境配置为准                                                                                                                                                                                                                                                                                                 |

> ⚠️ **安全提示（v0.3.0 P0+P1+P2+P3 加固后）**：默认更安全——`0.0.0.0`+空 `API_KEY` 会拒绝启动、代码/Git 定位默认仅限项目根、Playwright 默认拒私网/云元数据/`file://`。因此：**本地免鉴权**运行请用 `HOST=127.0.0.1`；**本地联调 Playwright** 设 `UI_URL_ALLOW_PRIVATE=true`（或 `UI_URL_ALLOWLIST`）；读项目根外源码配 `WHITELIST_PATH_PREFIX`/`GIT_PATH_WHITELIST`。新增配置：`TOOL_TIMEOUT_SECONDS`（默认 60）/`UI_URL_ALLOW_PRIVATE`/`UI_URL_ALLOWLIST`/`DEBUG_ENDPOINTS_ENABLED`（默认 false）。Release Audit 全部收口：P0+P1+P2+P3 已全部修复。

> 详细路线图见项目内部路线图文档。

## 项目结构

```
Lujo-MCP/
├── app/
│   ├── main.py               # FastAPI 应用入口
│   ├── api/                   # REST API 路由
│   ├── agent/                 # AI Debug Agent 模块（Phase 1：BaseAgent ABC + RepairAgent + Coordinator + RepairQueue；Phase 2：GitAgent + TestAgent + SecurityAgent + DAG，共 11 文件）
│   ├── llm/                   # LLM 分析模块
│   ├── mcp/                   # MCP 传输层（Phase 0 解耦后仅保留协议/工具/传输）
│   │   ├── tools/             # MCP 工具（HTTP 18 / stdio 18）
│   │   ├── protocol/          # JSON-RPC 协议实现
│   │   └── transports/        # 传输层（stdio / Streamable HTTP / SSE）
│   ├── runtime/               # 运行时核心（Phase 0 解耦，MCP 依赖 runtime）
│   │   ├── core/              # 核心引擎（logs / errors / redaction / git / trace_repo）
│   │   │   └── storage/       # 存储后端（pg_store / async_pg_store / memory / factory / ddl）
│   │   ├── collectors/        # 数据采集器（stacktrace / network / static_analyzer）
│   │   ├── context/           # 上下文构建（builder / fault_localizer）
│   │   ├── verifier/          # 断言引擎（assert_engine / spec_store / ui_runner）
│   │   ├── hooks/             # 异常钩子
│   │   └── state/             # 状态存储
│   ├── middleware.py          # 中间件栈（安全栈）
│   ├── middleware_network.py  # 网络采集中间件（可选）
│   └── config.py              # 统一配置
├── browser-sdk/               # 浏览器 SDK（V2-V6）
│   └── ai-debug.js            # SDK 核心文件
├── app/web/                   # Web 演示页面
│   ├── dashboard.html         # Dashboard 控制台
│   ├── network_capture_demo.html  # 网络捕获演示（/demo）
│   ├── silent_failure_demo.html   # 静默失败演示
│   └── auto_test_demo.html        # 自动遍历演示
├── migrations/                # SQL 迁移文件
├── scripts/                   # 一键式脚本
├── tests/                     # 测试
├── docker-compose.yaml        # Docker Compose 配置
└── .env.example               # 环境变量模板
```

## 文档导航

| 文档                                   | 用途                            |
| ------------------------------------ | ----------------------------- |
| [DEMO.md](./docs/public/DEMO.md)     | 端到端演示场景（React Login Bug 完整流程） |
| [PRD.md](./docs/public/PRD.md)       | 产品需求                          |
| [DESIGN.md](./docs/public/DESIGN.md) | 技术架构设计                        |

## 测试

```bash
# 运行全部测试（集成测试需要 PostgreSQL 运行中，单元测试不需要）
python -m pytest tests/ --tb=short -q

# 仅运行单元测试（无需外部依赖）
python -m pytest tests/unit/ --tb=short -q

# 仅运行集成测试（需要 PostgreSQL/Redis）
python -m pytest tests/integration/ --tb=short -q
```

> ⚠️ **注意**：单元测试前请确保 `.env` 不含 `API_KEY`（SEC-03 鉴权会导致集成测试 401 失败）；集成测试需 PostgreSQL/Redis（`docker compose up -d`）。

MCP stdio 唯一启动命令：

```bash
python -m app.mcp_server
```

测试覆盖：

- **单元测试**（`tests/unit/`）：redaction、fingerprint、storage、dashboard、verify\_api 等
- **集成测试**（`tests/integration/`）：API 端点、debug flow、PostgreSQL 集成
- **PG 集成测试**（`tests/integration/test_pg_integration.py`）：PGStore 连接、Dashboard 读取、MCP Tools 读取、LLM 分析

