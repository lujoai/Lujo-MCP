# Tasks

- [x] Task 1: 建立知识库基础模块
  - [x] SubTask 1.1: 新增 `app/llm/knowledge_base.py`，提供最小可用的知识库存取接口
  - [x] SubTask 1.2: 定义知识库条目结构、容量上限与淘汰策略
  - [x] SubTask 1.3: 为知识库模块补充单元测试，覆盖新增、查询、更新、淘汰

- [x] Task 2: 将知识库接入调试分析链路
  - [x] SubTask 2.1: 在调试分析流程中增加“先查知识库”的命中逻辑
  - [x] SubTask 2.2: 命中时返回知识库结果并标记 `knowledge_base_hit` / `analysis_source`
  - [x] SubTask 2.3: 未命中时保持现有 LLM 分析链路与返回结构兼容

- [x] Task 3: 增加自动沉淀机制
  - [x] SubTask 3.1: 在 LLM 分析成功后自动写入或更新知识库条目
  - [x] SubTask 3.2: 写入失败时仅记录日志，不影响主请求返回
  - [x] SubTask 3.3: 为自动沉淀行为补充单元测试

- [x] Task 4: 完成验证与回归
  - [x] SubTask 4.1: 运行知识库相关单元测试
  - [x] SubTask 4.2: 运行调试分析相关测试，确认知识库命中与未命中场景均正常
  - [x] SubTask 4.3: 检查既有调试接口返回结构未发生破坏性变化

# Task Dependencies

- Task 2 depends on Task 1
- Task 3 depends on Task 1
- Task 4 depends on Task 2
- Task 4 depends on Task 3

# Parallel Work Notes

- Task 1.3 与 Task 2 的代码接入可交错推进，但以 Task 1 的接口稳定为前提
- Task 2 与 Task 3 可在知识库基础模块稳定后并行开发
