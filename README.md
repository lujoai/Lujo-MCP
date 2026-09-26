# Lujo-MCP

**Lujo-MCP is an MCP Runtime Debugging Context Server for AI coding agents.**

让 Claude、Cursor、Trae 等 AI coding agents 获得**真实运行的 Debug Context** —— 不是只读你的静态代码，而是看到真实 Bug 运行现场。

> 💡 **定位与核心原则**：
> - **定位**：Lujo-MCP 是 AI coding assistant 的「眼睛」与 **Debug Context Infrastructure（调试上下文基础设施）** —— **不是另一个复杂 Agent**，不替代宿主 AI 的推理，而是把控制台异常、网络失败、交互轨迹与调用堆栈组装为结构化现场，喂给宿主 AI 完成精准修复。
> - **核心原则**：**服务端可零配置免环境启动（Trae 一次配好），业务运行现场仍需项目接入 SDK（页面引入脚本并初始化）**。Lujo 无法凭空透视未接入 SDK 的任意项目。

> **当前版本：v0.9.4（已发布稳定版）/ v0.9.5（代码库候选/未发布）**：最新已发布稳定版本为 `v0.9.4`（npm registry 见 [@lujoai/lujo-mcp](https://www.npmjs.com/package/@lujoai/lujo-mcp)，发布证据见 [GitHub Release v0.9.4](https://github.com/lujoai/Lujo-MCP/releases/tag/v0.9.4)）；虚拟帧扫描守卫核心修复已合入 main 分支（commit `390849f` / `2dc9c1c`），相关候选内容已进入 main（commit `e39aeb7`）；v0.9.5 仍处于候选阶段、尚未正式发布。

---

## ⚡ 核心认知：两步搞定，分工清晰

初次使用 Lujo-MCP 时，请牢记核心原则（避免误解）：**服务端可零配置免环境启动（Trae 一次配好），业务运行现场仍需项目接入 SDK（页面引入脚本并初始化）**。

1. **Lujo 服务端（真正零配置）**：通过 Trae / Cursor 配置 `npx` 即可直跑，**无需安装 Python、无需 Docker、无需配置数据库或大模型 Key**，本地自带 SQLite 笔记本与开箱即用的轻量 HTTP 采集服务。
2. **业务项目（一次性轻量接入）**：Lujo 不是系统底层驱动或注入插件，它**无法凭空透视任意本地页面的内部异常**。被调试的项目页面需要**引入 Browser SDK 脚本并在代码中调用 `AiDebug.init({ endpoint })`**（见下方第 1 步）。如果页面与服务不同源（如页面在 `localhost:3000`、Lujo 在 `8000`），需在服务端配置 `CORS_ORIGINS`；若服务端启用了 `API_KEY`，SDK 初始化时也需同步传入 `apiKey`。
3. **日常调试交互（自然语言对话，无需记忆特殊指令）**：配置好后，你在 Trae 里**正常与 AI 对话即可**（例如说：“刚才页面报错了，帮我看下控制台和网络现场”）。Trae 的智能体会根据你的问题**自主决策发起 Tool Calling** 调用 Lujo 工具，你**不需要**手动输入 `@lujo`。
   > 📌 **注**：是否调用工具由宿主大模型自主判断，不保证 100% 每次都选用。若 AI 未调用工具而仅凭静态代码猜测，你只需在对话中简单补充一句：“*请调用 Lujo 工具（diagnose_issue）检查真实的控制台和网络报错记录*”即可。

---

## ⚡ 服务端快速配置（Quick Start）

无需安装 Python 或 Docker 环境，通过 npm / npx 即可开箱即用在 Trae / Cursor 中配置好 Lujo 服务端（注意：此步完成服务端就绪；业务页面仍需接入 SDK 才能采集现场，详见后文「第 1 步」）：

### 推荐方式：npx 免安装直跑

在 MCP 客户端配置文件中填入当前已发布的稳定版本（推荐固定版本保证可复现）：

```json
{
  "mcpServers": {
    "lujo": {
      "command": "npx",
      "args": ["-y", "@lujoai/lujo-mcp@0.9.4"]
    }
  }
}
```

> **版本说明**：省略版本后缀时 npx 会默认拉取 npm 上的稳定最新版 `latest`（当前发布版为 0.9.4）。代码库正在演进 0.9.5 候选，以 npm registry 实际发布状态为准。
>
> **为什么推荐 npx**：跨平台（Windows / macOS / Linux）自动按需拉取对应平台的预编译二进制，彻底避免桌面 GUI 客户端（如 Claude Desktop）因未加载系统 Shell PATH 而找不到命令的问题。
>
> 📌 npm 入口默认启动**统一本地模式**：同一个进程同时提供 MCP stdio 和 `http://127.0.0.1:8000` HTTP。AI 可以直接使用 MCP 工具，浏览器 SDK 也能把控制台、网络失败和点击链路写入同一份内存上下文；不需要再手动启动第二个服务。
>
> 📄 可直接复制的机器可读版本在仓库根目录：[`mcp_config_example.json`](./mcp_config_example.json) —— 里面 `lujo`（npx 免安装）与 `lujo-from-source`（跑本地源码，已带 `--http`）是两条等价配置，**二选一**即可；用源码那条时把 `cwd` 换成你的仓库绝对路径，Windows 下建议把 `command` 指向项目的 `.venv/Scripts/python.exe`。

### 替代方式：全局安装

```bash
npm install -g @lujoai/lujo-mcp@0.9.4
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

> 需要纯 stdio（例如只做协议冒烟或兼容严格的旧客户端）时，把 `args` 改为 `["--no-http"]`。源码入口 `python -m app.mcp_server` 默认也是纯 stdio，传入 `--http` 才开启同样的统一本地模式。
>
> 页面若运行在 `localhost:3000` 等其他端口，请在 MCP 配置的 `env` 中加入 `"CORS_ORIGINS": "http://localhost:3000"`（多个来源用逗号分隔）；打开内置 `http://127.0.0.1:8000/demo` 则无需配置跨域。

---

## 🧭 主流客户端配置路径

| 客户端 | 界面操作与配置文件位置 |
|---|---|
| **Trae** | **界面操作**（菜单入口与配置路径可能随 Trae 版本更新而变化，请以当前 UI 为准）：<br>点击聊天框上方的 `MCP Servers` 图标（或 `Settings` → `Features` → `MCP`）→ 点击 `Add (添加)`，填入：<br>• **Name**: `lujo`<br>• **Command**: `npx`<br>• **Args**: `-y @lujoai/lujo-mcp@0.9.4`<br>**配置文件编辑**：若支持直接编辑配置文件，常见位置为 `.trae/mcp.json`（工作区）或 `~/.trae/mcp.json`（全局），该路径随版本演进可能不同，未验证的配置路径不保证永久有效，建议以 Trae 当前设置界面或官方最新文档为准 |
| **Cursor** | 项目根目录 `.cursor/mcp.json` 或全局 `~/.cursor/mcp.json` |
| **Claude Desktop** | `Settings` → `Developer` → `Edit Config`（编辑 `claude_desktop_config.json`） |
| **其他 MCP 客户端** | 任何支持 MCP 标准 stdio 协议的工具均可直接接入 |

---

## 🧠 先搞清楚：谁负责推理，谁负责采集

用大白话说清分工，可以避开 90% 的上手误区：

- **宿主智能体（Trae / Cursor / Claude…）负责大模型推理与工具调用**。你平时在 Trae 里提问“刚才报错了帮我修”，Trae 自身携带的大模型会**自主判断是否调用 Lujo 工具**，你完全不需要在对话时手动敲指令或手动传参数。
- **Lujo 只负责一件事：采集、关联、查询真实运行现场**。它把控制台异常、网络失败、UI 事件链、静默失败和调用堆栈组装成结构化现场，喂给宿主 AI 判断。Lujo 不是另一个聊天 Agent，也不替代宿主。
- **正常通过 MCP 使用 Lujo，不需要给 Lujo 配置任何大模型 API Key。** 推理由宿主完成；Lujo 的内置 LLM 分析是可选项（见下方「如何开启 LLM 分析」），与能不能用 MCP 工具无关。
- 仓库中的 `BENCHMARK_LLM_BASE_URL` / `BENCHMARK_LLM_API_KEY` / `BENCHMARK_LLM_MODEL` 环境变量**只服务于独立的真实 LLM Benchmark runner（基准评测实验工具）**，与日常 MCP 调试无关，正常使用完全不需要配置。

### 两条链路：MCP 调用链 ≠ 浏览器采集链

```
① MCP 调用链（宿主 AI 按需查现场）
   宿主智能体（Trae 对话）──自主调用 MCP 工具──▶ Lujo 进程

② 浏览器采集链（业务项目产生现场）
   被调试网页 ──Browser SDK 上报──▶ Lujo HTTP endpoint（/ingest）──▶ Lujo memory runtime
```

两条链路都通，宿主 AI 才能拿到浏览器现场：

- **只有 MCP 连接、没有第②条链路时，Lujo 不会自动知道页面里发生了什么。** MCP 面板显示 Lujo「已连接」，只证明工具可被宿主调用，不证明浏览器现场已被采集。如果在未接入 SDK 的项目里直接向 Trae 提问，AI 调用 `diagnose_issue` 会得到“未捕获到近期异常”，这是正常现象。
- Browser SDK 的 `endpoint` 必须指向**当前项目对应的 Lujo HTTP 实例和端口**；同一台机器多项目并行时，每个项目应使用不同 `--http-port`（详见下文「端口即隔离」）。
- 当前 runtime 默认是 **memory**：运行现场保存在 Lujo 进程内存中，**进程重启后旧现场可能消失**（KB 调试经验的本地 SQLite 笔记本是另一回事，不受影响）。持久化存储不是默认前提，也不需要 `.env` 才能跑。
- 正确的操作顺序：**保持 Lujo 进程运行 → 业务页面引入 Browser SDK 并执行 init → 在页面复现问题 → 直接在 Trae 对话框提问让 AI 分析**。

### 最短可执行流程（以 Trae 为例）

1. **配好 MCP**：在 Trae 里添加 Lujo MCP（填入上述 npx 配置）。
2. **接入业务项目**：在前端项目（HTML / React / Vue / Vite）里引入 Browser SDK 脚本并调用 `AiDebug.init({ endpoint: "http://127.0.0.1:8000" })`（页面跨端口需配 CORS，见下文）。
3. **复现问题**：在浏览器里点击或触发该 Bug。
4. **自然对话**：直接在 Trae 聊天框输入：“*刚才页面出现报错了，帮我看下控制台和网络现场并修复*”。
5. **宿主自主排查**：宿主模型可按需调用 `diagnose_issue` 获取结构化现场；是否选用工具由模型自主决定，不保证每次都调用。在有真实报错现场时，可辅助分析排障；若模型未选用工具，可明确提示“请调用 diagnose_issue 检查运行时现场”。

> 全程不需要记忆任何特殊指令，不需要给 Lujo 额外配置任何 LLM API Key。

---

## 🚀 5 分钟跑通第一个真实调试（浏览器 Bug 场景）

> 浏览器运行现场的采集链路是：**页面 SDK → Lujo-MCP HTTP 服务（/ingest）→ AI 通过 MCP 读取**。因此本流程需要先启动 Lujo-MCP HTTP 服务，并让 MCP 客户端以 HTTP 模式接入同一个服务进程。
>
> **推荐用本地源码 + 纯内存模式跑通**：不需要 Docker、Redis、密码或 API Key。Docker 编排面向持久化部署（需要 API Key），放在[进阶流程](#进阶docker-持久化部署需要完整凭据配置)。

### 第 0 步：启动 Lujo-MCP HTTP 服务（本地源码，零外部依赖）

如果已经按上面的 npm 方式接入，这一步已经由 `lujo-mcp-server` 自动完成，可直接访问 `http://127.0.0.1:8000/demo`。下面的源码方式适合开发 Lujo-MCP 本身，或需要自定义 Python 依赖的场景。

```bash
git clone https://github.com/lujoai/Lujo-MCP.git
cd Lujo-MCP
pip install -r requirements.txt
```

在项目根目录创建 `.env`（两个必填项都和启动安全校验/浏览器跨域有关，缺一不可）：

```ini
# 只监听本机回环地址（这也是源码默认值）。要对外服务才需要改 HOST，且此时
# 必须设置 API Key —— 通配地址（0.0.0.0 / ::）+ 无 API Key 会被启动校验直接拒绝；
# 本地调试也不应把无鉴权服务暴露到局域网。
HOST=127.0.0.1

# 你的开发页面源（协议+域名+端口）。页面端口与服务端口不同源时，
# 浏览器会先发 CORS 预检；不配置白名单，预检会被 405 拒绝、SDK 上报全部失败。
# 按需追加，逗号分隔，例如：CORS_ORIGINS=http://localhost:3000,http://localhost:5173
CORS_ORIGINS=http://localhost:3000
```

启动（纯内存存储，重启后数据清空，适合首次接入验证）：

```bash
python -m app.main
# 或：uvicorn app.main:app --host 127.0.0.1 --port 8000
```

也可以让源码入口同时提供 stdio + HTTP（推荐给本地 MCP 客户端）：

```bash
python -m app.mcp_server --http
```

MCP 客户端以 HTTP 模式接入（与 SDK 上报同一个服务进程）：

```json
{
  "mcpServers": {
    "lujo": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

> 未设置 `API_KEY` 时服务以免鉴权模式运行（仅限本机回环监听），SDK 与 MCP 客户端无需再传令牌。

### 进阶：Docker 持久化部署（需要完整凭据配置）

`docker compose up -d` 走 Redis 缓存栈（运行现场默认 memory，KB 经验由本地 SQLite 笔记本持久化；PostgreSQL 后端已移除），Compose 强制要求以下变量，缺一个容器就起不来。在项目根目录创建 `.env`：

```ini
API_KEY=change-me-api-key                 # 必填；SDK 与 MCP 客户端都要用它
# 容器内的监听地址由 compose 固定为 0.0.0.0（否则服务只听容器回环、端口发布
# 打不通），对外只发布到宿主机 127.0.0.1。下面这行只对「不经 compose 直接跑
# 源码」生效，写在这里是为了让 .env 与源码默认值保持一致。
HOST=127.0.0.1
CORS_ORIGINS=http://localhost:3000        # 开发页面源，同上
```

三项配置必须相互匹配，缺一会导致「服务在跑但 SDK 上不去 / MCP 连不上」：

1. **SDK**：初始化时若传入 `apiKey`（SDK 会换取短时令牌后上报）：

   ```html
   <script src="/ai-debug.js"></script>
   <script>
     window.AiDebug.init({ endpoint: "http://127.0.0.1:8000", apiKey: "change-me-api-key" });
   </script>
   ```

   > ⚠️ **前端安全警告**：若在浏览器客户端代码中填入 `apiKey`，该密钥会**完全暴露给页面访问者和所有前端代码**！**严禁将高权限或共享的服务端密钥直接写进公开前端页面**。注意：所有 `/ingest/*` 数据接入端点在开启 RBAC 时硬性要求 `admin` 或 `developer` 角色（`viewer` 角色会被 403 拒绝），系统**不存在**可用于浏览器上报的“只读 Key”或“仅上报 Key”。本地回环开发（`HOST=127.0.0.1`）优先使用免 Key 模式运行；若必须在远程或容器网络开启鉴权，应当由服务端应用代理（如 BFF 或反向代理）保管密钥并限制转发上报路由，避免直接把高权限/共享服务端密钥写进公开前端。

2. **MCP 客户端**：HTTP 接入时在请求头携带同一个 Key（客户端配置支持 `headers` 的写法）：

   ```json
   {
     "mcpServers": {
       "lujo": {
         "url": "http://127.0.0.1:8000/mcp",
         "headers": { "Authorization": "Bearer change-me-api-key" }
       }
     }
   }
   ```

3. **CORS**：`CORS_ORIGINS` 必须包含被调试前端页面的完整源（协议+域名+端口，如 `http://localhost:3000`）；服务端口（默认 8000）与页面端口不同源时，浏览器会先发起 OPTIONS 预检请求；服务端未配置对应的 `CORS_ORIGINS` 白名单时，预检失败会导致 SDK 上报全部被阻断。

### 第 1 步：页面接入采集 SDK（两行代码）

下载或复制仓库中的 [`browser-sdk/ai-debug.js`](./browser-sdk/ai-debug.js) 到你的前端项目（如 `public/` 目录），然后在页面 `<head>` 或 `<body>` 中通过 `<script>` 标签引入并调用：

```html
<script src="/ai-debug.js"></script>
<script>
  window.AiDebug.init({ endpoint: "http://127.0.0.1:8000" });
</script>
```

> **参数说明**：
> - `endpoint`：必填，指向上一步启动的 Lujo-MCP HTTP 服务地址（如 `http://127.0.0.1:8000`）。
> - **跨域 CORS**：若页面运行在 `http://localhost:3000`，请确保 Lujo-MCP 服务端配置了 `CORS_ORIGINS=http://localhost:3000`。
> - **API Key 安全**：本地回环开发（`HOST=127.0.0.1`）建议免 key 运行，无需传入 `apiKey`。切勿将服务端高权限密钥明文写在前端代码中。
>
> 💡 最快的同源验证路径：服务自带演示页 `http://127.0.0.1:8000/demo`（与服务同源，不涉及 CORS），打开后即可触发网络错误现场。

### Node 服务接入：使用 Node SDK（v0.9.4 已发布）

服务端 Node.js 使用独立包 `@lujoai/lujo-mcp-node-sdk`，支持 Node 18/20/22 和 CJS/ESM。它只做显式错误与网络上报，不安装浏览器的 DOM、XHR/fetch、console 或 `localStorage` 钩子；浏览器页面继续使用上面的 Browser SDK。

```bash
npm install @lujoai/lujo-mcp-node-sdk
```

```js
const { createClient } = require("@lujoai/lujo-mcp-node-sdk");

const lujo = createClient({
  endpoint: "http://127.0.0.1:8000",
  apiKey: process.env.LUJO_MCP_API_KEY,
  release: "orders-service@1.4.0",
});

async function main() {
  try {
    await handleRequest();
  } catch (error) {
    lujo.reportError(error, { operation: "handleRequest" });
    throw error;
  } finally {
    await lujo.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
```

事件先进入内存批量队列；`flush()` 等待发送和有限重试完成，单批不超过 100 条，429/5xx 会退避重试，永久 4xx 不会无限重试。应用退出或 worker 重启前应 `await lujo.close()`，它会完成最后一次 flush、停止定时器并释放资源。Node SDK 与 Browser SDK 的完整 API、脱敏和边界说明见 [SDK_GUIDE.md](./docs/public/SDK_GUIDE.md)。

### 第 2 步：触发一个运行时异常

比如在前端控制台或代码中执行一段错误逻辑：

```javascript
fetch('/api/user/profile').then(res => {
  if (!res.ok) throw new Error('API 500: Failed to fetch profile');
});
```

### 第 3 步：在 AI 对话框中直接提问

用户可正常用自然语言向 Trae、Cursor 或 Claude 等宿主智能体描述问题，无需手动调用 MCP 工具或记忆指令：

> 💬 *“刚才前端页面报错了，帮我查查是什么原因并给出修复方案。”*

宿主大模型会自主判断并尝试选用统一诊断入口 `diagnose_issue`（无需任何 request_id，直查最近一次真实错误现场，获取控制台报错、网络请求 Payload/Status、源码行号与调用栈辅助定位）。

> 📌 **重要说明**：Trae / 宿主模型自主决定是否选用工具，不保证每次都自动调用。若大模型未调用工具而仅凭静态代码猜测，你可明确提示它：“**请调用 diagnose_issue 检查运行时现场**”。

```text
AI Agent 调用工具现场结构示例（diagnose_issue）：
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
> 📋 **`diagnose_issue` 的准确用法**：
> - `diagnose_issue({})` —— 读取**最近一次错误**（免 ID 直查）。
> - `diagnose_issue({"query": "关键词"})` —— 对近期错误的 **type / message 做关键词过滤**。query 不是自然语言全字段检索，不保证匹配 selector、trace 元数据或所有上下文字段。
> - **query 未命中 ≠ Lujo 没有现场**。推荐回退顺序：`diagnose_issue({})` → `list_recent_traces` → 按返回 ID 调 `context` / `trace` / `stacktrace` / `get_network_trace`。详见 [API_REFERENCE.md](./docs/public/API_REFERENCE.md)。
>
> ⚠️ **数据边界说明**：只有纯 stdio（`--no-http` 或未加 `--http` 的源码入口）不会接收浏览器 SDK 的 HTTP 上报；npm 默认统一本地模式已经包含 `/ingest`。Agent 是否调用工具最终由宿主模型决定，本项目通过清晰的统一入口（`diagnose_issue`）与自包含的工具描述**提高**调用概率，但不承诺 100% 强制调用。

---

## 🎚️ 能力阶梯：零配置 vs 进阶配置

Lujo-MCP 设计遵循**渐进式增强**原则：

```
┌─────────────────────────────────────────────────────────────┐
│ 🟢 零配置（默认开箱即用）                                     │
│   • MCP 调试工具集即刻可用（diagnose_issue 统一诊断入口）    │
│   • 运行时堆栈、源码行号与系统快照收集                       │
│   • 本地运行，无外部服务依赖（经验自动存本机 SQLite 单文件） │
│   • 浏览器现场采集（控制台/网络/UI 链路）：接入 Browser SDK   │
│     + HTTP 服务即启用（见下方 5 分钟流程）                   │
├─────────────────────────────────────────────────────────────┤
│ 🟡 进阶增强（配置 1 个 API Key，可选）                       │
│   • 解锁 Lujo 内置 LLM 辅助分析与历史知识库自动沉淀          │
│   • 支持免费智谱 GLM-4.7-Flash、DeepSeek、OpenAI 等          │
│   • 支持可选的 Redis 缓存与多实例端口隔离                   │
└─────────────────────────────────────────────────────────────┘
```

### 调试经验会丢吗？（本地「笔记本」，无需配置）

不会。分两层：

- **内置经验（开箱即用）**：Lujo 自带 45 条常见异常经验（类型错误、键不存在、连接失败、HTTP 异常等），每次启动自动加载，不需要任何配置或持久化。
- **自有经验（本地笔记本）**：你自己项目里沉淀的调试经验（启用 LLM 分析后产生）默认写入当前用户的数据目录：Windows `%LOCALAPPDATA%\lujo-mcp\lujo-kb.sqlite3`、macOS `~/Library/Application Support/lujo-mcp/lujo-kb.sqlite3`、Linux `$XDG_DATA_HOME/lujo-mcp/lujo-kb.sqlite3`（未设置时为 `~/.local/share/lujo-mcp/lujo-kb.sqlite3`）。它是 SQLite 单文件，零安装、无需外部服务，进程重启后自动回灌。

笔记本行为说明：

- **数据不出本机**：经验数据保存在普通 SQLite 文件中；停止 Lujo 后删除用户数据目录的 `lujo-kb.sqlite3` 即可重置经验库，也可用 `KB_PERSIST_PATH` 显式指定路径。若启动工作目录中存在通过校验的旧版 `lujo-kb.sqlite3`，Lujo 首次初始化默认目录时会创建一致性快照并保留原文件；同目录的 `.lujo-kb-cwd-migration-complete` 仅记录已初始化状态，重置时请保留它，避免旧库再次导入。不会扫描其他目录寻找旧库。
- **想回到纯内存行为**：设置 `KB_PERSIST_ENABLED=false`，经验仅保留在当前进程内（与 v0.7.x 一致）。

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

### Q4: 同一台机器调试多个项目，AI 查到了别的项目的现场？
- **原因**：多个项目的 Lujo 实例争用同一个默认采集口 `127.0.0.1:8000`，浏览器 SDK 上报只会进占住该端口的那个实例。
- **解决方案**：按「端口即隔离」给每个项目分配独立 `--http-port`，并将各页面 SDK 的 `endpoint` 指向各自端口。详见「🛠️ 进阶开发与私有化部署」中的**多项目同机调试**小节。

---

## 🛠️ 进阶开发与私有化部署

<details>
<summary><b>方式一：Docker Compose 部署（含 Redis 缓存栈）</b></summary>

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

# 启动 MCP stdio 服务（默认纯 stdio）
python -m app.mcp_server

# 同一进程同时启动 MCP stdio + HTTP API 与 Web 界面
python -m app.mcp_server --http

# 仅启动 HTTP API 与 Web 界面
python -m app.main
```

</details>

### 多项目同机调试：「端口即隔离」

Lujo-MCP 的定位是**单用户、本地自用**：npm 一条命令装完即用，一人装一套，数据留在本机（运行现场 memory + KB 经验本地 SQLite 笔记本），**没有服务端、不承诺多人共用一台中央数据库的隔离**。在这一前提下，同一台机器上同时调试多个项目时，若都使用默认采集口 `127.0.0.1:8000`，两个项目的浏览器 SDK 上报只会进入「占住 8000 的那个实例」——另一个项目的 AI 查到的是别的项目的现场。**既定方案是「端口即隔离」**：每个项目用独立端口，互不串台。

**1. 每个项目分配独立的 `--http-port`**（在各自宿主的 MCP 配置中，其余参数由 npm 启动器原样转发给服务）：

```json
{
  "mcpServers": {
    "lujo-project-a": {
      "command": "npx",
      "args": ["-y", "@lujoai/lujo-mcp@0.9.4", "--http-port", "8101"]
    },
    "lujo-project-b": {
      "command": "npx",
      "args": ["-y", "@lujoai/lujo-mcp@0.9.4", "--http-port", "8102"]
    }
  }
}
```

**2. 各项目的页面 SDK `endpoint` 指向各自端口**：

```html
<!-- 项目 A 的页面 -->
<script>
  AiDebug.init({ endpoint: "http://127.0.0.1:8101" });
</script>
<!-- 项目 B 的页面 -->
<script>
  AiDebug.init({ endpoint: "http://127.0.0.1:8102" });
</script>
```

**3. 只做协议冒烟、不需要浏览器现场时用 `--no-http`**：`args: ["-y", "@lujoai/lujo-mcp@0.9.4", "--no-http"]`。此时每个宿主窗口各自一个 Lujo 进程，默认 memory 后端下数据天然按进程隔离，无需端口规划。

**已知限制（如实说明）**：

- 默认采集口是 `127.0.0.1:8000`。npm 统一模式下默认端口冲突时，MCP stdio 会继续运行，但 HTTP/Browser SDK 采集不会启动，并会向 stderr 记录警告；需要浏览器采集时，请为该实例配置空闲的 `--http-port`，并让 SDK endpoint 使用同一端口。显式指定的端口若被占用则启动失败。Lujo 不会自动选择随机端口，因为页面 SDK 也必须知道上报目标端口。
- `diagnose_issue` 缺省取本服务跨页面/标签的最近一条错误（同类错误重复出现时返回最新一次现场）；用户明确在说某个页面/会话时，可给工具传 `session_id` 过滤（缺省 = 不过滤）。

---

## 📚 文档导航（公开文档）

| 分类 | 文档 | 描述 |
|---|---|---|
| **接入与实战** | 📖 [DEMO.md](./docs/public/DEMO.md) | 端到端实战演示（以 React 登录 Bug 为例的完整调试链路与零依赖样例） |
| | 💻 [SDK_GUIDE.md](./docs/public/SDK_GUIDE.md) | Browser SDK 与 Node SDK 使用手册（运行时边界、上报、脱敏、重试与体积截断限制） |
| | 🔌 [API_REFERENCE.md](./docs/public/API_REFERENCE.md) | 18 个 MCP 工具详细入参、返回值、双传输错误码规范与 REST 端点参考 |
| **系统架构** | 🏗️ [DESIGN.md](./docs/public/DESIGN.md) | 核心系统架构、调试经验知识库（RAG 进化机制）与架构冻结规范（整合原 KNOWLEDGE_BASE 与 ARCHITECTURE_REVIEW） |
| | 📋 [PRD.md](./docs/public/PRD.md) | 产品功能需求规格与设计边界承诺 |
| **部署与排障** | 🚦 [PREFLIGHT_CHECKLIST.md](./docs/public/PREFLIGHT_CHECKLIST.md) | 环境依赖、功能启用（Redis/Playwright/OTel）与 Docker 部署前预检综合手册（整合原 ENABLEMENT_GUIDE） |
| | 🛠️ [TROUBLESHOOTING.md](./docs/public/TROUBLESHOOTING.md) | 启动异常、配置错误、网络与 MCP 协议异常排查指南 |
| **发版与演进** | 📜 [CHANGELOG.md](./docs/public/CHANGELOG.md) | 完整版本变更历史、各版本发行说明（Release Notes）与未发布维护批次修复记录 |

---

## 📄 License

MIT License © 2026 [LujoAI](https://github.com/lujoai)
