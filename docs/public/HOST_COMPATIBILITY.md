# MCP 宿主兼容性指南（端口与传参通道）

Lujo 通过两个通道接收启动参数：MCP 配置的 `args`（CLI 参数，如 `--http-port`）与
`env`（环境变量，如 `HTTP_PORT` / `HTTP_HOST`）。**优先级：CLI 显式参数 > 环境变量 >
内置默认值**。默认监听 `127.0.0.1:8710`。

## Trae（CN 版）实测行为

以下为真实环境实测结论（Trae CN 桌面版；第 1–5 条为 v0.9.6 时期实测，行为与 v0.9.7 一致）：

1. **`npx` 命令被自解析为缓存 exe 直启**：配置写 `npx`，Trae 实际启动的是它自己
   解析出的 npx 缓存可执行文件，不经过用户配置的原始命令行。
2. **`args` 附加参数被丢弃**：`args` 里的 `--http-port 8101` 等附加参数不会到达
   Lujo 进程（Lujo 以默认参数启动）。
3. **`.cmd` command 被无视**：把 `command` 指向 `.cmd` 脚本包装同样不生效。
4. **注入自己的 `--http`**：Trae 会自行附加它需要的参数启动进程——统一模式仍能
   工作（HTTP 默认端口可用的前提下），但你配置的参数不在其中。
5. **`mcp.json` 是内部存储的镜像**：Trae 把 UI 里的配置存进内部数据库后镜像出
   `mcp.json`；直接改该文件，合法改动会被导入，非法改动会被静默回退——它不是
   配置真源。
6. **宿主把 MCP server 的 stderr 记入自身日志文件**（本轮实测）：Lujo 写往 stderr
   的警告与日志会被 Trae 收进它自己的日志文件，实测路径为
   `%APPDATA%\Trae CN\logs\<run>\window1\exthost\mcp-servers-host.log`，可作为
   排查入口；但 Trae 的用户界面**不直接展示** stderr，需要主动打开上述日志文件查看。

## 推荐做法

- **一律用 Trae 的 UI 配置 MCP**，不要直接编辑镜像出来的 `mcp.json`。
- **传参用 `env`，不用 `args`**：环境变量经宿主进程继承可靠到达 Lujo。换端口：
  `"env": { "HTTP_PORT": "8101" }`；换监听地址：`HTTP_HOST`。
- **多项目同机调试用 `HTTP_PORT` 区分**（「端口即隔离」），各项目页面 SDK 的
  `endpoint` 指向各自端口。
- 环境变量配置的端口被占用时，Lujo 沿用默认端口冲突语义：stdio MCP 继续可用、
  HTTP 采集降级关闭并在 stderr 提示（不会让整个 MCP 挂掉）。

## 跨项目调试：授权目标项目根（GIT_PATH_WHITELIST）

git 归因工具（get_recent_diff / get_blame_for_frame）只查询「授权项目根」内的
文件；被调试项目通常不是 Lujo 自己的仓库，需在启动配置的 `env` 里显式授权
（逗号分隔的绝对路径）：

```json
"env": { "GIT_PATH_WHITELIST": "C:\\path\\proj1,C:\\path\\proj2" }
```

- 未配置时默认收敛到 Lujo 进程工作目录，根外路径一律拒绝（安全默认）。
- 路径判定前会规范化（解析 `..` 与符号链接），Windows 下大小写不敏感。
- 只授权你信任的项目根：授权根内的 diff/blame 内容会进入宿主模型上下文。
- 配置是否生效可用 `doctor` 工具的 `git_roots` 自检项回显核对。

## 其他宿主（不受影响）

Claude Desktop、Cursor 等宿主按 MCP 标准透传 `args`，`"args": ["--http-port",
"8101"]` 与文档示例照常工作；这类宿主同样支持 `env`，两种通道可任选。

> 排查入口：HTTP 采集未启动时先看 Lujo 的 stderr 警告（Trae CN 下宿主会把它记入
> 上文第 6 条的日志文件，用户 UI 不直接展示），再用
> `diagnose_issue` / doctor 工具回显的监听地址端口核对是否与 SDK endpoint
> 一致。详见 [TROUBLESHOOTING.md](./TROUBLESHOOTING.md)。
