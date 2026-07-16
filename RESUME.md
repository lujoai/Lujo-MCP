## 项目经历

### ai-debug-mcp — 基于 MCP 协议的 AI 智能调试服务

*2026.07 ｜ 独立开发 ｜ Python / FastAPI / MCP / Playwright / PostgreSQL / Redis*

**项目定位**：解决"无报错但功能不对"的静默失败检测和"多 Agent 协同调试"两个核心问题的 AI 智能调试服务。

**技术栈**：Python / FastAPI / MCP (JSON-RPC 2.0) / Playwright / pytest / PostgreSQL / Redis / 智谱 GLM-4.5-Air / Trae / Qoder / Codex

**项目亮点**：

- 独立设计五层分层架构（传输→中间件→路由→引擎→存储），15 REST 端点 + 15 MCP 工具
- 断言引擎纯函数实现 **<1ms 静默失败判定**（对比 LLM 方案 500ms+），确定性可解释
- MCP JSON-RPC 2.0 协议双传输（HTTP+stdio），已在 Trae 和 Qoder 中实际集成验证
  - 统一工具注册表（HTTP 15 个工具），stdio 侧 handler 复用业务函数，Agent 间会话隔离互不污染
- Playwright 前端自动遍历（auto_test）+ 浏览器 SDK 上报，可选依赖不影响核心功能
- 存储工厂模式（memory/PG 一键切换）+ 状态工厂（memory/Redis）+ 多 LLM provider
- Docker Compose 一键启动（PostgreSQL + Redis + App），scripts/ + migrations/ 标准化

**技术成果**：

- 测试覆盖全部模块（测试状态以 [README.md](./README.md) 项目状态表为准），规范驱动闭环完整可用（定义 → 验证 → Dashboard 可视化）
- 断言引擎 <1ms 判定静默失败，前端自动遍历 <30s/页
- 生产部署仅需改 3 行配置（`STORAGE_BACKEND` / `API_KEY` / `LLM_PROVIDER`），业务代码零改动
- 实战定位并修复 Starlette 1.3 中间件 body 重放失效导致的 422 生产级 bug

**关键架构决策**：

| 决策 | 选择 | 理由 |
|------|------|------|
| 协议 | MCP JSON-RPC 2.0 | Claude/Trae 原生支持，零适配成本 |
| 断言 | 纯函数 assert_behavior | 确定性 > 灵活性；<1ms > 500ms |
| 存储 | 工厂模式 memory↔PG | 开发用内存秒启，生产切 PG 一行配置 |
| 安全 | fail-closed（默认拒绝） | 线上安全第一，不存侥幸 |

**后续路线图**：Phase 1.x 工程化增强（进行中）→ Phase 2 分布式链路追踪 → Phase 4 RAG 知识库
