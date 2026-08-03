# 变更记录（CHANGELOG）

> 本文件记录 Lujo-MCP 项目对外文档与代码的变更历史。
> 格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [Unreleased] - 2026-08-03

> v0.4.0 开发中。M1 Quality Foundation 已完成，M2-M4 待启动。

### 新增

#### 代码

- **Quality System 核心框架**（`app/quality/`）
  - `schemas.py`：`QualityReport` / `ContextCompleteness` / `AnalysisConfidence` / `EvidenceItem` / `DimensionScore` 数据模型
  - `scorer.py`：规则引擎 `QualityScorer.evaluate()`——9 维度加权评分 + 证据提取 + 可信度评分 + 改进建议，纯函数 + 静默降级
  - `__init__.py`：包导出
- **配置项**（`app/config.py`）：`quality_scoring_enabled` / `agent_iterative_repair_enabled` / `agent_max_iterations`
- **Context Assembler 质量注入**（`app/agent/context_assembler.py`）：`assemble()` 返回新增 `quality_report` 字段，feature flag 控制，失败静默降级
- **LLM 分析增强**（`app/llm/analyzer.py`）：SYSTEM_PROMPT 新增 `reasoning_chain` + `evidence_items`；`_validate_and_normalize` 向后兼容旧格式
- **Dashboard 质量报告**（`app/api/dashboard.py` + `app/web/dashboard.html`）
  - `GET /api/dashboard/trace/{tid}/quality` 独立端点
  - `get_trace_detail` 注入 `quality_report` 字段
  - 前端 Quality 卡片：综合评分进度条 + 9 维度网格 + 证据列表 + 改进建议
- **StaticAnalyzer**（`app/mcp/collectors/static_analyzer.py`）：基于 Python `ast` 标准库的函数级静态分析，提取函数签名/参数/类型注解/内部调用/复杂度/可疑输入（M3 Task 12，零外部依赖）
- **测试**（`tests/unit/test_quality.py`）：86 个用例覆盖 19 个测试类；`tests/unit/test_dashboard.py` 新增 6 个质量报告测试用例

#### 文档

- **PRD.md §12.2**：v0.4.0 路线图——Milestone 概览 + M1 评分基线（5 场景对比）+ M2-M4 评分提升预期 + 各 Milestone 贡献分解
- **DESIGN.md §19**：v0.4.0 架构评审决策（§19.1-19.6）
  - §19.1 项目当前状态评估（Beta 偏 Demo 判定）
  - §19.2 Quality System 评分模型设计（9 维度权重 + 模块结构 + 设计约束）
  - §19.3 M1 评分基线（5 场景对比 + 基线分析要点）
  - §19.4 M2-M4 改进逻辑与评分推演（逐场景维度变化 + 综合评分推演汇总）
  - §19.5 架构稳定性约束（6 个禁止大改模块）
  - §19.6 v0.4.0 明确不做（7 项）

### 修复

#### 文档

- **PRD.md**：修订记录 v5.6 中的 README.md 链接 `../README.md` → `../../README.md`（路径修正）
- **DESIGN.md**：3 处 `§6.1` 死链修复为 `§6`（§6 无子章节）

### 变更

#### 文档

- **PRD.md**：修订记录新增 v5.6（v0.4.0 开发路线制定 + M1 Quality Foundation 交付）

> 测试基线：764 passed, 6 skipped, 0 failed

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
