# RAG 知识库基础能力 Spec

## Why
当前项目已经具备错误采集、LLM 分析、指纹聚合与调试工具链，但每次分析仍主要依赖实时 LLM 调用，无法复用历史问题结论。为降低重复分析成本、提升响应速度并为后续 AI Debug Agent 打基础，需要先补齐一个最小可用的 RAG 知识库能力。

## What Changes
- 新增错误指纹驱动的知识库模块，用于存储历史分析结论与修复建议
- 在调试分析链路中接入知识库优先命中逻辑，命中时可直接返回结果
- 在 LLM 分析成功后自动沉淀知识库条目，形成可持续积累机制
- 新增知识库相关的单元测试与最小集成验证
- **BREAKING** 无

## Impact
- Affected specs: 调试分析、LLM 分析、错误指纹聚合、缓存与性能优化
- Affected code: `app/llm/analyzer.py`、`app/mcp/tools/debug_api.py`、`app/api/debug.py`、新增 `app/llm/knowledge_base.py`、相关测试文件

## ADDED Requirements
### Requirement: 指纹知识库存储
系统 SHALL 提供一个基于错误指纹的本地知识库存储能力，用于保存历史分析结论、修复建议和元数据。

#### Scenario: 新增知识库条目
- **WHEN** 系统接收到一个带有错误指纹、分析结论和修复建议的知识库写入请求
- **THEN** 系统应保存该条目
- **AND** 条目至少包含 `fingerprint`、`analysis`、`fix_suggestion`、`source`、`created_at`、`updated_at`

#### Scenario: 按指纹查询知识库
- **WHEN** 系统以错误指纹查询知识库
- **THEN** 系统应返回该指纹最近的有效知识库条目
- **AND** 若不存在匹配条目，则返回未命中结果

#### Scenario: 容量控制
- **WHEN** 知识库条目数量超过预设上限
- **THEN** 系统应按既定淘汰策略清理旧条目
- **AND** 不得导致查询接口异常

### Requirement: 调试链路优先命中知识库
系统 SHALL 在调试分析链路中优先查询知识库，命中时返回知识库结果并跳过实时 LLM 调用。

#### Scenario: 命中知识库
- **WHEN** 调试请求中包含可计算的错误指纹且知识库存在匹配条目
- **THEN** 系统应返回知识库中的分析结果
- **AND** 响应中应明确标记结果来源为知识库命中
- **AND** 不应触发新的 LLM 请求

#### Scenario: 未命中知识库
- **WHEN** 调试请求未命中知识库
- **THEN** 系统应继续走现有 LLM 分析流程
- **AND** 保持当前调试接口的既有行为不变

### Requirement: LLM 结果自动沉淀
系统 SHALL 在 LLM 分析成功后，自动将可复用结论写入知识库。

#### Scenario: LLM 分析成功后沉淀
- **WHEN** LLM 返回结构化分析结果且请求包含有效错误指纹
- **THEN** 系统应自动写入或更新对应知识库条目
- **AND** 不得影响本次调试请求的主链路返回

#### Scenario: 写入失败时降级
- **WHEN** 知识库写入失败
- **THEN** 系统应记录服务端日志
- **AND** 本次调试请求仍应返回 LLM 分析结果

### Requirement: 可观测的命中结果
系统 SHALL 对知识库命中与自动沉淀行为提供最小可观测信息，便于调试与验证。

#### Scenario: 命中标识
- **WHEN** 调试请求命中知识库
- **THEN** 返回结果中应包含可识别字段，如 `knowledge_base_hit=true`
- **AND** 应包含命中来源标记，如 `analysis_source="knowledge_base"`

#### Scenario: 自动沉淀标识
- **WHEN** 系统完成一次成功的知识库自动沉淀
- **THEN** 应在服务端日志中记录沉淀事件
- **AND** 记录中应包含错误指纹或对应 trace 标识

## MODIFIED Requirements
### Requirement: 调试分析结果来源
系统现有调试分析功能 SHALL 支持多来源结果返回，优先级为：知识库命中 > LLM 实时分析 > 现有回退结果。

#### Scenario: 多来源优先级
- **WHEN** 同一请求同时满足知识库命中与 LLM 可用
- **THEN** 系统应优先返回知识库结果
- **AND** 不应重复调用 LLM

#### Scenario: 保持向后兼容
- **WHEN** 知识库未启用或未命中
- **THEN** 系统应保持现有调试分析接口结构与主要字段兼容

## REMOVED Requirements
### Requirement: 无
**Reason**: 本次为增量能力建设，不移除既有需求。
**Migration**: 无
