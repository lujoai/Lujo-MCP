# npm 分发（开箱即用）

Lujo-MCP 的 stdio MCP Server 通过 **npm 元包 + 平台二进制包** 模式分发，让用户
无需安装 Python 即可开箱即用。参考了 esbuild / @biomejs / @nuphus/nuphus-mcp 的做法。

## 发布结构

```
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

## 发布流程（维护者）

1. **打 Python 依赖的二进制**（每个平台）：
   ```bash
   pip install pyinstaller -r requirements-locked.txt
   pyinstaller --clean -y packaging/lujo-mcp-server.spec
   # 产物：dist/lujo-mcp-server(.exe)
   ```
2. **生成平台包骨架**：
   ```bash
   node npm/scripts/gen-platform-packages.js 0.4.0-beta.1
   ```
3. **把各平台二进制放入对应平台包**：
   `npm/packages/lujo-mcp-<suffix>/bin/lujo-mcp-server(.exe)`
4. **逐个发布**（先平台包，后元包）：
   ```bash
   cd npm/packages/lujo-mcp-win32-x64 && npm publish --access public
   # ... 其余平台包
   cd npm/packages/lujo-mcp && npm publish --access public
   ```

> 版本号需在 `package.json`（元包 + 3 平台包 + gen 脚本入参）之间保持一致，
> `postinstall` 会校验平台包版本与元包一致。

## 备注

- 二进制由 PyInstaller 从 Python 源码打包，仍保留 Python 运行时体积（数十 MB），
  属预期代价；可用 UPX 进一步压缩。
- 所有平台包发布后才能发布元包，否则 `npm install` 找不到对应平台二进制。
## CI 自动构建 + 发布

已提供 [release-npm.yml](../.github/workflows/release-npm.yml)：

- **矩阵构建**：`windows-latest`(win32-x64) / `ubuntu-latest`(linux-x64) / `macos-latest`(osx-arm64)
  各自原生跑 PyInstaller 打出单文件二进制。
- **自动发布**：下载各平台二进制 → 生成平台包骨架 → 放置二进制 → 先发布 3 个平台包
  → 再发布元包。

**发布前需在仓库配置 npm token secret**：

1. npmjs.com → Access Tokens → 生成 **Automation** 类型 token
2. GitHub 仓库 → Settings → Secrets and variables → Actions → New repository secret
3. 名称填 `NPM_TOKEN`，粘贴 token

**触发方式（二选一）**：

```bash
# 方式一：手动触发（填版本号）
gh workflow run release-npm.yml -f version=0.4.0-beta.1

# 方式二：打 tag 自动触发（v 前缀）
git tag v0.4.0-beta.1
git push origin v0.4.0-beta.1
```

> 注意：只有打了 tag 或手动触发才会进入 `publish` 阶段；普通 push 不会发布。
> 平台包需先于元包发布（workflow 已处理该顺序）。
