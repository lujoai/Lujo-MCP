# ai-debug-mcp

基于 MCP（Model Context Protocol）协议的 AI 智能调试服务 —— 规范驱动 + 静默失败检测 + 多 Agent 协同 + UI 自动验收 + 浏览器网络请求捕获。

## 项目介绍

ai-debug-mcp 是一款面向开发者的智能调试平台，致力于解决以下痛点：

1. **静默失败检测** — 接口返回 200、无异常日志，但功能实际不对（如按钮没反应、字段缺失），传统监控完全查不出来
2. **多 Agent 协同调试** — 代码报错后需要手动查日志、翻代码、拼提示词再丢给 AI，每次耗时 5–15 分钟
3. **前端网络盲区** — 前端请求细节（请求体、响应体、耗时）难以追踪，问题定位困难

## 核心功能

### 后端调试能力
- **请求追踪** — 自动记录每个请求的完整执行链路（时间、步骤、数据）
- **调试上下文构建** — 将原始追踪日志转换为 AI 可理解的结构化上下文
- **异常堆栈捕获** — 捕获异常调用栈、局部变量、源码行号
- **运行时快照** — 采集系统/进程/解释器状态（CPU、内存、线程等）
- **LLM 智能分析** — 对接智谱 GLM-4.5-Air / OpenAI，自动分析错误根因并给出修复建议
- **规范驱动 + verify 自动断言** — 定义期望规范，系统自动比对实际结果，检测"返回正常但不符合规范"的静默失败
- **UI 自动验收** — auto_test 自动遍历页面所有可交互元素，捕获控制台错误和网络 4xx/5xx

### 浏览器 SDK 能力（V2 Network Capture）
- **网络请求拦截** — 同时支持 XMLHttpRequest 和 fetch 请求
- **请求体安全序列化** — 支持 String、FormData、Blob、ArrayBuffer、URLSearchParams
- **响应体捕获** — 自动截取响应体前 2000 字符
- **采样控制** — `networkSampleRate` 控制采样比例（0-1）
- **节流控制** — `networkThrottleMs` 控制相同请求间隔上报
- **SDK 自排除** — 防止上报请求递归捕获
- **敏感信息脱敏** — 自动脱敏 password、token、secret、authorization 字段

## 系统架构

采用五层分层架构：

```
┌─────────────────────────────────────────────────────────────┐
│                      传输层 (Transport)                      │
│  MCP (JSON-RPC 2.0) / HTTP REST / WebSocket                 │
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

> 详细架构设计（含架构图、模块关系、数据流）请查看 [DESIGN.md](./DESIGN.md)。

## 快速启动方式

### 方式一：Docker Compose（推荐）

一键拉起 PostgreSQL、Redis 和 App：

```bash
git clone https://github.com/your-username/ai-debug-mcp.git
cd ai-debug-mcp

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
- PostgreSQL 16（端口 5432）
- Redis 7（端口 6379）
- AI Debug MCP Server（端口 8000）

### 方式二：本地开发

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 配置
python -m uvicorn app.main:app --reload
```

### 环境变量配置

开发最小配置：

```
LLM_PROVIDER=zhipu                          # openai | zhipu | custom
OPENAI_API_KEY=your-zhipu-or-openai-key
LLM_MODEL=glm-4.5-air                       # 或 gpt-4o
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
# → {"status":"ok","service":"ai-debug-mcp","version":"0.3.0"}
```

## Demo 演示流程

1. **启动服务**：`docker compose up -d` 或 `uvicorn app.main:app --reload`
2. **访问网络捕获 Demo**：打开 `http://localhost:8000/demo`
3. **点击测试按钮**：测试 XHR/fetch 请求捕获、FormData/Blob 请求、采样率控制等
4. **查看 AI 调试**：打开 `http://localhost:8000/dashboard` 查看追踪记录和 AI 分析结果

## 已完成能力列表

### V1 核心调试能力
- ✅ 请求追踪（Trace）
- ✅ 调试上下文构建（Context）
- ✅ 异常堆栈捕获（Stacktrace）
- ✅ 运行时快照（Runtime）
- ✅ LLM 智能分析（Analyzer）
- ✅ MCP 工具集（HTTP 15 个 / stdio 14 个）
- ✅ 规范驱动 + verify 自动断言
- ✅ UI 自动验收（auto_test）

### V2 浏览器网络捕获能力
- ✅ XMLHttpRequest 拦截（open/send hook）
- ✅ fetch 请求拦截
- ✅ 请求体安全序列化（String/FormData/Blob/ArrayBuffer/URLSearchParams）
- ✅ 响应体捕获
- ✅ networkSampleRate 采样控制
- ✅ networkThrottleMs 节流控制
- ✅ SDK 自排除（防递归）
- ✅ captureNetwork 默认开启
- ✅ onNetworkCapture 回调 API
- ✅ 敏感信息脱敏

## 项目状态

| 指标 | 状态 |
|------|------|
| MCP 工具数 | HTTP 15 / stdio 14 |
| 测试覆盖 | 以当前实际 pytest 运行结果为准 |
| 存储后端 | PostgreSQL（生产）/ memory（默认）|
| LLM Provider | openai / zhipu / custom |
| Dashboard | Web 控制台（实时读取 PostgreSQL） |
| 集成测试 | PGStore + Dashboard + MCP Tools + LLM |
| 当前阶段 | V2 Network Capture 完成，Release Preparation |

## 项目结构

```
ai-debug-mcp/
├── app/
│   ├── main.py               # FastAPI 应用入口
│   ├── api/                   # REST API 路由
│   ├── llm/                   # LLM 分析模块
│   ├── mcp/                   # MCP 核心模块
│   │   ├── tools/             # MCP 工具（HTTP 15 / stdio 14）
│   │   ├── protocol/          # JSON-RPC 协议实现
│   │   ├── core/              # 核心引擎 + 存储抽象
│   │   ├── builders/          # 数据构建器
│   │   ├── collectors/        # 数据采集器
│   │   ├── verifier/          # 断言引擎
│   │   ├── hooks/             # 异常钩子
│   │   └── transports/        # 传输层
│   ├── middleware.py          # 中间件栈
│   └── config.py              # 统一配置
├── browser-sdk/               # 浏览器 SDK（V2 Network Capture）
│   └── ai-debug.js            # SDK 核心文件
├── app/web/                   # Web 演示页面
│   └── network_capture_demo.html  # 网络捕获演示页面
├── migrations/                # SQL 迁移文件
├── scripts/                   # 一键式脚本
├── tests/                     # 测试
├── docker-compose.yaml        # Docker Compose 配置
└── .env.example               # 环境变量模板
```

## 文档导航

| 文档 | 用途 |
|------|------|
| [DEMO_GUIDE.md](./DEMO_GUIDE.md) | Demo 演示指南 |
| [PROJECT_SUMMARY.md](./PROJECT_SUMMARY.md) | AI 上下文入口（AI 第一阅读文件） |
| [AI_RULES.md](./AI_RULES.md) | AI 开发规则 |
| [AI_HANDOFF.md](./AI_HANDOFF.md) | AI 交接状态 |
| [PRD.md](./PRD.md) | 产品需求 |
| [DESIGN.md](./DESIGN.md) | 技术架构设计 |
| [DEV_PLAN.md](./DEV_PLAN.md) | 当前开发计划 |
| [CODE_REVIEW.md](./CODE_REVIEW.md) | 长期技术路线 |

## 测试

```bash
# 运行全部测试（需要 PostgreSQL 运行中）
python -m pytest tests/ --tb=short -q

# 仅运行单元测试
python -m pytest tests/unit/ --tb=short -q

# 仅运行集成测试
python -m pytest tests/integration/ --tb=short -q
```

测试覆盖：
- **单元测试**（`tests/unit/`）：redaction、fingerprint、storage、dashboard、verify_api 等
- **集成测试**（`tests/integration/`）：API 端点、debug flow、PostgreSQL 集成
- **PG 集成测试**（`tests/integration/test_pg_integration.py`）：PGStore 连接、Dashboard 读取、MCP Tools 读取、LLM 分析