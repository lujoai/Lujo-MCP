# 变更记录（CHANGELOG）

> 本文件记录 Lujo-MCP 项目对外文档与代码的变更历史。
> 格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [Unreleased]

> v0.4.0 开发中。M1 Quality Foundation、M2 知识库三级 fallback、M3 无堆栈定位、M4 Agent Verify Loop、M5 全量回归、P1 Debug Experience RAG（v0.4.0-beta）已完成；CODE_REVIEW_FIX_PROMPT 全量代码审查修复（P0×5 + P1×20 + P2）已完成；**npm 开箱即用分发（PyInstaller 打包 + npm 元包 + GitHub Actions 构建发布）已完成（2026-08-09，`@lujoai/lujo-mcp@0.4.0` 已发布 npm）**。

### 新增

#### 代码

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

> 测试基线：单元 891 passed / 6 skipped / 0 failed（含 CODE_REVIEW_FIX_PROMPT 修复与回归测试，不含依赖真实 LLM 的 `coordinator` 用例）+ e2e 10 passed（需启动 uvicorn 服务器）。`test_coordinator.py`、`test_agent_repair_e2e.py` 依赖有效 API Key，无 Key 时 skip，属环境依赖非代码回归。

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
