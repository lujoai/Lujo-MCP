#!/usr/bin/env node
// Generates the per-platform npm packages that ship the PyInstaller binary.
//
// Each platform package (lujo-mcp-<platform>-<arch>) contains exactly two files:
//   package.json   — declares the binary path
//   bin/lujo-mcp-server(.exe)  — the actual binary (added at build time)
//
// Usage:
//   node npm/scripts/gen-platform-packages.js 0.7.6 [--out <dir>]
//   # then place each built binary into the matching package's bin/ directory.
//
// --out <dir>（可选）：把产物写到指定目录（<dir>/lujo-mcp-<suffix>/package.json）。
//   默认仍写 npm/packages（CI 行为不变）。本地验证/测试必须用 --out，
//   否则会直接覆写仓库内的平台包 manifest。
//
// engines 字段（v0.7.8 起）：覆写前读取目标目录中该包现有 package.json 的
//   engines 带进产物；读不到（文件或字段缺失）时回退默认 {"node": ">=18"}。
//   背景：此前按内嵌 8 字段 schema 整体覆写，仓库 manifest 声明的 engines
//   在发布产物中被静默丢弃（v0.7.6/v0.7.7 线上三包实证为空，见 CODE_REVIEW §9）。
'use strict';
const fs = require('fs');
const path = require('path');

const argv = process.argv.slice(2);
const version = argv[0];
if (!version) {
  console.error('Usage: node npm/scripts/gen-platform-packages.js <version> [--out <dir>]');
  process.exit(1);
}

// 解析可选 --out 参数（支持 --out <dir> 与 --out=<dir> 两种写法）
let outRootArg = null;
for (let i = 1; i < argv.length; i++) {
  if (argv[i] === '--out') {
    outRootArg = argv[++i];
  } else if (argv[i].startsWith('--out=')) {
    outRootArg = argv[i].slice('--out='.length);
  }
}

// [node platform, node arch, pyinstaller artifact name, package suffix]
// 仅包含 CI 实际构建并发布的平台；新增平台需同步 CI build 矩阵与元包 optionalDependencies。
const platforms = [
  { platform: 'win32', arch: 'x64', exe: 'lujo-mcp-server.exe', suffix: 'win32-x64' },
  { platform: 'linux', arch: 'x64', exe: 'lujo-mcp-server', suffix: 'linux-x64' },
  { platform: 'darwin', arch: 'arm64', exe: 'lujo-mcp-server', suffix: 'osx-arm64' },
];

// 仓库 manifest（npm/packages/lujo-mcp-*/package.json）声明的 node 下限。
// 回退值必须与之保持一致；若仓库 manifest 调整了 engines，这里要同步。
const DEFAULT_ENGINES = { node: '>=18' };

// 覆写前读取目标包目录现有 manifest 的 engines；读不到时回退默认。
function readExistingEngines(pkgDir) {
  try {
    const manifest = JSON.parse(
      fs.readFileSync(path.join(pkgDir, 'package.json'), 'utf8'),
    );
    if (
      manifest &&
      typeof manifest === 'object' &&
      manifest.engines &&
      typeof manifest.engines === 'object' &&
      manifest.engines.node
    ) {
      return { node: String(manifest.engines.node) };
    }
  } catch {
    // 文件缺失 / JSON 损坏 → 回退默认（首次生成或 --out 空目录场景）
  }
  return { ...DEFAULT_ENGINES };
}

const packagesRoot = outRootArg ? path.resolve(outRootArg) : path.resolve(__dirname, '..', 'packages');

for (const p of platforms) {
  const pkgName = `lujo-mcp-${p.suffix}`;
  const scope = '@lujoai';
  const pkgDir = path.join(packagesRoot, pkgName);
  fs.mkdirSync(path.join(pkgDir, 'bin'), { recursive: true });

  // engines 取自覆写前的现有 manifest（默认输出根 = npm/packages，
  // 仓库 manifest 即「现有 manifest」，其 engines 声明由此进入发布产物）
  const engines = readExistingEngines(pkgDir);

  const pkgJson = {
    name: `${scope}/${pkgName}`,
    version,
    engines,
    description: `Lujo-MCP binary for ${p.platform}-${p.arch} (auto-installed via the meta package).`,
    license: 'MIT',
    os: [p.platform === 'osx' ? 'darwin' : p.platform],
    cpu: [p.arch],
    bin: {
      // 统一用正斜杠（跨平台：Windows 上 path.join 会产出反斜杠，导致发布 manifest 非确定性）
      'lujo-mcp-server': ['bin', p.exe].join('/'),
    },
    files: ['bin'],
  };

  fs.writeFileSync(
    path.join(pkgDir, 'package.json'),
    JSON.stringify(pkgJson, null, 2) + '\n',
  );
  console.log(`generated ${scope}/${pkgName}@${version}`);
}

console.log('\nDone. Place each built binary into:');
for (const p of platforms) {
  const base = outRootArg ? path.join(packagesRoot, `lujo-mcp-${p.suffix}`) : `npm/packages/lujo-mcp-${p.suffix}`;
  console.log(`  ${base}/bin/lujo-mcp-server${p.platform === 'win32' ? '.exe' : ''}`);
}
