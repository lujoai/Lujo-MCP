# Tasks

- [x] Task 1: 将 INTERVIEW.md 从 git 跟踪中移除并加入 .gitignore
  - [ ] SubTask 1.1: 执行 `git rm --cached INTERVIEW.md`（保留本地文件）
  - [ ] SubTask 1.2: 在 `.gitignore` 中添加 `INTERVIEW.md` 条目
  - [ ] SubTask 1.3: 验证 `git status` 显示 INTERVIEW.md 不再被跟踪

- [x] Task 2: 对 RAG 模块执行 TRAE-code-review 代码质量审查
  - [ ] SubTask 2.1: 收集审查范围 diff（RAG 迁移 commit e06ad98 或当前文件状态）
  - [ ] SubTask 2.2: 调用 TRAE-code-review skill 执行审查
  - [ ] SubTask 2.3: 汇总审查结果表格（问题编号/标题/建议/代码链接）

- [x] Task 3: 对 RAG 模块执行 TRAE-security-review 安全审查
  - [ ] SubTask 3.1: 确认安全审查范围与代码审查一致
  - [ ] SubTask 3.2: 调用 TRAE-security-review skill 执行审查
  - [ ] SubTask 3.3: 汇总安全审查结果表格（风险等级/证据/建议/代码链接）

- [x] Task 4: 汇总审查报告并确认 INTERVIEW.md 排除状态
  - [ ] SubTask 4.1: 合并代码审查与安全审查结果为统一报告
  - [ ] SubTask 4.2: 验证 INTERVIEW.md 不在 git 暂存区
  - [ ] SubTask 4.3: 向用户报告审查发现和后续建议

# Task Dependencies
- [Task 2] 和 [Task 3] 可并行执行
- [Task 1] 与 [Task 2]/[Task 3] 无依赖，可并行
- [Task 4] 依赖 [Task 1] + [Task 2] + [Task 3] 全部完成
