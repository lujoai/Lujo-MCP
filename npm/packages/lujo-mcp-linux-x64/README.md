# @lujoai/lujo-mcp-linux-x64

Lujo-MCP 的 **linux-x64 平台二进制包**。

本包由元包 `@lujoai/lujo-mcp` 通过 `optionalDependencies` 按平台自动安装，**不需要单独安装**。
正常入口是元包：

```bash
npm install -g @lujoai/lujo-mcp
```

或在宿主 IDE（Claude Desktop / Cursor / Trae 等）的 MCP 配置里免安装直跑：

```json
{
  "mcpServers": {
    "lujo-mcp": {
      "command": "npx",
      "args": ["-y", "@lujoai/lujo-mcp"]
    }
  }
}
```

## 包内容

- `bin/lujo-mcp-server` —— PyInstaller 打包的 Lujo-MCP 服务端（stdio MCP + 本地 HTTP 采集入口）。
- 本包**不声明 `bin` 字段**：可执行入口只由元包的 `bin/cli.js` 按固定路径
  `bin/lujo-mcp-server` 定位。与元包同名的 bin 会让 npm 在同一安装树跳过全部
  同名链接，导致 `node_modules/.bin` 为空。

其他平台包：`@lujoai/lujo-mcp-win32-x64`、`@lujoai/lujo-mcp-osx-arm64`。

## 文档

用法、配置项与故障排查见仓库：<https://github.com/lujoai/Lujo-MCP>

- 安装与宿主配置：[README](https://github.com/lujoai/Lujo-MCP/blob/main/README.md)
- SDK 接入：[docs/public/SDK_GUIDE.md](https://github.com/lujoai/Lujo-MCP/blob/main/docs/public/SDK_GUIDE.md)
- HTTP/MCP 接口：[docs/public/API_REFERENCE.md](https://github.com/lujoai/Lujo-MCP/blob/main/docs/public/API_REFERENCE.md)

## License

MIT —— 见 [LICENSE](./LICENSE)。
