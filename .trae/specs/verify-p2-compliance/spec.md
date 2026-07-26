# P2 合规性核验与状态审计 Spec

## Why
P2（Phase 2 / 中期优化）在文档中被标注为“已完成”的事项较多，需要以“代码 + 测试 + 运行证据”为唯一依据做一次合规性核验，避免完成度口径失真，并把未纳入完成口径的事项显式收敛为待办清单。

## What Changes
- 输出一份可追溯的 P2 合规性核验报告：逐项对照 P2 预设需求、交付标准、代码落点与测试/运行证据
- 明确标注 P2 范围内“已完成/需依赖环境/部分完成”的判定依据与边界
- 识别并登记遗漏项、技术债或不符合规范的实现点（若存在），形成可执行的整改任务清单
- 明确 P2 范围外但常被误读为“已完成”的事项，作为“未纳入完成口径”的显式声明

## Impact
- Affected specs: 稳定性验证、Browser SDK 采集链路、MCP Streamable HTTP（SSE/notifications）、存储与观测能力（PG/asyncpg/Redis/OTel）
- Affected code: 不新增代码变更要求（本次为核验与收口）；若发现缺口，则在 tasks.md 中形成后续整改任务
- Affected docs: 以 `docs/internal/DELIVERY_MATRIX.md` 与 `docs/internal/STABILITY_REPORT.md` 为权威口径进行核验；必要时补充同步到 `docs/internal/TODO.md`

## ADDED Requirements
### Requirement: P2 合规性核验报告
系统 SHALL 产出一份 P2 合规性核验报告，满足以下约束：
- 必须逐项覆盖 P2 预设的核心能力与交付标准
- 每一项必须给出可追溯证据：代码落点（文件路径）与测试/运行证据（命令与结果摘要）
- 必须显式列出“未纳入已完成口径”的事项清单，避免文档完成度过度乐观

#### Scenario: 核验通过（无缺口）
- **WHEN** 对照 P2 预设任务清单逐项核验
- **THEN** 每项均能在代码与测试/运行证据中找到对应支撑
- **THEN** 生成“核验通过”的结论，并输出范围外事项的显式声明

#### Scenario: 核验不通过（存在缺口）
- **WHEN** 任一 P2 预设任务无法找到对应代码或证据，或存在与规范不一致的实现
- **THEN** 在 tasks.md 中新增整改任务，并将该项在 checklist.md 标记为未通过
- **THEN** 报告中必须明确缺口风险、影响范围与建议修复路径

## MODIFIED Requirements
### Requirement: P2 状态口径一致性
系统 SHALL 确保 P2 的状态口径与以下权威文档一致，且互不矛盾：
- `docs/internal/DEV_PLAN.md`（阶段性任务规划与状态）
- `docs/internal/DELIVERY_MATRIX.md`（真实完成度唯一权威口径）
- `docs/internal/STABILITY_REPORT.md`（稳定性验证与真实环境证据）

## REMOVED Requirements
（无）

## Appendix: P2 预设要求与现状核验依据（初始快照）
- 预设任务来源（P2 范围）：
  - `docs/internal/DEV_PLAN.md` §二“近期任务（当前 Sprint）”中标注为 P2 的 STAB-001~005、SDK-003、SDK-006、KB-001、DOC-003
  - `docs/internal/DEV_PLAN.md` §三“Phase 2：中期优化”中 P2-1~P2-5（均标注已完成）
- 当前“未纳入已完成口径”的事项（需显式声明）：
  - 更丰富的 MCP server->client notifications 事件类型（当前仅 `notifications/session/ready` 与 POST SSE 结果桥接）
  - Docker 容器化复现实验（STAB-007，依赖环境）
  - 向量数据库版 RAG 知识库与 AI Debug Agent

