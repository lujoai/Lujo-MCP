# Tasks

- [x] Task 1: 设计业务级 UI 验证规范结构
  - [x] SubTask 1.1: 分析现有 `verify_ui` 规范结构，确定扩展点
  - [x] SubTask 1.2: 设计表单验证规范字段，包括字段类型、验证规则等
  - [x] SubTask 1.3: 设计登录流程验证规范字段，包括用户名密码输入、验证步骤等
  - [x] SubTask 1.4: 设计数据展示验证规范字段，包括表格、列表验证规则等

- [x] Task 2: 实现业务验证功能
  - [x] SubTask 2.1: 在 `ui_runner` 中实现表单填写和提交验证逻辑
  - [x] SubTask 2.2: 在 `ui_runner` 中实现登录流程验证逻辑
  - [x] SubTask 2.3: 在 `ui_runner` 中实现数据展示验证逻辑
  - [x] SubTask 2.4: 实现业务断言扩展，如数值范围、日期格式验证等

- [x] Task 3: 更新工具层和测试
  - [x] SubTask 3.1: 更新 `verify_ui` 工具层的输入说明与返回契约
  - [x] SubTask 3.2: 补充 MCP 通道集成测试，覆盖新增的业务验证场景
  - [x] SubTask 3.3: 补充真实浏览器集成测试，覆盖表单、登录、数据展示验证场景

- [x] Task 4: 完成验证与收口
  - [x] SubTask 4.1: 运行受影响单测与集成测试
  - [x] SubTask 4.2: 对照 `checklist.md` 逐项核验
  - [x] SubTask 4.3: 如行为变化影响交付口径，再同步最小必要文档

# Task Dependencies

- Task 2 depends on Task 1
- Task 3 depends on Task 2
- Task 4 depends on Task 3