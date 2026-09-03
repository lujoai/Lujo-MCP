#!/usr/bin/env node
// prepublishOnly guard — ensures the committed bin/cli.js and bin/check.js are
// present and deterministic before publishing the meta package.
'use strict';
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const required = ['bin/cli.js', 'bin/check.js', 'browser-sdk/ai-debug.js'];

let ok = true;
for (const rel of required) {
  const full = path.join(root, rel);
  if (!fs.existsSync(full)) {
    console.error(`[lujo-mcp-server] missing required file before publish: ${rel}`);
    ok = false;
  }
}

// v0.7.2: browser-sdk 随主包分发（CDN 一行引用的前提）。防止仓库源与分发副本
// 漂移：两者内容不一致时拒绝发布，先同步再发。
const sdkSource = path.resolve(root, '..', '..', 'browser-sdk', 'ai-debug.js');
const sdkCopy = path.join(root, 'browser-sdk', 'ai-debug.js');
if (fs.existsSync(sdkSource) && fs.existsSync(sdkCopy)) {
  if (!fs.readFileSync(sdkSource).equals(fs.readFileSync(sdkCopy))) {
    console.error(
      '[lujo-mcp-server] browser-sdk/ai-debug.js drifted from npm/packages/lujo-mcp/browser-sdk/ai-debug.js.\n' +
        'Sync the copy before publishing: cp browser-sdk/ai-debug.js npm/packages/lujo-mcp/browser-sdk/'
    );
    ok = false;
  }
}

// Refuse to publish if a stale platform binary slipped into the meta package.
const binDir = path.join(root, 'bin');
if (fs.existsSync(binDir)) {
  const stale = fs.readdirSync(binDir).filter((f) => /lujo-mcp-server(\.exe)?$/.test(f));
  if (stale.length) {
    console.error(`[lujo-mcp-server] platform binary should NOT be inside the meta package: ${stale.join(', ')}`);
    ok = false;
  }
}

if (!ok) process.exit(1);
process.exit(0);
