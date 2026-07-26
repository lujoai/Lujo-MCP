# UI 验证增强 Spec

## Why
当前 `verify_ui` 已经能完成基础页面访问和简单 DOM 变化校验，但还不足以覆盖真实业务页面常见的交互断言、安全限制验证和失败排查诉求。为了让这条链路更接近可交付能力，需要把“能跑”提升到“能判断、能解释、能留证”。

## What Changes
- 扩展 `verify_ui` 的断言能力，支持更贴近业务场景的页面结果校验
- 补齐 UI 安全边界验证，使 URL allowlist / 私网限制的验证结果可直接体现在工具输出中
- 为失败交互补充结构化留证信息，便于定位页面未响应、选择器失效或跳转异常
- 为增强后的 `verify_ui` 补充 MCP 通道和真实浏览器回归测试

## Impact
- Affected specs: UI 自动化验证、MCP 工具调用、页面安全限制校验
- Affected code: `app/mcp/tools/verify_ui_api.py`, `app/mcp/verifier/ui_runner.py`, `tests/integration/test_mcp_verify_ui.py`, `tests/integration/test_ui_verify_live.py`

## ADDED Requirements
### Requirement: 丰富 UI 交互断言
系统 SHALL 支持在 `verify_ui` 规范中表达常见业务级断言，而不仅限于选择器出现。

#### Scenario: 文本断言成功
- **WHEN** 用户通过 `verify_ui` 提交包含文本断言的 UI 规范
- **THEN** 系统返回 `matched=true`
- **AND** 返回结果中包含本次交互的断言通过信息

#### Scenario: 文本断言失败
- **WHEN** 页面交互完成但目标元素文本与规范不一致
- **THEN** 系统返回 `matched=false`
- **AND** `diffs` 中明确记录期望文本与实际文本

#### Scenario: URL 断言失败
- **WHEN** 交互后页面未跳转到规范要求的 URL 或路由
- **THEN** 系统返回结构化差异信息
- **AND** 标明预期 URL 与实际 URL

### Requirement: 显式表达 UI 安全边界结果
系统 SHALL 将 URL allowlist / 私网限制等 UI 安全校验结果以结构化方式返回给调用方。

#### Scenario: 私网地址被拒绝
- **WHEN** 目标 URL 命中私网、回环或未放行地址
- **THEN** 系统拒绝执行页面验证
- **AND** 返回明确的拒绝原因

#### Scenario: Allowlist 地址被放行
- **WHEN** 目标 URL 虽然命中受限地址，但主机名在 allowlist 中
- **THEN** 系统允许继续执行 UI 验证
- **AND** 后续交互结果按正常流程返回

### Requirement: 为失败交互提供留证信息
系统 SHALL 在 UI 验证失败时返回可用于排查的结构化留证信息。

#### Scenario: 选择器失效导致失败
- **WHEN** 交互步骤因目标元素不存在而失败
- **THEN** 系统返回失败步骤、目标选择器和失败原因
- **AND** 留证信息可被 MCP 调用方直接消费，无需读取服务端日志

#### Scenario: 页面执行异常
- **WHEN** Playwright 执行过程中发生导航、点击或断言异常
- **THEN** 系统返回失败步骤和异常类型
- **AND** 保留足够的上下文信息以支持后续排查

## MODIFIED Requirements
### Requirement: verify_ui 返回结果
系统 SHALL 继续返回 `matched`、`diffs`、`silent_failure` 和 `interactions` 字段，并在增强后为每个交互步骤附加更细粒度的断言结果与失败留证信息。

## REMOVED Requirements
### Requirement: 仅支持 DOM 出现性断言
**Reason**: 该能力不足以覆盖真实业务页面的验证需求。
**Migration**: 现有只使用 `dom_change` 的规范保持兼容；新规范可渐进式增加文本、URL 和失败留证相关断言。
