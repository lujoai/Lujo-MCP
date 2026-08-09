#!/usr/bin/env node
// prepublishOnly guard — ensures the committed bin/cli.js and bin/check.js are
// present and deterministic before publishing the meta package.
'use strict';
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const required = ['bin/cli.js', 'bin/check.js'];

let ok = true;
for (const rel of required) {
  const full = path.join(root, rel);
  if (!fs.existsSync(full)) {
    console.error(`[lujo-mcp-server] missing required file before publish: ${rel}`);
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
