# @lujoai/lujo-mcp

**Lujo-MCP — MCP Runtime Debugging Context Server for AI coding agents.**

给 Claude / Codex / Cursor / Trae 等 MCP 宿主提供**真实运行现场**：浏览器错误、
console 日志、网络失败、UI 交互轨迹与会话上下文。Lujo 只负责采集、关联与查询，
推理与改代码由宿主智能体完成。

- 单机、本地自用、开箱即用：`npx` 一条命令即可接入，无需数据库或外部服务
- 运行现场默认留在本机内存；调试经验（KB）写穿到本地单文件 SQLite「笔记本」
- 采集侧提供 Browser SDK（本包已随附，可被 CDN 直接引用）与 Node SDK
  （[`@lujoai/lujo-mcp-node-sdk`](https://www.npmjs.com/package/@lujoai/lujo-mcp-node-sdk)）

## 安装与接入

在 MCP 客户端配置里加入（推荐 `npx`：跨平台自动拉取对应的预编译二进制，
避免桌面 GUI 客户端因未加载 shell PATH 而找不到命令）：

```json
{
  "mcpServers": {
    "lujo": {
      "command": "npx",
      "args": ["-y", "@lujoai/lujo-mcp@0.9.3"]
    }
  }
}
```

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
