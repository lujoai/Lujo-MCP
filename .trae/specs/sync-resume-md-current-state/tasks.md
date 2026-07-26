# Tasks

- [x] Task 1: 核实 REST 端点数（15 vs 17），确认 L15 修正口径
  - [x] SubTask 1.1: 在 app/api/ 目录统计实际 REST 端点数（含新增的 repair/async + repair/result）
  - [x] SubTask 1.2: 对照 README.md / DELIVERY_MATRIX.md 确认权威口径
  - [x] SubTask 1.3: 若 REST 端点也为 17，则 L15 修正为"17 REST 端点 + 17 MCP 工具"；否则保留"15 REST 端点 + 17 MCP 工具"
  - **结论**：REST 端点为 17（DELIVERY_MATRIX.md L46 确认 AI Debug Agent 新增 2 REST 端点），L15 修正为"17 REST 端点 + 17 MCP 工具"

- [x] Task 2: 修正 RESUME.md L15 + L18 工具数 15→17
  - [x] SubTask 2.1: L15 `15 REST 端点 + 15 MCP 工具` → `17 REST 端点 + 17 MCP 工具`
  - [x] SubTask 2.2: L18 `HTTP 15 个工具，stdio 15 个` → `HTTP 17 个工具，stdio 17 个`

- [x] Task 3: 修正 RESUME.md L29 测试基线 41→583
  - [x] SubTask 3.1: `知识库专项验证 **41 passed**` → `**583 passed / 6 skipped / 0 failed**（含 AI Debug Agent Phase 1 新增 63 项）`
  - [x] SubTask 3.2: 保留"测试覆盖全部模块，项目长期保持单元/集成双层验证"叙述句

- [x] Task 4: 修正 RESUME.md L44 路线图
  - [x] SubTask 4.1: `→ 向量检索版 RAG / AI Debug Agent` → `→ 向量检索版 RAG（in-process + Qdrant 语义召回）✅ → AI Debug Agent Phase 1（单 Agent + 多 Agent 协同框架预留）✅ → Phase 2 多 Agent DAG（待启动）`

- [x] Task 5: 验证保留项未被误改
  - [x] SubTask 5.1: 确认 L25 `145 项相关单测 + 17 项安全断言` 保留原样（P0 落地历史快照）
  - [x] SubTask 5.2: 确认 L19 v0.3.0 收口叙事保留原样
  - [x] SubTask 5.3: 确认简历叙事视角与措辞风格未被破坏
  - [x] SubTask 5.4: 跨文档一致性——修正 INTERVIEW.md L207 "15 个 REST 端点" → "17 个 REST 端点"（上一轮遗漏）
  - [x] SubTask 5.5: rg 扫描 RESUME.md 中 `15 个工具|15 MCP|15 REST|41 passed|向量检索版 RAG / AI Debug Agent$` 0 残留

# Task Dependencies

- Task 2 依赖 Task 1（需先确认 REST 端点数才能定 L15 修正口径）
- Task 3、Task 4 可与 Task 2 并行（不同行，无依赖）
- Task 5 依赖 Task 2/3/4 全部完成（验证保留项未被误改）
