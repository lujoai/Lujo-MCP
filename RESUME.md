## 项目经历

### ai-debug-mcp — 基于 MCP 协议的 AI 智能调试服务

*2026.07 ｜ 独立开发 ｜ Python / FastAPI / MCP / Playwright / PostgreSQL / Redis*

**项目定位**：解决"无报错但功能不对"的静默失败检测和"多 Agent 协同调试"两个核心问题的 AI 智能调试服务。

**技术栈**：Python / FastAPI / MCP (JSON-RPC 2.0) / Playwright / pytest / PostgreSQL / Redis / 智谱 GLM-4.5-Air / Trae / Qoder / Codex

**项目亮点**：

- 独立设计五层分层架构（传输→中间件→路由→引擎→存储），15 REST 端点 + 15 MCP 工具
- 断言引擎纯函数实现 **<1ms 静默失败判定**（对比 LLM 方案 500ms+），确定性可解释
- MCP JSON-RPC 2.0 协议双传输（HTTP+stdio），已在 Trae 和 Qoder 中实际集成验证
  - 统一工具注册表（HTTP 15 个工具，stdio 15 个）；stdio 传输下每个 Agent 独立子进程，进程级隔离
  - **v0.3.0 收口**：JSON-RPC 错误码规范化（-32700/-32600/-32601/-32602/-32603）、LLM 输出 schema 校验+结构化 fallback、stdio 生命周期资源回收
- Playwright 前端自动遍历（auto_test）+ 浏览器 SDK 上报（环形缓冲 + 静默失败事件链），可选依赖不影响核心功能
- 存储工厂模式（memory/PG 一键切换）+ 状态工厂（memory/Redis）+ 多 LLM provider
- Docker Compose 一键启动（PostgreSQL + Redis + App），scripts/ + migrations/ 标准化
- **安全加固**：fail-closed 鉴权、入库前脱敏（locals/message/frames/network/UI/console）、存储工厂 fail-fast、内部错误串全仓收口（17 类已收口）
- **安全自审 + P0 落地（数据流驱动）**：对自研服务做端到端「数据流通 + 权限控制 + 框架合理性」三维审查，识别并分级 **15 项风险**（含任意文件读取 LFI、Playwright SSRF、鉴权默认关闭、共享 HTTP 会话数据隔离缺失），逐项附 `文件:行` 证据链；并**落地修复 P0 四项**（LFI 路径白名单默认收敛项目根、Playwright SSRF URL 校验、鉴权启动防护移入 lifespan、工具调用 `asyncio.wait_for` 超时），145 项相关单测 + 17 项安全断言通过。结论：核心架构合理、无需重写，定向加固约 2 人周

**技术成果**：

- 测试覆盖全部模块，**340 个测试**全部通过（含进程边界、脱敏、错误码、LLM 输出校验等专项集成测试），规范驱动闭环完整可用（定义 → 验证 → Dashboard 可视化）
- 断言引擎 <1ms 判定静默失败，前端自动遍历 <30s/页
- 生产部署仅需改 3 行配置（`STORAGE_BACKEND` / `API_KEY` / `LLM_PROVIDER`），业务代码零改动
- 实战定位并修复 Starlette 1.3 中间件 body 重放失效导致的 422 生产级 bug
- **多 Agent 协同开发**：用多个 AI 子智能体并行执行代码审计与任务（Solo 模式），我作为总代理做方案审查、冲突域隔离、风险决策，最终集成验证

**关键架构决策**：

| 决策 | 选择 | 理由 |
|------|------|------|
| 协议 | MCP JSON-RPC 2.0 | Claude/Trae 原生支持，零适配成本 |
| 断言 | 纯函数 assert_behavior | 确定性 > 灵活性；<1ms > 500ms |
| 存储 | 工厂模式 memory↔PG | 开发用内存秒启，生产切 PG 一行配置 |
| 安全 | fail-closed（默认拒绝） | 线上安全第一，不存侥幸 |

**后续路线图**：v0.3.0 Release Audit 收口完成 ✅ → Browser SDK V3-V6 → 数据层长期优化（分区/归档） → 可观测性（OpenTelemetry） → 智能化（RAG 知识库）
