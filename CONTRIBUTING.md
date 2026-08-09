# 贡献指南 / Contributing Guide

感谢您对 Lujo-MCP 的关注！欢迎通过 Issue、PR 或讨论参与项目贡献。

## 行为准则

本项目采用了 [Contributor Covenant v2.1](CODE_OF_CONDUCT.md) 作为行为准则。参与即表示您同意遵守该准则。

## 如何贡献

### 报告 Bug

1. 先搜索已有 Issue，确认是否已存在相同问题
2. 使用 Bug Report 模板创建 Issue
3. 提供复现步骤、环境信息、日志/截图

### 提交功能请求

1. 先在 Discussion 中讨论，确认方向
2. 使用 Feature Request 模板创建 Issue
3. 描述使用场景和预期行为

### 提交代码

1. **Fork** 本仓库并创建您的特性分支：`git checkout -b feat/my-feature`
2. 遵循代码规范（见下文）
3. 运行测试确保全部通过：`python -m pytest tests/unit/ -q --tb=short`
4. 运行 lint 检查：`ruff check app/ tests/`
5. 提交 PR 并关联相关 Issue

## 代码规范

### Python

- 遵循 [PEP 8](https://peps.python.org/pep-0008/)
- 类型注解：所有公共函数和类方法必须标注类型
- 文档字符串：公共模块/类/函数使用 Google 风格 docstring
- 导入顺序：标准库 → 第三方 → 项目内部，每组空行分隔
- 命名规范：
  - 类名：`PascalCase`
  - 函数/方法：`snake_case`
  - 常量：`UPPER_SNAKE_CASE`
  - 私有成员：`_leading_underscore`

### JavaScript (Browser SDK)

- 遵循 [Standard JS](https://standardjs.com/) 风格
- 使用 `'use strict'` 严格模式
- 所有公共函数使用 JSDoc 注释

### 测试

- 单元测试必须覆盖新增代码的公共路径
- 集成测试放置在 `tests/integration/`，使用 `@pytest.mark.integration` 标记
- 依赖外部服务的测试使用对应的 marker（`@pytest.mark.pg`、`@pytest.mark.llm`）

### Commit 规范

使用 Conventional Commits 格式：

```
<type>(<scope>): <description>

[optional body]
```

- `type`: feat / fix / refactor / test / docs / chore / style / perf
- `scope`: agent / mcp / api / dashboard / sdk / auth / config / docs / deps / ci
- `description`: 使用中文或英文，首字母小写，末尾不加句号

示例：
```
feat(agent): 新增 GitAgent 归因分析
fix(auth): 修复 RBAC 空角色时跳过的安全漏洞
docs(readme): 更新测试基线为 672 passed
```

## 开发环境

```bash
# 克隆仓库
git clone https://github.com/lujoai/Lujo-MCP.git
cd Lujo-MCP

# 安装依赖
pip install -r requirements.txt
pip install -r requirements-dev.txt

# 复制环境变量模板
cp .env.example .env
# 编辑 .env 填入必要配置

# 运行测试
python -m pytest tests/unit/ -q --tb=short
```

### 环境预检查

首次开发前，运行预检查脚本确认环境就绪：

```bash
python scripts/preflight_check.py
```

## Pull Request 流程

1. 确保 PR 标题遵循 Conventional Commits 格式
2. 在 PR 描述中说明变更目的和测试结果
3. 关联相关 Issue（如 `Closes #123`）
4. CI 检查全部通过后方可合并
5. 至少一位维护者 Review 通过后方可合并

## 分支策略

- `main`: 稳定发布分支，仅通过 PR 合入
- `feat/*`: 特性开发分支
- `fix/*`: Bug 修复分支
- `docs/*`: 文档更新分支

## 安全问题

请勿在公开 Issue 中报告安全漏洞。请通过 SECURITY.md 中描述的渠道报告。