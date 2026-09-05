# Lujo-MCP

**Lujo-MCP is an MCP Runtime Debugging Context Server for AI coding agents.**

让 Claude、Cursor、Trae 等 AI coding agents 获得**真实运行的 Debug Context** —— 不是只读你的静态代码，而是看到真实 Bug 运行现场。

> 💡 **定位**：Lujo-MCP 是 AI coding assistant 的「眼睛」与 **Debug Context Infrastructure（调试上下文基础设施）** —— **不是另一个复杂 Agent**，不替代宿主 AI 的推理，而是把控制台异常、网络失败、交互轨迹与调用堆栈组装为结构化现场，喂给宿主 AI 完成精准修复。

---

## ⚡ 30 秒极速接入（Quick Start）

无需安装 Python 或 Docker 环境，通过 npm / npx 即可开箱即用：

### 推荐方式：npx 免安装直跑

在 MCP 客户端配置文件中填入：

```json
{
  "mcpServers": {
    "lujo": {
      "command": "npx",
      "args": ["-y", "@lujoai/lujo-mcp"]
    }
  }
}
```

> **为什么推荐 npx**：跨平台（Windows / macOS / Linux）自动按需拉取对应平台的预编译二进制，彻底避免桌面 GUI 客户端（如 Claude Desktop）因未加载系统 Shell PATH 而找不到命令的问题。
>
> 📌 stdio 模式让你**即刻获得 MCP 调试工具集**（统一诊断入口 `diagnose_issue`、堆栈解析、上下文构建、规范断言等，以 `tools/list` 实际返回为准）。若要让 AI 看到**浏览器运行现场**（控制台、网络失败、点击链路），还需按下方「5 分钟跑通第一个真实调试」启动 HTTP 服务并在页面接入 SDK。

### 替代方式：全局安装

```bash
npm install -g @lujoai/lujo-mcp
```

客户端配置：

```json
{
  "mcpServers": {
    "lujo": {
      "command": "lujo-mcp-server",
      "args": []
    }
  }
}
```

---

## 🧭 主流客户端配置路径

| 客户端 | 配置文件位置 |
|---|---|
| **Claude Desktop** | `Settings` → `Developer` → `Edit Config`（或编辑 `claude_desktop_config.json`） |
| **Cursor** | 项目根目录 `.cursor/mcp.json` 或全局 `~/.cursor/mcp.json` |
| **Trae** | 设置面板 → `MCP Server` → `添加`（填入上述 JSON） |
| **其他 MCP 客户端** | 任何支持 MCP 标准 stdio 协议的工具均可直接接入 |

---

## 🚀 5 分钟跑通第一个真实调试（浏览器 Bug 场景）

> 浏览器运行现场的采集链路是：**页面 SDK → Lujo-MCP HTTP 服务（/ingest）→ AI 通过 MCP 读取**。因此本流程需要先启动 Lujo-MCP HTTP 服务（一行 Docker 命令即可），并让 MCP 客户端以 HTTP 模式接入。

### 第 0 步：启动 Lujo-MCP HTTP 服务（一次性）

```bash
git clone https://github.com/lujoai/Lujo-MCP.git
cd Lujo-MCP
docker compose up -d          # 拉起 HTTP 服务（含 Dashboard 与 /ingest 端点）
```

MCP 客户端改用 HTTP 模式接入（与 SDK 上报同一个服务进程）：

```json
{
  "mcpServers": {
    "lujo": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

### 第 1 步：页面接入采集 SDK（两行代码）

下载或复制仓库中的 [`browser-sdk/ai-debug.js`](./browser-sdk/ai-debug.js) 到你的前端项目，然后在页面中加入：

```html
<script src="/ai-debug.js"></script>
<script>
  window.AiDebug.init({ endpoint: "http://localhost:8000" });
</script>
```

> SDK 无需构建工具，`<script>` 直接引入即可；`init` 时的 `endpoint` 指向上一步启动的 Lujo-MCP 服务地址。

### 第 2 步：触发一个运行时异常

比如在前端控制台或代码中执行一段错误逻辑：

```javascript
fetch('/api/user/profile').then(res => {
  if (!res.ok) throw new Error('API 500: Failed to fetch profile');
});
```

### 第 3 步：在 AI 对话框中直接提问

在 Cursor、Claude 或 Trae 中直接对 AI 提问：

> 💬 *“刚才前端页面报错了，帮我查查是什么原因并给出修复方案。”*

宿主 AI 会自动调用统一诊断入口 `diagnose_issue`（**无需任何 request_id**，自动定位最近一次真实错误），一次性读取完整的控制台报错、网络请求 Payload/Status、源码行号与调用栈，直接给出修复代码！

```text
AI Agent 自动调用上下文：
┌────────────────────────────────────────────────────────┐
│ diagnose_issue          ← 统一诊断入口，免 ID 直查      │
│ ├─ exception_type: "Error"                             │
│ ├─ message: "API 500: Failed to fetch profile"         │
│ ├─ network_trace: GET /api/user/profile (Status: 500)  │
│ ├─ stacktrace: at profile.js:42:15                     │
│ └─ ui_events: Click on button#load-profile             │
└────────────────────────────────────────────────────────┘
```

> 📖 想看完整还原的实战案例（React 登录静默失败），见 [DEMO.md](./docs/public/DEMO.md)。
>
> ⚠️ **数据边界说明**：stdio MCP 接入不能自动接收浏览器 SDK 的 HTTP 上报——浏览器运行现场（控制台/网络/交互链路）需要「Browser SDK + HTTP 服务 + `/ingest`」链路；后端异常经全局异常钩子自动捕获，stdio 下也可用。Agent 是否调用工具最终由宿主模型决定，本项目通过清晰的统一入口（`diagnose_issue`）与自包含的工具描述**提高**调用概率，但不承诺 100% 强制调用。

---

## 🎚️ 能力阶梯：零配置 vs 进阶配置

Lujo-MCP 设计遵循**渐进式增强**原则：

```
┌─────────────────────────────────────────────────────────────┐
│ 🟢 零配置（默认开箱即用）                                     │
│   • MCP 调试工具集即刻可用（diagnose_issue 统一诊断入口）    │
│   • 运行时堆栈、源码行号与系统快照收集                       │
│   • 纯内存运行，无外部数据库与 API Key 依赖                  │
│   • 浏览器现场采集（控制台/网络/UI 链路）：接入 Browser SDK   │
│     + HTTP 服务即启用（见下方 5 分钟流程）                   │
├─────────────────────────────────────────────────────────────┤
│ 🟡 进阶增强（配置 1 个 API Key，可选）                       │
│   • 解锁 Lujo 内置 LLM 辅助分析与历史知识库自动沉淀          │
│   • 支持免费智谱 GLM-4.7-Flash、DeepSeek、OpenAI 等          │
│   • 支持可选的 PostgreSQL 持久化与 Redis 缓存                │
└─────────────────────────────────────────────────────────────┘
```

### 如何开启 LLM 分析（可选）

如需启用 Lujo-MCP 内置的 LLM 智能分析与经验学习，只需在客户端的 `env` 字段中配置 API Key：

```json
{
  "mcpServers": {
    "lujo": {
      "command": "npx",
      "args": ["-y", "@lujoai/lujo-mcp"],
      "env": {
        "LLM_PROVIDER": "zhipu",
        "OPENAI_API_KEY": "your-zhipu-api-key",
        "LLM_MODEL": "glm-4.7-flash"
      }
    }
  }
}
```

> **提示**：智谱 `glm-4.7-flash` 为免费纯文本模型，免科学上网，填入即可使用。也支持 `LLM_PROVIDER=deepseek` 或 `openai`。

---

## ❓ 常见问题与排错（FAQ）

### Q1: Claude Desktop 报错 `command not found: lujo-mcp-server`？
- **原因**：macOS/Windows 下桌面 GUI 应用启动时不继承用户 Shell 的环境变量 PATH。
- **解决方案**：强烈建议改用 `command: "npx"` + `args: ["-y", "@lujoai/lujo-mcp"]`，由 Node 运行时自动调度，或填写全局 npm bin 的完整绝对路径。

### Q2: 国内安装 npm 包较慢或出现 404？
- **解决方案**：指定官方 npm 注册源安装：
  ```bash
  npm install -g @lujoai/lujo-mcp --registry=https://registry.npmjs.org/
  ```

### Q3: 为什么 AI 提示没有找到错误追踪（Trace）？
- **排查**：
  1. 确认 Lujo-MCP HTTP 服务已启动（SDK 上报依赖 `/ingest` 端点）；
  2. 确认页面已加载 SDK 并调用了 `AiDebug.init({ endpoint: "http://localhost:8000" })`——**未配置 `endpoint` 时 SDK 会静默不上报**；
  3. 打开浏览器 DevTools Network 面板，确认页面有发往 `endpoint` 的 `/ingest/batch` 请求；
  4. 可让 AI 调用 `diagnose_issue`（免 ID 自动定位最近错误）或 `list_recent_traces` 检索最近的运行日志。

---

## 🛠️ 进阶开发与私有化部署

<details>
<summary><b>方式一：Docker Compose 全栈部署（含 PostgreSQL + Redis）</b></summary>

```bash
git clone https://github.com/lujoai/Lujo-MCP.git
cd Lujo-MCP
cp .env.example .env
docker compose up -d
```
服务将运行于 `http://localhost:8000`，支持 Web Dashboard（`http://localhost:8000/dashboard`）与 Streamable HTTP MCP 端点（`http://localhost:8000/mcp`）。

</details>

<details>
<summary><b>方式二：Python 源码本地开发与调试</b></summary>

```bash
# 安装依赖
pip install -r requirements.txt

# 启动 MCP stdio 服务
python -m app.mcp_server

# 或启动 HTTP API 与 Web 界面
python -m app.main
```

</details>

---

## 📚 文档导航

| 文档 | 描述 |
|---|---|
| 📖 [DEMO.md](./docs/public/DEMO.md) | 端到端实战演示（以 React 登录 Bug 为例的完整调试链路） |
| 🔌 [API_REFERENCE.md](./docs/public/API_REFERENCE.md) | MCP 工具详细入参、返回值与 REST 端点参考 |
| 💻 [SDK_GUIDE.md](./docs/public/SDK_GUIDE.md) | Browser SDK 采集手册（XHR/Fetch 拦截、脱敏、UI 静默失败检测） |
| 🧠 [KNOWLEDGE_BASE.md](./docs/public/KNOWLEDGE_BASE.md) | 调试经验知识库：指纹匹配、跨会话沉淀与置信度进化机制 |
| 🏗️ [DESIGN.md](./docs/public/DESIGN.md) | 核心六层系统架构与数据流转设计 |
| 📝 [RELEASE_NOTES.md](./docs/public/RELEASE_NOTES.md) | 版本演进历史与详细更新日志 |

---

## 📄 License

MIT License © 2026 [LujoAI](https://github.com/lujoai)
