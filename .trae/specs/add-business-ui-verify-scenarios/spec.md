# 业务级 UI 验证场景增强 Spec

## Why
当前 `verify_ui` 已经具备基础的页面访问、DOM 变化校验、文本断言和 URL 断言能力，但仍然缺少真实业务场景所需的高级验证功能。为了让 UI 验证更贴近实际业务需求，需要增加业务相关的验证场景，如表单提交验证、登录流程验证、数据展示验证等。

## What Changes
- 扩展 `verify_ui` 的验证能力，支持更复杂的业务级交互场景
- 增加表单验证、登录流程验证、数据表格验证等常见业务场景的支持
- 为业务验证场景提供更丰富的断言类型和验证选项
- 为新增的业务验证场景补充相应的测试用例

## Impact
- Affected specs: UI 自动化验证、MCP 工具调用、业务场景验证
- Affected code: `app/mcp/tools/verify_ui_api.py`, `app/mcp/verifier/ui_runner.py`, `tests/integration/test_mcp_verify_ui.py`, `tests/integration/test_ui_verify_live.py`

## ADDED Requirements

### Requirement: 表单验证能力
系统 SHALL 支持在 `verify_ui` 规范中表达表单填写和提交验证场景。

#### Scenario: 表单填写验证成功
- **WHEN** 用户通过 `verify_ui` 提交包含表单填写的 UI 规范
- **THEN** 系统能够依次填写表单字段并提交
- **AND** 返回结果中包含表单提交状态和可能的响应信息

#### Scenario: 表单验证失败
- **WHEN** 表单提交后页面反馈错误信息或未达到预期状态
- **THEN** 系统返回 `matched=false`
- **AND** `diffs` 中明确记录表单验证失败的原因

### Requirement: 登录流程验证
系统 SHALL 支持验证用户登录流程，包括用户名密码输入、登录按钮点击和登录后状态验证。

#### Scenario: 登录流程验证成功
- **WHEN** 用户通过 `verify_ui` 提交包含登录流程的 UI 规范
- **THEN** 系统能够依次执行登录步骤
- **AND** 验证登录后的页面状态或跳转

#### Scenario: 登录失败验证
- **WHEN** 登录凭证错误或登录流程中断
- **THEN** 系统返回结构化的登录失败信息
- **AND** 包含具体的失败步骤和原因

### Requirement: 数据展示验证
系统 SHALL 支持验证页面上的数据展示，如表格、列表、图表等业务数据的呈现。

#### Scenario: 数据表格验证成功
- **WHEN** 页面包含数据表格且数据符合预期
- **THEN** 系统验证表格行数、列数和关键数据点
- **AND** 返回验证成功的确认信息

#### Scenario: 数据展示验证失败
- **WHEN** 页面数据不符合预期或数据加载失败
- **THEN** 系统返回 `matched=false`
- **AND** `diffs` 中包含数据验证失败的具体信息

### Requirement: 业务断言扩展
系统 SHALL 扩展现有的断言能力，支持业务相关的验证，如数值范围、日期格式、状态转换等。

#### Scenario: 数值范围验证
- **WHEN** 页面显示的数值需要在特定范围内
- **THEN** 系统验证数值是否符合范围要求
- **AND** 返回验证结果

#### Scenario: 状态转换验证
- **WHEN** 业务操作导致状态发生变化
- **THEN** 系统验证状态是否按预期转换
- **AND** 记录状态转换过程

## MODIFIED Requirements

### Requirement: verify_ui 返回结果
系统 SHALL 继续返回 `matched`、`diffs`、`silent_failure` 和 `interactions` 字段，并在增强后为每个业务验证步骤附加更详细的验证结果。

## REMOVED Requirements

### Requirement: 仅支持基础 UI 交互
**Reason**: 该能力不足以覆盖真实业务场景的验证需求。
**Migration**: 现有基础交互规范保持兼容；新规范可渐进式增加业务验证相关断言。