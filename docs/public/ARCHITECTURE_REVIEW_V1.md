# Lujo-MCP Architecture Review V1

> 状态：**Architecture Frozen**（架构冻结审查 V1）
> 日期：2026-08-07
> 范围：Runtime Layer 解耦完成后的分层边界冻结审查。本报告只审查、不修复；发现问题由后续迭代按边界规范解决。

---

## 1. 当前架构图

```
┌─────────────────────────────────────────────────────────────┐
│                     Entry / Facade 层                        │
│   app/main.py · app/mcp_server.py · app/api/* · middleware  │
│      （HTTP/SSE/stdio → MCP，应用组合边界）                    │
└──────────────────┬──────────────────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────────────────┐
│                    MCP Layer  (app/mcp)                      │
│   protocol/ (JSON-RPC) · transports/ (stdio/SSE/session)     │
│   tools/ (18 个 tool 定义与注册)                               │
│   ⚠ tools 作为编排边界，消费 runtime/llm/agent（见 F1/F2）      │
└──────────────────┬──────────────────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────────────────┐
│                  Runtime Layer  (app/runtime)                │
│   core/ (logs/errors/git/session/trace_repo/redaction)       │
│   core/storage/ (Trace/Session/Error/Spec 抽象 + 实现)        │
│   collectors/ (network/stacktrace/runtime/ui_event/...)      │
│   context/builder.py · verifier/ · hooks/                    │
└──────────────────┬──────────────────────────────────────────┘
                   │
        ┌──────────┴──────────┬──────────────┐
        ▼                     ▼              ▼
┌───────────────┐    ┌──────────────┐  ┌──────────────┐
│  Agent Layer  │    │   LLM Layer  │  │   RAG Layer  │
│   app/agent   │    │   app/llm    │  │   app/rag    │
└───────────────┘    └──────┬───────┘  └──────▲───────┘
                            │  (F3: llm→rag)  │
                            └─────────────────┘
```

- **数据持久化**（debug trace 数据）：`app/runtime/core/storage/*`（内存 / PostgreSQL / asyncPG）
- **共享运行状态**（限流 / 指标计数）：`app/state/store.py`（内存 / Redis）—— 与 storage **职责不重叠**（见 Step 3 结论）

---

## 2. Layer 职责说明

| 层 | 目录 | 职责 | 允许依赖 |
|----|------|------|---------|
| **MCP** | `app/mcp` | JSON-RPC 协议、tool 注册、transport、tool 定义 | `app.runtime` 公开能力、`app.config`；⚠ 编排时经 tools 调用 `app.llm`/`app.agent`（见 F1/F2） |
| **Runtime** | `app/runtime` | Debug Context 生成、数据采集（collectors）、trace 存取（core/storage）、verifier、hooks | `app.config`、自身内部 |
| **Agent** | `app/agent` | 修复/测试/安全编排、DAG 协调、repair queue | `app.runtime` 公开能力、`app.llm`、`app.rag`、`app.config` |
| **LLM** | `app/llm` | 模型调用、prompt/token/retry/cache/model routing、分析队列 | `app.rag`（知识检索，F3）、`app.config` |
| **RAG** | `app/rag` | embedding、vector store、knowledge base、知识检索 | `app.config`、外部向量库（qdrant） |

---

## 3. Dependency Rules（冻结规范）

### 允许方向（正向依赖，禁止反向）

```
api / main / mcp_server ──▶ mcp ──▶ runtime ──▶ config
                          └──▶ llm / agent / rag（编排）
api ──▶ runtime · llm · agent · rag（HTTP facade 组合）
agent ──▶ runtime（公开能力）· llm · rag
llm ──▶ rag（知识检索）
rag ──▶ 外部向量库（qdrant）
```

### 核心不变量

1. **Runtime 禁止依赖** `app/mcp`、`app/agent`、`app/rag`、`app/llm` —— Runtime 是自包含的调试能力核心。
2. **MCP 只承载协议**（protocol/tools/transports），不承载业务实现。
3. **Agent 禁止直接访问** storage 实现、PostgreSQL、trace 表、collector 内部 —— 必须经由 runtime 公开接口。
4. **LLM 禁止**自己管理 debug 数据、自己访问 storage。
5. **RAG 禁止**修改 runtime trace、调用业务数据库、执行 repair。
6. 各层只能通过**公开符号**交互，禁止跨层触碰私有实现。

---

## 4. Forbidden Dependencies（全仓扫描结果）

| 检查项 | 结果 |
|--------|------|
| `app.mcp.core/collectors/builders/verifier/hooks`（旧 shim 路径） | **0** ✅（目录已删除） |
| `runtime → mcp` | **0** ✅ |
| `runtime → agent/rag/llm` | **0** ✅ |
| `agent → storage / postgres / pg_store / collector 内部` | **0** ✅ |
| `llm → storage / postgres / debug 数据管理` | **0** ✅（仅 → rag，见 F3） |
| `rag → runtime 内部 / repair / 业务 db` | **0** ✅ |

**结论**：Runtime 层已完全自包含；Agent/LLM/RAG 均未触碰 storage 与数据库实现；旧 shim 路径无任何残留。依赖方向符合"正向依赖、禁止反向"。

---

## 5. 当前发现问题（V1 已裁决，冻结）

### F1（✅ 已接受）— tools 直接调用 LLM
- 位置：[debug_api.py](../../app/mcp/tools/debug_api.py#L8) `from app.llm.analyzer import analyze`
- **裁决**：接受。`mcp/tools` 作为 **Application Adapter / Composition Layer**，负责组合 runtime/llm/agent 能力。**不引入 service facade 层**，除非后续明确要求。
- 允许方向：`mcp/tools → runtime · llm · agent`（编排）。

### F2（✅ 已接受）— tools 直接调用 Agent
- 位置：[repair_api.py](../../app/mcp/tools/repair_api.py#L15) `from app.agent.repair_queue import get_repair_queue`
- **裁决**：接受，与 F1 一并作为编排边界。禁止改为 `mcp → services → agent`，除非明确要求。

### F3（✅ 已批准）— LLM 依赖 RAG
- 位置：[analyzer.py](../../app/llm/analyzer.py#L17-L29) `app.rag.knowledge_base` / `app.rag.debug_case` / `app.rag.vector_store`
- **裁决**：批准为显式依赖 `llm → rag`（知识检索增强）。禁止反向 `rag → llm`。

### F4（⏸ 暂不处理）— app/services 为空
- 位置：[services/__init__.py](../../app/services/__init__.py)
- **裁决**：暂不处理。不因 F4 引入 service 层；空占位保持现状，除非后续明确要求清理。

### F5（⏸ 后续单独处理）— schema 重复定义
- 位置：[schemas/__init__.py](../../app/schemas/__init__.py#L8) 与 [schemas/trace.py](../../app/schemas/trace.py#L15) 均定义 `TraceEntry`
- **裁决**：后续单独处理，不在架构冻结范围强制。

### ✅ 已确认边界清晰项（无需处理）
- **`app/runtime/core/storage`（debug trace 持久化）** 与 **`app/state/store.py`（限流/指标共享状态，内存/Redis）**：职责完全不重叠，边界清晰。
- **`app/schemas`（API/传输层 Pydantic 模型）** 与 runtime 内部数据模型：schemas 面向 API 传输，runtime 面向内部处理，接口清晰。
- **`app/api` 作为 HTTP facade** 组合 runtime/llm/agent/mcp：符合应用边界职责。

---

## 6. 后续开发规范（Architecture Frozen）

> 从此开始，新增任何 **Agent / RAG / Verifier Loop / SWE-bench** 能力必须遵守以下边界。

1. **先审边界，再写代码**：新增依赖前检查是否违反 Section 3 的方向规则；违反则先报告，不自行绕过。
2. **Runtime 自包含**：任何调试能力（采集/上下文/trace/verifier/storage）只能落在 `app/runtime`，runtime 不得 import `app/mcp`、`app/agent`、`app/llm`、`app/rag`。
3. **MCP 保持纯协议**：`app/mcp` 只承载 protocol/tools/transports；业务实现一律进 runtime。`mcp/tools` 是 **Application Adapter / Composition Layer**，允许编排 runtime/llm/agent（F1/F2 已接受）；**禁止** runtime/agent/llm 反向依赖 mcp，**禁止**引入 service facade 层（除非明确要求）。
4. **Agent 走公开接口**：agent 访问 trace/context/verifier 只能通过 `app.runtime` 公开符号，禁止直接触碰 `runtime/core/storage` 的 storage 实现或 PostgreSQL。
5. **LLM 不做存储**：LLM 只负责模型调用与 prompt 管理，debug 数据由 runtime 持有，LLM 经参数/接口获取，不自行访问 storage。
6. **RAG 只检索**：RAG 只做知识检索与向量存储，不修改 runtime trace、不执行 repair。
7. **禁止复活旧路径**：`app.mcp.core / app.mcp.collectors / app.mcp.builders / app.mcp.verifier / app.mcp.hooks` 已删除，任何场景禁止重新引入。
8. **发现即报告**：若后续审查发现边界违反，先在本报告追加问题记录，经确认后再修复。

---

*本报告基于 2026-08-07 全仓静态依赖扫描生成。发现问题仅记录，未修改任何代码。*

---

## Post Freeze Evolution

> 冻结之后新增能力的演进记录。不改变既有冻结内容与审查结论，仅追加新条目。

### 2026-08-07：Runtime Context Fault Localization

- **新增能力**：`app/runtime/context/fault_localizer.py`（C1）+ 集成进 `build_debug_context`（C2）。
- **归属**：`app/runtime/context`（Debug Context 生成能力，符合 Section 2 Layer 职责与 Section 6 规则 2 "Runtime 自包含"）。
- **依赖**：仅 `app.runtime.collectors.static_analyzer` + Python 标准库；未引入 `app.mcp` / `app.agent` / `app.llm` / `app.rag`。
- **影响**：
  - Debug Context 增强：输出新增可选字段 `fault_localization`（失败/无 frames 时 `None`）。
  - Runtime 依赖方向不变，未新增任何跨层依赖。
  - Architecture Frozen 规则继续有效。

### 2026-08-15：Source Map 解析（v0.5.1）

- **新增能力**：`app/runtime/collectors/sourcemap_resolver.py`（纯 Python base64-VLQ 解码）+ `sourcemap_store.py`（上传/磁盘双通道）+ `resolved_frames` 注入 `build_debug_context`；新 MCP 工具 `resolve_stack`（tools 18 个，含 `repair_async`/`repair_result`/`resolve_stack`）。
- **归属**：`app/runtime/collectors`（前端 minified 堆栈还原，符合 Section 6 规则 2 "Runtime 自包含"）。
- **依赖**：仅 Python 标准库 + `app.runtime` 既有公开能力（code_locator 白名单兜底）；未引入 `app.mcp` / `app.agent` / `app.llm` / `app.rag`。
- **影响**：Debug Context 新增可选 `resolved_frames`；QualityScorer TRACE 维度还原加成（+0.3）；工具数 17 → 18。冻结依赖方向不变。

### 2026-08-15：品牌统一（v0.5.2）

- **新增能力**：全仓 `ai-debug-mcp` 标识统一为 `lujo-mcp`（MCP server 名 / logger / OTel service name / 配置示例 / SDK description / LICENSE 署名 LujoAI）。无功能变更、无 Breaking Change。
- **归属**：跨层标识统一，未引入新依赖。

### 2026-08-18：RAG 知识库 PostgreSQL 持久化 + P3-9 修复（v0.5.3）

- **新增能力**：
  - `app/rag/knowledge_base.py` 写穿（write-through）持久化：`kb_entries` 表（fingerprint 主键 + analysis JSONB + 三级指纹索引 + verify_count/case_confidence），`upsert`/`record_verification`/`clear`/LRU 驱逐同步落库，启动 `load_from_persistent()` 回灌（learned 经验跨重启保留）。
  - `app/runtime/core/storage/factory.py` 新增 `get_knowledge_store()` 分发（`PGKnowledgeBaseStore` / `NoOpKnowledgeBaseStore`，PG 失败降级 no-op）。
  - 数据库改名 `ai_debug_mcp` → `lujo_mcp`。
  - `app/runtime/core/storage/pg_store.py` P3-9：`_query_with_retry` 返回 `(rows, conn)`，7 处调用方归还最新连接，消除重连后连接泄漏。
- **归属**：RAG 层 `app/rag`（知识检索/持久化，符合 Section 6 规则 6 "RAG 只检索"）；PG 连接管理归 `app/runtime/core/storage`。
- **依赖**：RAG → `app.runtime.core.storage.factory` 经公开符号取 KB 存储后端；未引入跨层反向依赖。Architecture Frozen 规则继续有效。
