# @lujoai/lujo-mcp

**Lujo-MCP — MCP Runtime Debugging Context Server for AI coding agents.**

给 Claude / Codex / Cursor / Trae 等 MCP 宿主提供**真实运行现场**：浏览器错误、
console 日志、网络失败、UI 交互轨迹与会话上下文。Lujo 只负责采集、关联与查询，
推理与改代码由宿主智能体完成。

> **本包 v0.9.6 更新**：虚拟/非本地堆栈帧过滤，避免不必要的项目根查找与目录遍历；KB SQLite 默认位置迁至用户数据目录，并对有效旧库执行一致性快照迁移；默认 HTTP 端口冲突时保留 MCP stdio 启动。完整发行说明见仓库 [CHANGELOG](https://github.com/lujoai/Lujo-MCP/blob/main/docs/public/CHANGELOG.md)。

- 单机、本地自用、服务端零配置：`npx` 一条命令即可接入，无需数据库、Docker 或配置大模型 Key
- 宿主自然交互：在 Trae / Cursor / Claude 中正常用自然语言描述问题即可，无需手动调用 MCP 工具或记忆特定指令；宿主模型自主决定是否选用工具（不保证每次都调用；未调用时可提示：“请调用 diagnose_issue 检查运行时现场”）
- 业务端轻量接入：前端页面通过 `<script>` 引入 Browser SDK 并执行 `AiDebug.init({ endpoint })` 建立现场采集（跨端口需配 CORS，本地回环推荐免 key 避免前端密钥泄露）
- 运行现场默认留在本机内存；调试经验（KB）写穿到本地单文件 SQLite「笔记本」
- 采集侧提供 Browser SDK（本包已随附，可被 CDN 直接引用）与 Node SDK
  （[`@lujoai/lujo-mcp-node-sdk`](https://www.npmjs.com/package/@lujoai/lujo-mcp-node-sdk)，独立发布；当前版本以 npm registry 为准）

## 安装与接入

在 MCP 客户端配置里加入 npm 的 `latest` 版本（如需固定版本，可将 `latest` 替换为 registry
中当前已发布的具体版本号）。推荐 `npx`：跨平台自动拉取对应的预编译二进制，避免桌面 GUI
客户端因未加载 shell PATH 而找不到命令：

```json
{
  "mcpServers": {
    "lujo": {
      "command": "npx",
      "args": ["-y", "@lujoai/lujo-mcp@latest"]
    }
  }
}
```

> **客户端配置说明**：Trae / Cursor 等客户端的具体添加入口或菜单路径可能随版本变化，请以当前客户端 UI 为准。

默认是**统一模式**：同一个进程既提供 stdio MCP，又在 `127.0.0.1:8000` 开一个
本地 HTTP 采集口供 Browser SDK 上报。只需要纯 stdio 时把 `args` 改成
`["--no-http"]`；需要换采集端口用 `--http-port <port>`（同一台机器调试多个项目时，
**端口即隔离**：每个项目用不同端口，页面 SDK 的 `endpoint` 指向各自端口）。

页面里引入 Browser SDK：

```html
<script src="http://127.0.0.1:8000/ai-debug.js"></script>
<script>
  AiDebug.init({ endpoint: "http://127.0.0.1:8000" });
</script>
```

> ⚠️ **CORS 与安全性**：
> - 若前端运行在不同源端口（如 `http://localhost:3000`），需在服务端配置 `CORS_ORIGINS: "http://localhost:3000"`。
> - 若在浏览器前端代码中配置 `apiKey`，该密钥会直接暴露给所有页面访问者与客户端代码，**严禁将高权限或共享的服务端密钥写进公开前端**。所有 `/ingest/*` 上报端点硬性要求 `admin`/`developer` 角色（`viewer` 会被 403 拒绝），系统不存在只读上报 Key。本地回环开发优先免 Key 运行；若必须在远程/容器网络开启鉴权，应当由服务端应用代理（如 BFF/反向代理）保管密钥并限制转发上报路由。

内置演示页：`http://127.0.0.1:8000/demo`（无需配置跨域）；Web 控制台：
`http://127.0.0.1:8000/dashboard`。

## 安全默认值

- 只监听回环 `127.0.0.1`（要对外服务需显式设 `HOST`，且此时必须配 `API_KEY`：
  通配地址 + 无任何 Key 会被启动校验直接拒绝）
- 未设 `API_KEY` 时以免鉴权模式运行，仅限本机回环
- 敏感内容在**存储边界之前**脱敏；日志 formatter 另有一道遮蔽
- `/metrics` 的免鉴权豁免只在回环绑定时成立

## 平台包

本包通过 `optionalDependencies` 自动安装对应平台的二进制：
`@lujoai/lujo-mcp-win32-x64`、`@lujoai/lujo-mcp-linux-x64`、
`@lujoai/lujo-mcp-osx-arm64`。它们是实现细节，不需要手动安装。

## 更多文档

仓库：<https://github.com/lujoai/Lujo-MCP>

- 快速上手与配置：[README](https://github.com/lujoai/Lujo-MCP#readme)
- 工具清单与错误语义：[API_REFERENCE](https://github.com/lujoai/Lujo-MCP/blob/main/docs/public/API_REFERENCE.md)
- Browser / Node SDK：[SDK_GUIDE](https://github.com/lujoai/Lujo-MCP/blob/main/docs/public/SDK_GUIDE.md)
- 排障：[TROUBLESHOOTING](https://github.com/lujoai/Lujo-MCP/blob/main/docs/public/TROUBLESHOOTING.md)

## License

MIT — 见随包的 [LICENSE](./LICENSE)。
