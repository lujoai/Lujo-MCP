# RAG 模块代码审查与安全审查 Spec

## Why

RAG 模块刚从 `app/llm/` 迁移到 `app/rag/` 领域（commit `e06ad98`），涉及 3 个文件移动 + 5 个消费方 import 更新 + 6 个测试文件路径修正。迁移虽通过 583 项测试基线，但尚未经过结构化的代码质量审查和安全审查。同时 INTERVIEW.md 作为面试资料文件不应纳入版本控制，但目前仍被 git 跟踪。

## What Changes

- 对 RAG 模块（`app/rag/vector_store.py`、`app/rag/qdrant_vector_store.py`、`app/rag/knowledge_base.py`）及消费方集成点（`app/llm/analyzer.py` RAG 相关函数、`app/agent/context_assembler.py`）执行 TRAE-code-review 代码质量审查
- 对同一范围执行 TRAE-security-review 安全审查
- 将 INTERVIEW.md 从 git 跟踪中移除（`git rm --cached`）并加入 `.gitignore`，确保后续修改不被纳入版本控制

## Impact

- Affected code: `app/rag/*.py`、`app/llm/analyzer.py`（L17-18 import + L542-645 RAG 编排函数）、`app/agent/context_assembler.py`（L79 延迟 import）
- Affected config: `.gitignore`（新增 INTERVIEW.md 条目）
- Git tracking: INTERVIEW.md 从跟踪状态变为忽略状态（本地文件保留不动）
- 不修改任何 RAG 业务代码（审查发现问题后仅记录，不在此 spec 内修复）

## ADDED Requirements

### Requirement: RAG 模块代码质量审查

系统 SHALL 对 RAG 模块代码执行结构化审查，覆盖代码正确性、逻辑缺陷、回归风险和测试覆盖。

#### Scenario: 代码审查执行
- **WHEN** 使用 TRAE-code-review skill 审查 RAG 迁移变更
- **THEN** 产出包含问题编号、严重度、建议、代码链接的审查报告
- **AND** 审查范围限定为 RAG 模块文件及消费方集成点，不审查 .md 文件

### Requirement: RAG 模块安全审查

系统 SHALL 对 RAG 模块代码执行安全扫描，覆盖注入风险、敏感数据泄露、认证授权缺陷。

#### Scenario: 安全审查执行
- **WHEN** 使用 TRAE-security-review skill 审查 RAG 迁移变更
- **THEN** 产出包含风险等级、证据链、修复建议的安全报告
- **AND** 审查范围与代码审查一致

### Requirement: INTERVIEW.md 版本控制排除

系统 SHALL 确保 INTERVIEW.md 文件不被纳入 git 版本控制提交。

#### Scenario: 从 git 跟踪中移除
- **WHEN** 执行 `git rm --cached INTERVIEW.md`
- **THEN** INTERVIEW.md 从 git 索引中移除，本地文件保留
- **AND** `.gitignore` 新增 `INTERVIEW.md` 条目
- **AND** 后续 `git add .` 不会将 INTERVIEW.md 纳入暂存区

## REMOVED Requirements

### Requirement: INTERVIEW.md git 跟踪

**Reason**: INTERVIEW.md 为面试准备资料，不属于项目交付物，不应纳入版本控制
**Migration**: `git rm --cached INTERVIEW.md` + `.gitignore` 添加条目，本地文件不动
