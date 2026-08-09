#!/usr/bin/env node
// Generates the per-platform npm packages that ship the PyInstaller binary.
//
// Each platform package (lujo-mcp-<platform>-<arch>) contains exactly two files:
//   package.json   — declares the binary path
//   bin/lujo-mcp-server(.exe)  — the actual binary (added at build time)
//
// Usage:
//   node npm/scripts/gen-platform-packages.js 0.4.0-beta.1
//   # then place each built binary into the matching package's bin/ directory.
'use strict';
const fs = require('fs');
const path = require('path');

const version = process.argv[2];
if (!version) {
  console.error('Usage: node npm/scripts/gen-platform-packages.js <version>');
  process.exit(1);
}

// [node platform, node arch, pyinstaller artifact name, package suffix]
const platforms = [
  { platform: 'win32', arch: 'x64', exe: 'lujo-mcp-server.exe', suffix: 'win32-x64' },
  { platform: 'win32', arch: 'arm64', exe: 'lujo-mcp-server.exe', suffix: 'win32-arm64' },
  { platform: 'linux', arch: 'x64', exe: 'lujo-mcp-server', suffix: 'linux-x64' },
  { platform: 'linux', arch: 'arm64', exe: 'lujo-mcp-server', suffix: 'linux-arm64' },
  { platform: 'darwin', arch: 'arm64', exe: 'lujo-mcp-server', suffix: 'osx-arm64' },
];

const packagesRoot = path.resolve(__dirname, '..', 'packages');

for (const p of platforms) {
  const pkgName = `lujo-mcp-${p.suffix}`;
  const scope = '@lujoai';
  const pkgDir = path.join(packagesRoot, pkgName);
  fs.mkdirSync(path.join(pkgDir, 'bin'), { recursive: true });

  const pkgJson = {
    name: `${scope}/${pkgName}`,
    version,
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
  console.log(`  npm/packages/lujo-mcp-${p.suffix}/bin/lujo-mcp-server${p.platform === 'win32' ? '.exe' : ''}`);
}
