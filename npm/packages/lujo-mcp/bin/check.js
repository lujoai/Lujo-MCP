#!/usr/bin/env node
// postinstall check — verifies the platform-specific companion package landed.
// If optionalDependencies resolution failed (e.g. npm without --os or a very
// old npm that skips platform-matched optionals), fail loudly with a clear fix.
'use strict';
const path = require('path');
const fs = require('fs');

const platform =
  process.platform === 'win32' ? 'win32' : process.platform === 'darwin' ? 'osx' : process.platform === 'linux' ? 'linux' : process.platform;
const arch = process.arch === 'x64' ? 'x64' : process.arch === 'arm64' ? 'arm64' : process.arch;
const pkg = `lujo-mcp-${platform}-${arch}`;
const binFile = `lujo-mcp-server${process.platform === 'win32' ? '.exe' : ''}`;

// FIX: P2-F6 —— 仅 CI 实际构建的平台有官方预编译包：win32-x64 / linux-x64 / osx-arm64。
// 其他平台（如 linux-arm64、osx-x64 用户）上 optionalDependencies 全部跳过属预期行为，
// 安装本应成功。此前对任何平台缺失都 exit 1 并指引"安装一个不存在的平台包"，导致
// 该平台 install 硬失败 + 报错指引装不到东西。这里先把当前平台分到两个分支：
//   - 受支持平台 → 沿用严格的"缺失/版本不匹配"校验；
//   - 不受支持平台 → 给出清晰说明并以 0 退出（不阻断 npm install）。
// 支持集需与 npm/scripts/gen-platform-packages.js 的平台数组保持同步。
const supportedPlatforms = new Set(['win32-x64', 'linux-x64', 'osx-arm64']);

function pkgVersionAt(pkgDir) {
  try {
    return JSON.parse(fs.readFileSync(path.join(pkgDir, 'package.json'), 'utf8')).version || null;
  } catch {
    return null;
  }
}

let metaV = null;
try {
  metaV = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf8')).version || null;
} catch {
  metaV = null;
}

// 非受支持平台：无官方预编译包属预期，不阻断安装，仅提示源码途径。
if (!supportedPlatforms.has(pkg)) {
  console.error(
    `[lujo-mcp-server] 当前平台（${pkg}）暂无官方预编译二进制。\n` +
      '官方预编译包仅支持: win32-x64, linux-x64, osx-arm64。\n' +
      '请改用 Python 源码方式运行（见项目 README），或在 GitHub issues 反馈需要支持该平台。'
  );
  process.exit(0);
}

let exe = null;
const mismatches = [];
let dir = __dirname;
for (;;) {
  for (const sub of [path.join('node_modules', '@lujoai', pkg), path.join('node_modules', pkg)]) {
    const pkgDir = path.join(dir, sub);
    const candidate = path.join(pkgDir, 'bin', binFile);
    if (fs.existsSync(candidate)) {
      const v = pkgVersionAt(pkgDir);
      if (!metaV || !v || v === metaV) {
        exe = candidate;
        break;
      }
      mismatches.push(`${pkgDir} (platform ${v} ≠ meta ${metaV})`);
    }
  }
  if (exe) break;
  const parent = path.dirname(dir);
  if (parent === dir) break;
  dir = parent;
}

if (exe) {
  process.exit(0); // good
}

if (mismatches.length) {
  console.error(`[lujo-mcp-server] platform package '${pkg}' was found but its version does not match the meta package @${metaV}:`);
  for (const m of mismatches) console.error(`  - ${m}`);
  console.error('');
  console.error('The install is corrupt or version-mismatched. Reinstall to fix:');
  console.error('');
  console.error('    npm install -g @lujoai/lujo-mcp');
  process.exit(1);
}

console.error(`[lujo-mcp-server] platform package '${pkg}' was not installed (binary missing at ${binFile}).`);
console.error('');
console.error('This usually happens when npm did not resolve platform-matched optionalDependencies.');
console.error('Fix by installing the platform package explicitly:');
console.error('');
console.error(`    npm install -g @lujoai/${pkg}`);
console.error('');
console.error('Or reinstall with a fresh lockfile:');
console.error('');
console.error('    npm install -g @lujoai/lujo-mcp --force');
process.exit(1);
