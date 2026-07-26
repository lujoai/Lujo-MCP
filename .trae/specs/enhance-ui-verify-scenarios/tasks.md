# Tasks

- [x] Task 1: 明确增强后的 UI 验证规范输入输出
  - [x] SubTask 1.1: 梳理现有 `verify_ui` 输入结构与返回结构，确定兼容边界
  - [x] SubTask 1.2: 设计新增断言能力的字段范围，优先覆盖文本、URL 和失败留证
  - [x] SubTask 1.3: 明确安全边界校验在工具输出中的结构化表达方式

- [x] Task 2: 实现 UI 运行器增强能力
  - [x] SubTask 2.1: 在 `ui_runner` 中扩展业务级断言能力
  - [x] SubTask 2.2: 为失败交互补充结构化留证信息
  - [x] SubTask 2.3: 保持现有 `dom_change` 规范兼容，避免破坏已有用例

- [x] Task 3: 打通 MCP 工具层与回归测试
  - [x] SubTask 3.1: 更新 `verify_ui` 工具层的输入说明与返回契约
  - [x] SubTask 3.2: 补充 MCP 通道集成测试，覆盖安全拒绝和断言失败场景
  - [x] SubTask 3.3: 补充真实浏览器集成测试，覆盖文本断言或 URL 断言成功路径

- [x] Task 4: 完成验证与收口
  - [x] SubTask 4.1: 运行受影响单测与集成测试
  - [x] SubTask 4.2: 对照 `checklist.md` 逐项核验
  - [x] SubTask 4.3: 如行为变化影响交付口径，再同步最小必要文档

# Task Dependencies

- Task 2 depends on Task 1
- Task 3 depends on Task 2
- Task 4 depends on Task 3
