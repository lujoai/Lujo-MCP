# npm 分发（开箱即用）

Lujo-MCP 的本地 MCP Server 通过 **npm 元包 + 平台二进制包** 模式分发，让用户
无需安装 Python 即可开箱即用。默认一个进程同时提供 MCP stdio 和 localhost HTTP，
浏览器 SDK 的 `/ingest` 数据会与 MCP 工具共享同一份内存状态。

Node.js 服务端 SDK 是独立包 `@lujoai/lujo-mcp-node-sdk`，不包含在本地 MCP Server
元包中；它面向 Node 18/20/22，提供 CJS/ESM 根入口和显式错误/网络上报。v0.7.9
已于 2026-09-10 发布；Node SDK 与 MCP 元包、三平台包均已发布为 `0.7.9`，`latest`
指向 `0.7.9`，且 `engines.node` 均为 `>=18`。发布流水线 run `34383308343` 成功。

## 发布结构

```
node-sdk/                         # @lujoai/lujo-mcp-node-sdk（独立发布）
└── package.json
npm/
└── packages/
    ├── lujo-mcp/                 # 元包（薄层，用户 `npm install -g` 的就是它）
    │   ├── package.json          # bin + optionalDependencies（列出所有平台包）
    │   ├── bin/cli.js            # 定位并 spawn 当前平台的二进制
    │   ├── bin/check.js          # postinstall 校验平台包是否就位
    │   └── scripts/check-clean-bin.js  # prepublishOnly 门禁
    ├── lujo-mcp-win32-x64/       # 平台包 ×3（CI 实际构建发布）
    ├── lujo-mcp-linux-x64/
    └── lujo-mcp-osx-arm64/
└── scripts/
    └── gen-platform-packages.js  # 一键生成 3 个平台包的 package.json
```

仓库根目录的 `node-sdk/` 是独立发布包 `@lujoai/lujo-mcp-node-sdk`，不随
`@lujoai/lujo-mcp` 元包安装。

## 用户使用

```bash
npm install -g @lujoai/lujo-mcp
```

MCP 客户端配置（Claude Desktop / Cursor / Trae）：

```json
{
  "mcpServers": {
    "lujo-mcp": {
      "command": "lujo-mcp-server",
      "args": []
    }
  }
}
```

启动器默认启用统一本地模式（HTTP 绑定 `127.0.0.1:8000`）。需要纯 stdio 时使用
`"args": ["--no-http"]`；也可传 `--http-port` 或 `--http-host` 覆盖 HTTP 监听参数。
浏览器页面若在其他端口运行，请通过 MCP 配置的 `env` 设置 `CORS_ORIGINS`，例如
`"CORS_ORIGINS": "http://localhost:3000"`。

## Node.js 服务端接入

```bash
npm install @lujoai/lujo-mcp-node-sdk
```

```js
import { createClient } from "@lujoai/lujo-mcp-node-sdk";

const lujo = createClient({
  endpoint: "http://127.0.0.1:8000",
  apiKey: process.env.LUJO_MCP_API_KEY,
});

async function main() {
  try {
    await runJob();
  } catch (error) {
    lujo.reportError(error, { operation: "runJob" });
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

Node SDK 只响应显式调用，不自动拦截浏览器或 Node 的网络 API；页面采集仍使用随主包
分发的 Browser SDK。`flush()` 等待当前批量发送和有限重试完成，`close()` 完成最后一次
flush、停止定时器并释放资源。完整 API 和脱敏规则见
[SDK_GUIDE.md](../docs/public/SDK_GUIDE.md)。

## 发布流程（维护者）

1. **打 Python 依赖的二进制**（每个平台）：
   ```bash
   pip install pyinstaller -r requirements-locked.txt
   pyinstaller --clean -y packaging/lujo-mcp-server.spec
   # 产物：dist/lujo-mcp-server(.exe)
   ```
2. **生成平台包骨架**：
   ```bash
   node npm/scripts/gen-platform-packages.js <version>  # 例如 0.7.6
   ```
3. **把各平台二进制放入对应平台包**：
   `npm/packages/lujo-mcp-<suffix>/bin/lujo-mcp-server(.exe)`
4. **逐个发布**（先平台包，后元包）：
   ```bash
   cd npm/packages/lujo-mcp-win32-x64 && npm publish --access public
   # ... 其余平台包
   cd npm/packages/lujo-mcp && npm publish --access public
   ```
5. **发布 Node SDK**（独立包，发布前必须通过测试和 clean-install smoke）：
   ```bash
   npm test --prefix node-sdk
   npm publish ./node-sdk --access public
   ```

> 版本号需在 `package.json`（元包 + 3 平台包 + gen 脚本入参）之间保持一致，
> `postinstall` 会校验平台包版本与元包一致。

## 备注

- 二进制由 PyInstaller 从 Python 源码打包，仍保留 Python 运行时和 Web/MCP 依赖体积（数十 MB 以上），
  属预期代价；可用 UPX 进一步压缩。
- 所有平台包发布后才能发布元包，否则 `npm install` 找不到对应平台二进制。

## CI 自动构建 + 发布

已提供 [release-npm.yml](../.github/workflows/release-npm.yml)：

- **矩阵构建**：`windows-latest`(win32-x64) / `ubuntu-latest`(linux-x64) / `macos-latest`(osx-arm64)
  各自原生跑 PyInstaller 打出单文件二进制。
- **自动发布**：先发布 `@lujoai/lujo-mcp-node-sdk`，再下载各平台二进制 → 生成平台包骨架
  → 放置二进制 → 发布 3 个平台包 → 再发布元包。Node SDK 发布前会从 `npm pack`
  产物安装到空目录，并分别执行 CJS/ESM 根入口导入 smoke；已存在的同版本会幂等跳过。

- **Node SDK CI**：Node 18/20/22 各运行一次完整测试，并在同一 packed fixture 中验证
  CJS、ESM 和 `close()`；缺失 `node-sdk` 或根入口不完整会直接失败。

**发布前需在仓库配置 npm token secret**：

1. npmjs.com → Access Tokens → 生成 **Automation** 类型 token
2. GitHub 仓库 → Settings → Secrets and variables → Actions → New repository secret
3. 名称填 `NPM_TOKEN`，粘贴 token

**触发方式（二选一）**：

```bash
# 方式一：手动触发（填版本号）
gh workflow run release-npm.yml -f version=<version>  # 例如 0.7.6

# 方式二：打 tag 自动触发（v 前缀）
git tag v<version>  # 例如 v0.7.6
git push origin v<version>
```

> 注意：只有打了 tag 或手动触发才会进入 `publish` 阶段；普通 push 不会发布。
> 平台包需先于元包发布（workflow 已处理该顺序）。
