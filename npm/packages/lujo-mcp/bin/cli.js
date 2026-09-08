#!/usr/bin/env node
// lujo-mcp-server launcher — locates the platform-specific binary in the companion
// lujo-mcp-<platform>-<arch> package and starts the local unified server.
//
// Mirrors the pattern used by esbuild/@biomejs/@nuphus: the root package is a thin
// meta package whose optionalDependencies install only the binary for the current
// platform. This file is the `bin` entry npm exposes as `lujo-mcp-server`.
'use strict';
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');

// Map Node's process.platform/arch → our platform package suffix.
function platformPackageName() {
  const platform =
    process.platform === 'win32' ? 'win32' : process.platform === 'darwin' ? 'osx' : process.platform === 'linux' ? 'linux' : process.platform;
  const arch = process.arch === 'x64' ? 'x64' : process.arch === 'arm64' ? 'arm64' : process.arch;
  return `lujo-mcp-${platform}-${arch}`;
}

function binaryInDir(pkgDir, pkg, binFile) {
  // The platform package always lays the binary at bin/lujo-mcp-server(.exe).
  const candidate = path.join(pkgDir, 'bin', binFile);
  if (fs.existsSync(candidate)) return candidate;
  // Fall back to reading the platform package's package.json bin field.
  const metaPath = path.join(pkgDir, 'package.json');
  if (fs.existsSync(metaPath)) {
    // FIX(v0.7.1-b5-2): 平台包 package.json 损坏时 JSON.parse 抛异常会直接崩掉
    // 启动器；降级为读不到 bin 字段（与文件不存在同语义），交给上层统一报错。
    let meta;
    try {
      meta = JSON.parse(fs.readFileSync(metaPath, 'utf8'));
    } catch {
      meta = null;
    }
    if (meta && meta.bin) {
      const binTarget = typeof meta.bin === 'string' ? meta.bin : meta.bin['lujo-mcp-server'] || meta.bin[pkg];
      if (binTarget) {
        const p = path.join(pkgDir, binTarget);
        if (fs.existsSync(p)) return p;
      }
    }
  }
  return null;
}

function binaryNameFor(pkg) {
  const binFile = 'lujo-mcp-server' + (process.platform === 'win32' ? '.exe' : '');
  // npm can lay out the optional platform packages two ways:
  //   hoisted sibling or nested under the meta package. Walk up the
  //   directory tree checking node_modules at each level, like `require` does.
  let dir = __dirname;
  for (;;) {
    for (const sub of [path.join('node_modules', '@lujoai', pkg), path.join('node_modules', pkg)]) {
      const found = binaryInDir(path.join(dir, sub), pkg, binFile);
      if (found) return found;
    }
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

function run() {
  const pkg = platformPackageName();
  // FIX: P2-F6 —— 与 check.js 一致：非 CI 构建平台无官方预编译包，给出清晰指引而非
  // 提示安装一个不存在的平台包。支持集需与 gen-platform-packages.js 平台数组保持同步。
  // FIX(v0.7.4 P0)：白名单此前写成无前缀后缀（win32-x64），与 platformPackageName()
  // 产出的完整包名（lujo-mcp-win32-x64）永不相等 → 启动器在所有平台 100% exit 1，
  // npm stdio 用户自 v0.6.8 起完全无法启动且 CI 未发现（发布冒烟只测裸二进制）。
  // 白名单必须用完整包名，test_distribution_smoke 有静态一致性守卫。
  const supportedPlatforms = new Set(['lujo-mcp-win32-x64', 'lujo-mcp-linux-x64', 'lujo-mcp-osx-arm64']);
  if (!supportedPlatforms.has(pkg)) {
    console.error(
      `[lujo-mcp-server] 当前平台（${pkg}）暂无官方预编译二进制。\n` +
        '官方预编译包仅支持: win32-x64, linux-x64, osx-arm64。\n' +
        '请改用 Python 源码方式运行（见项目 README），或在 GitHub issues 反馈需要支持该平台。'
    );
    process.exit(1);
  }
  const bin = binaryNameFor(pkg);
  if (!bin) {
    console.error(`[lujo-mcp-server] could not locate the ${pkg} binary.
This usually means the platform package was not installed. Install it explicitly:
    npm install -g @lujoai/${pkg}
or reinstall the meta package:
    npm install -g @lujoai/lujo-mcp`);
    process.exit(1);
  }
  // npm users get the useful local mode out of the box: one process serves both
  // MCP stdio and localhost HTTP, so Browser SDK /ingest events are immediately
  // visible to the MCP client. Keep an explicit --no-http escape hatch for
  // clients that require a pure stdio process (and for release smoke tests).
  const child = spawn(bin, translateArgs(process.argv.slice(2)), {
    stdio: 'inherit',
    windowsHide: true,
  });
  child.on('error', (err) => {
    console.error(`[lujo-mcp-server] failed to spawn ${bin}: ${err.message}`);
    process.exit(1);
  });

  // FIX(R8): 终止信号必须转发。宿主（Claude / Codex / Trae 等）停止 MCP 服务
  // 时只处理它直接启动的那个进程——也就是本启动器——被 spawn 出来的二进制不会
  // 被自动带走（Windows 没有 POSIX 的进程组/信号语义，实测杀掉启动器后二进制
  // 存活并继续监听统一模式的 HTTP 端口）。孤儿会接收浏览器 SDK 发往该端口的
  // 全部上报，新起的实例抢不到端口，当前会话因此表现为「报过错但
  // diagnose_issue 查不到任何现场」。
  // 子进程自身退出时由下面的 exit 处理器决定本进程退出码，这里只负责带离。
  let forwarding = false;
  function forward(signal) {
    if (forwarding) return;
    forwarding = true;
    try {
      child.kill(signal);
    } catch {
      // 子进程可能已经退出：无需要做的，交给 exit 兜底
    }
  }
  // Windows 收不到 SIGTERM/SIGHUP（注册无害且永不触发），但能收到 SIGBREAK。
  for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP', 'SIGBREAK']) {
    process.on(sig, () => forward('SIGTERM'));
  }
  process.on('uncaughtException', (err) => {
    console.error(`[lujo-mcp-server] launcher error: ${err && err.message}`);
    forward('SIGTERM');
    process.exit(1);
  });
  // 任何原因导致的本进程退出（含上面的 process.exit 路径）都不能把子进程留下。
  process.on('exit', () => forward('SIGTERM'));

  child.on('exit', (code, signal) => {
    if (signal) process.kill(process.pid, signal);
    else process.exit(code == null ? 0 : code);
  });
}

// 参数拼装与 npm 默认模式：抽出以便在无平台二进制的环境下直接单测。
function translateArgs(requestedArgs) {
  const args = Array.isArray(requestedArgs) ? requestedArgs.slice() : [];
  const disableHttp = args.includes('--no-http');
  const childArgs = args.filter((arg) => arg !== '--no-http');
  if (!disableHttp && !childArgs.includes('--http')) childArgs.push('--http');
  return childArgs;
}

if (require.main === module) {
  run();
}

module.exports = { platformPackageName, translateArgs, binaryNameFor };
