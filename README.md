# Lujo-MCP

基于 MCP（Model Context Protocol）协议的 AI 智能调试服务 —— 规范驱动 + 静默失败检测 + AI Debug Agent 自动修复 + UI 自动验收 + 浏览器网络请求捕获 + 指纹知识库复用。

## 项目介绍

Lujo-MCP 是一款面向开发者的智能调试平台，致力于解决以下痛点：

1. **静默失败检测** — 接口返回 200、无异常日志，但功能实际不对（如按钮没反应、字段缺失），传统监控完全查不出来
2. **多 Agent 协同调试** — 代码报错后需要手动查日志、翻代码、拼提示词再丢给 AI，每次耗时 5–15 分钟
3. **前端网络盲区** — 前端请求细节（请求体、响应体、耗时）难以追踪，问题定位困难

> 当前项目的功能完成度以 [真实交付功能矩阵](./docs/internal/DELIVERY_MATRIX.md) 为准。  
> 待开发项统一收敛在 [TODO 台账](./docs/internal/TODO.md)，稳定性启用状态见 [稳定性验证报告](./docs/internal/STABILITY_REPORT.md)。
> 需要启用 PG/asyncpg、Redis、Playwright、熔断器、OTel 时，请按 [环境部署与功能启用指南](./docs/public/ENABLEMENT_GUIDE.md) 操作。

## 核心功能

### 后端调试能力
- **请求追踪** — 自动记录每个请求的完整执行链路（时间、步骤、数据）
- **调试上下文构建** — 将原始追踪日志转换为 AI 可理解的结构化上下文
- **异常堆栈捕获** — 捕获异常调用栈、局部变量、源码行号
- **运行时快照** — 采集系统/进程/解释器状态（CPU、内存、线程等）
- **LLM 智能分析** — 对接智谱 GLM-4.5-Air / OpenAI（AsyncOpenAI 异步调用），自动分析错误根因并给出修复建议
- **异步分析削峰队列** — P3-6 有界 `asyncio.Queue(maxsize=N)` + K 常驻消费协程 + `asyncio.Semaphore(K)` 对齐 LLM RPM/TPM；队列满返回 429；优雅停机 drain；新增 `POST /api/debug/analyze/async` + `GET /api/debug/analyze/result/{job_id}`
- **多级缓存** — L1(LRU) + L2(Redis) 多级缓存，减少重复 LLM 调用
- **指纹知识库** — 基于错误指纹复用历史分析结论，命中时优先返回，并在 LLM 成功后自动沉淀
- **向量检索 RAG** — Phase 7 `VectorStore` ABC 纯检索语义（`add(docs)` / `search(query, top_k)`）；InProcessVectorStore（Jaccard 相似度，零依赖）+ QdrantVectorStore（OpenAI/智谱 Embeddings 语义召回）双后端；精确指纹 miss 后做向量召回 fallback；Qdrant 不可用时静默降级
- **AI Debug Agent（Phase 1 + Phase 2）** — 自动修复 + 多 Agent DAG 协同；`BaseAgent` ABC + `RepairAgent`（复用 `analyzer._get_async_client`，独立重试/fallback + 容错 JSON）+ `Coordinator` 编排器（Phase 1 单 Agent 串行 / Phase 2 多 Agent DAG 调度）+ `RepairQueue` 削峰队列；`RepairContextAssembler` 并发聚合 LLM 分析 + 向量召回 + Git diff，各失败静默降级；新增 `POST /api/debug/repair/async` + `GET /api/debug/repair/result/{job_id}` REST 端点与 `repair_async` / `repair_result` MCP 工具；`agent_enabled` 默认 False，向后兼容；**Phase 2 多 Agent DAG（`AGENT-002`，2026-07-30 落地）**：`RepairAgent`（先行，产出 `repair_plan`）→ `GitAgent` / `TestAgent` / `SecurityAgent`（并行审查，依赖 `repair_plan`）；`GitAgent` 纯 git 归因（不调 LLM），`TestAgent` 生成验证策略，`SecurityAgent` 做 10 类安全审查；`agent_multi_agent_enabled` 默认 False 走 Phase 1 串行（向后兼容），并行节点失败静默降级 + `dag_degraded` 信号
- **规范驱动 + verify 自动断言** — 定义期望规范，系统自动比对实际结果，检测"返回正常但不符合规范"的静默失败
- **UI 自动验收** — auto_test 自动遍历页面所有可交互元素，捕获控制台错误和网络 4xx/5xx
- **errors 持久化聚合** — 异常自动入库 errors 表，支持指纹去重与聚合统计
- **spec_store 独立表** — 规范持久化到独立表，支持 CRUD 与审计追溯

### 浏览器 SDK 能力（V2-V6）
- **网络请求拦截** — 同时支持 XMLHttpRequest 和 fetch 请求
- **请求体安全序列化** — 支持 String、FormData、Blob、ArrayBuffer、URLSearchParams
- **响应体捕获** — 自动截取响应体前 2000 字符
- **批量上报** — V2 批量上报 + sendBeacon 兜底，减少请求次数
- **网络错误自动标记** — V3 自动把 fetch / XHR 失败转为静默失败，并支持 `reportNetworkError()`
- **SDK trace_id 关联** — V4 初始化即生成 trace_id，并贯穿上报链路
- **增强 ingest** — V5 支持分类型批量入库，便于服务端按事件类别处理
- **UI 静默失败自动检测** — V6 对点击 / 提交后的 DOM、路由、网络变化做观察窗口判定
- **采样控制** — `networkSampleRate` 控制采样比例（0-1）
- **节流控制** — `networkThrottleMs` 控制相同请求间隔上报
- **SDK 自排除** — 防止上报请求递归捕获
- **敏感信息脱敏** — 自动脱敏 password、token、secret、authorization 字段

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

### 方式一：Docker Compose（推荐）

一键拉起 PostgreSQL、Redis 和 App：

```bash
git clone https://github.com/your-username/Lujo-MCP.git
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
LLM_MODEL=gpt-4o                            # 或 glm-4.5-air
LLM_FALLBACK_MODEL=glm-4-flash
```

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
# → {"status":"ok","service":"Lujo-MCP","version":"0.3.0"}
```

## Demo 演示流程

1. **启动服务**：`docker compose up -d` 或 `python -m app.main`
2. **访问网络捕获 Demo**：打开 `http://localhost:8000/demo`
3. **点击测试按钮**：测试 XHR/fetch 请求捕获、网络错误自动上报、FormData/Blob 请求、采样率控制等
4. **按需验证静默失败 Demo**：当前仓库提供 `app/web/silent_failure_demo.html` 作为本地演示页，用于手动验证 UI 静默失败自动检测
5. **查看 AI 调试**：打开 `http://localhost:8000/dashboard` 查看追踪记录和 AI 分析结果

## 真实交付状态摘要

- 默认可用能力：请求追踪、上下文构建、异常捕获、运行时快照、MCP 双传输、规范 CRUD、verify、指纹知识库命中与自动沉淀、向量检索 RAG（in-process）、Browser SDK V2-V6 采集、安全中间件、Prometheus `/metrics`
- 需依赖环境才能启用：LLM 分析、异步分析削峰队列、AI Debug Agent（自动修复，`agent_enabled` 默认 False）、PostgreSQL / asyncpg、Playwright UI verify / auto_test、Redis L2 缓存、L3 缓存预热、熔断器、OpenTelemetry 导出、Qdrant 向量检索（语义召回）
- 部分完成能力：MCP HTTP server→client notifications 已具备基础推送闭环，但更丰富的通知类型仍待补充；向量检索 RAG 抽象层与 in-process + Qdrant 双后端已完成；AI Debug Agent Phase 1（单 Agent `RepairAgent` + `BaseAgent` ABC 多 Agent 协同框架）+ Phase 2（多 Agent DAG：`GitAgent` + `TestAgent` + `SecurityAgent` 编排，`AGENT-002`，2026-07-30）均已落地，`agent_multi_agent_enabled` 默认 False 向后兼容

完整条目与代码位置请直接查看 [DELIVERY_MATRIX.md](./docs/internal/DELIVERY_MATRIX.md)。

## 项目状态

| 指标 | 状态 |
|------|------|
| MCP 工具数 | HTTP 17 / stdio 17（新增 `repair_async` / `repair_result`，FR19） |
| 测试基线 | 单元 `654 passed / 6 skipped / 0 failed`（含 AI Debug Agent Phase 1 63 项 + Phase 2 53 项 + Dashboard SSE 18 项 + Qdrant 适配器 23 项 + L3 预热 12 项 + 三轨并行 104 项） |
| 存储后端 | memory 默认可用；PostgreSQL / asyncpg 需依赖外部数据库环境 |
| 稳定性能力 | 分区、归档、Redis L2、L3 缓存预热、熔断器、OTel、异步分析削峰队列均有真实代码，但需按环境启用并单独验证 |
| 安全能力 | fail-closed 鉴权 + 多 key 恒定时间比较轮换 + RBAC 角色分级（admin/developer/viewer）+ LFI/SSRF 防护 |
| 当前阶段 | Phase 0-6 全部完成；Phase 7 智能化（指纹知识库 + 向量检索 RAG in-process + Qdrant 语义召回 + AI Debug Agent Phase 1 单 Agent + Phase 2 多 Agent DAG）+ Phase 8 实时观测增强（Dashboard 实时 SSE 推送 `DASH-SSE-001`）均已落地；下一步为 Browser SDK 压缩 e2e 联调、Docker 容器化复现实验 |
| 权威口径 | 功能状态见 [DELIVERY_MATRIX.md](./docs/internal/DELIVERY_MATRIX.md)，启用验证见 [STABILITY_REPORT.md](./docs/internal/STABILITY_REPORT.md) |
| 安全审查 | 安全加固代码已落地，实际启用边界与前提条件以运行环境配置为准，详见 [SECURITY_REVIEW.md](./docs/internal/SECURITY_REVIEW.md) |

> ⚠️ **安全提示（v0.3.0 P0+P1+P2+P3 加固后）**：默认更安全——`0.0.0.0`+空 `API_KEY` 会拒绝启动、代码/Git 定位默认仅限项目根、Playwright 默认拒私网/云元数据/`file://`。因此：**本地免鉴权**运行请用 `HOST=127.0.0.1`；**本地联调 Playwright** 设 `UI_URL_ALLOW_PRIVATE=true`（或 `UI_URL_ALLOWLIST`）；读项目根外源码配 `WHITELIST_PATH_PREFIX`/`GIT_PATH_WHITELIST`。新增配置：`TOOL_TIMEOUT_SECONDS`（默认 60）/`UI_URL_ALLOW_PRIVATE`/`UI_URL_ALLOWLIST`/`DEBUG_ENDPOINTS_ENABLED`（默认 false）。Release Audit 全部收口：P0+P1+P2+P3 已全部修复。

> 详细路线图见 [ROADMAP](./docs/internal/ROADMAP.md)

## 项目结构

```
Lujo-MCP/
├── app/
│   ├── main.py               # FastAPI 应用入口
│   ├── api/                   # REST API 路由
│   ├── agent/                 # AI Debug Agent 模块（Phase 1：BaseAgent ABC + RepairAgent + Coordinator + RepairQueue；Phase 2：GitAgent + TestAgent + SecurityAgent + DAG，共 11 文件）
│   ├── llm/                   # LLM 分析模块
│   ├── mcp/                   # MCP 核心模块
│   │   ├── tools/             # MCP 工具（HTTP 17 / stdio 17）
│   │   ├── protocol/          # JSON-RPC 协议实现
│   │   ├── core/              # 核心引擎 + 存储抽象
│   │   ├── builders/          # 数据构建器
│   │   ├── collectors/        # 数据采集器
│   │   ├── verifier/          # 断言引擎
│   │   ├── hooks/             # 异常钩子
│   │   └── transports/        # 传输层
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

| 文档 | 用途 |
|------|------|
| [DEMO_GUIDE.md](./docs/public/DEMO_GUIDE.md) | Demo 演示指南 |
| [PROJECT_SUMMARY.md](./docs/public/PROJECT_SUMMARY.md) | AI 上下文入口（AI 第一阅读文件） |
| [AI_RULES.md](./docs/internal/AI_RULES.md) | AI 开发规则 |
| [AI_HANDOFF.md](./docs/internal/AI_HANDOFF.md) | AI 交接状态 |
| [PRD.md](./docs/public/PRD.md) | 产品需求 |
| [DESIGN.md](./docs/public/DESIGN.md) | 技术架构设计 |
| [DEV_PLAN.md](./docs/internal/DEV_PLAN.md) | 当前开发计划 |
| [ROADMAP.md](./docs/internal/ROADMAP.md) | 长期路线图 |
| [CODE_REVIEW.md](./docs/internal/CODE_REVIEW.md) | 长期技术路线 |

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
- **单元测试**（`tests/unit/`）：redaction、fingerprint、storage、dashboard、verify_api 等
- **集成测试**（`tests/integration/`）：API 端点、debug flow、PostgreSQL 集成
- **PG 集成测试**（`tests/integration/test_pg_integration.py`）：PGStore 连接、Dashboard 读取、MCP Tools 读取、LLM 分析
