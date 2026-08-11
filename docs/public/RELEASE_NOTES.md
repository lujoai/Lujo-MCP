# Release Notes / 发布说明

> 最新版本：**v0.4.0（2026-08-09）**。基于 v0.4.0（Debug Context Quality）主干，完成 P1 Debug Experience RAG（D1-D4）、文档冻结（D5）与 **npm 开箱即用分发**（PyInstaller 打包 + npm 元包 + GitHub Actions 构建发布，`@lujoai/lujo-mcp@0.4.0` 已发布 npm）。
> 测试基线：单元 908 passed / 6 skipped / 0 failed（含 CODE_REVIEW_FIX_PROMPT 代码审查修复与回归测试 + stacktrace 工具与存储工厂边界测试 17 项，不含依赖真实 LLM 的 `coordinator` 用例）+ e2e 10 passed。
>
> **架构冻结（Architecture Frozen）**：Runtime / RAG / Agent 依赖方向已冻结。允许 Agent → RAG；禁止 Runtime → RAG/Agent/LLM/MCP、禁止 RAG → Agent/Runtime/LLM/MCP。
>
> 历史 v0.3.0 之后的主干演进（已并入 v0.4.0）：
> - Browser SDK 已继续补齐 V3 网络错误自动标记、V6 UI 静默失败自动检测
> - 调试分析链路已新增指纹知识库基础能力（命中优先 + 自动沉淀）
> - Dashboard 实时 SSE 推送（DASH-SSE-001，2026-07-30）：`DashboardEventBus` 广播总线 + SSE 端点 + 前端 EventSource
> - AI Debug Agent Phase 2 多 Agent DAG（AGENT-002，2026-07-30）：`RepairAgent` + `GitAgent`/`TestAgent`/`SecurityAgent` 并行审查
> - MCP 工具数增至 17（新增 `repair_async` / `repair_result`）
> - ⚠️ **beta-release 全量审查（2026-07-27）**：发现 P0×6 + P1×9 + P2×12 + 文档×5 = 32 项，阻断上线和开源。健康度 8.5/10 → 6.5/10。详见内部审计报告

**Version / 版本**: v0.4.0  
**Release Date / 发布日期**: 2026-08-09  
**Codename / 代号**: Debug Experience RAG + npm 开箱即用分发 — 让 AI Agent 理解真实 Bug 运行现场，一条命令开箱即用

---

## v0.4.0（2026-08-09）

### 中文版本

#### 📋 版本概述

v0.4.0-beta 是 Lujo-MCP 的 **P1 Debug Experience RAG** 里程碑版本。在 v0.4.0（Debug Context Quality）主干之上，通过 Debug Experience 数据链路（D1）、三层检索 Retriever（D2）、Context Assembler 解耦（D3）、全量验证（D4）与文档冻结（D5），让 AI Agent 不仅能读取代码，还能复用历史调试经验理解真实 Bug 运行现场。测试基线提升至 **908 passed / 6 skipped / 0 failed**（含 CODE_REVIEW_FIX_PROMPT 修复与回归测试 + stacktrace 工具与存储工厂边界测试 17 项），无回归，并完成架构依赖方向冻结（Architecture Frozen）。

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
