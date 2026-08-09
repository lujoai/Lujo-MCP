#!/usr/bin/env node
// lujo-mcp-server launcher — locates the platform-specific binary in the companion
// lujo-mcp-<platform>-<arch> package and spawns it as the MCP stdio server.
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
    const meta = JSON.parse(fs.readFileSync(metaPath, 'utf8'));
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
  const bin = binaryNameFor(pkg);
  if (!bin) {
    console.error(`[lujo-mcp-server] could not locate the ${pkg} binary.
This usually means the platform package was not installed. Install it explicitly:
    npm install -g @lujoai/${pkg}
or reinstall the meta package:
    npm install -g @lujoai/lujo-mcp`);
    process.exit(1);
  }
  const child = spawn(bin, process.argv.slice(2), {
    stdio: 'inherit',
    windowsHide: true,
  });
  child.on('error', (err) => {
    console.error(`[lujo-mcp-server] failed to spawn ${bin}: ${err.message}`);
    process.exit(1);
  });
  child.on('exit', (code, signal) => {
    if (signal) process.kill(process.pid, signal);
    else process.exit(code == null ? 0 : code);
  });
}

run();
