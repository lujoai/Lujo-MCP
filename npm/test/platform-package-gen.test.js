/**
 * 平台包生成器（gen-platform-packages.js）守卫测试 —— Node 原生 test runner，无额外依赖。
 *
 * 背景：生成器此前按内嵌 8 字段 schema 整体覆写平台包 package.json，仓库
 * manifest 声明的 "engines" 在发布产物中被静默丢弃（v0.7.6 / v0.7.7 线上
 * 三包 npm view engines 实证为空，见 CODE_REVIEW §9 与 PLAN_v0.7.8.md §3）。
 * 本文件守住两件事：
 *   1. 产物必须含非空 engines.node（回退默认 >=18）且 name/version/os/cpu/bin/files 齐全；
 *   2. 输出目录已有 manifest 声明 engines 时必须继承（这是「仓库声明了
 *      engines 而产物没有」场景的守卫：若生成器丢字段或只留回退，本用例变红）。
 *
 * 全部用例运行临时目录中的生成器副本，默认与显式输出路径均在沙箱内。
 * 异常参数逐次比较整个沙箱快照，测试结束清理，不污染仓库工作树。
 *
 * 运行：node --test npm/test/platform-package-gen.test.js
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const GEN = path.join(__dirname, "..", "scripts", "gen-platform-packages.js");
const VERSION = "0.0.0-test";
const PLATFORMS = ["win32-x64", "linux-x64", "osx-arm64"];

function runGenerator(outDir) {
  const res = spawnSync(process.execPath, [generatorFor(outDir), VERSION, "--out", outDir], {
    encoding: "utf8",
  });
  assert.strictEqual(res.status, 0, `生成器应正常退出：${res.stderr}`);
  return res;
}

function readPkg(outDir, suffix) {
  return JSON.parse(
    fs.readFileSync(path.join(outDir, `lujo-mcp-${suffix}`, "package.json"), "utf8"),
  );
}

function makeTempOut() {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), "lujo-pkggen-"));
  const out = path.join(sandbox, "out");
  const scriptDir = path.join(sandbox, "npm", "scripts");
  fs.mkdirSync(out);
  fs.mkdirSync(scriptDir, { recursive: true });
  fs.copyFileSync(GEN, path.join(scriptDir, "gen-platform-packages.js"));
  return out;
}

function generatorFor(out) {
  // 默认输出相对于 __dirname：只改 cwd 无法隔离仓库中的生成器。
  return path.join(out, "..", "npm", "scripts", "gen-platform-packages.js");
}

function snapshotTree(dir) {
  return fs.readdirSync(dir, { withFileTypes: true })
    .sort((a, b) => a.name.localeCompare(b.name))
    .map((entry) => {
      const file = path.join(dir, entry.name);
      return entry.isDirectory()
        ? [entry.name, "directory", snapshotTree(file)]
        : [entry.name, "file", fs.readFileSync(file).toString("base64")];
    });
}

function seedOutputRoots(out) {
  for (const outputRoot of [out, path.join(out, "..", "npm", "packages")]) {
    for (const suffix of PLATFORMS) {
      const pkgDir = path.join(outputRoot, `lujo-mcp-${suffix}`);
      fs.mkdirSync(pkgDir, { recursive: true });
      fs.writeFileSync(path.join(pkgDir, "package.json"),
        JSON.stringify({ name: suffix, version: "0.0.0-untouched" }));
    }
  }
}

function cleanup(out) {
  const sandbox = path.resolve(out, "..");
  assert.strictEqual(path.dirname(sandbox), path.resolve(os.tmpdir()));
  assert.ok(path.basename(sandbox).startsWith("lujo-pkggen-"));
  fs.rmSync(sandbox, { recursive: true, force: true });
}

test("空输出目录：三平台产物均含非空 engines.node（回退默认 >=18），六项字段齐全", () => {
  const out = makeTempOut();
  try {
    runGenerator(out);
    for (const suffix of PLATFORMS) {
      const pkg = readPkg(out, suffix);
      assert.strictEqual(pkg.name, `@lujoai/lujo-mcp-${suffix}`);
      assert.strictEqual(pkg.version, VERSION);
      assert.ok(
        typeof pkg.engines === "object" &&
          pkg.engines !== null &&
          typeof pkg.engines.node === "string" &&
          pkg.engines.node.length > 0,
        `${suffix} 产物应含非空 engines.node`,
      );
      assert.strictEqual(pkg.engines.node, ">=18");
      assert.ok(Array.isArray(pkg.os) && pkg.os.length > 0, `${suffix} 产物应含 os`);
      assert.ok(Array.isArray(pkg.cpu) && pkg.cpu.length > 0, `${suffix} 产物应含 cpu`);
      assert.ok(
        typeof pkg.bin === "object" &&
          typeof pkg.bin["lujo-mcp-server"] === "string" &&
          pkg.bin["lujo-mcp-server"].length > 0,
        `${suffix} 产物应含 bin 入口`,
      );
      assert.deepStrictEqual(pkg.files, ["bin"]);
    }
  } finally {
    cleanup(out);
  }
});

test("输出目录已有 manifest 声明 engines 时必须继承（仓库声明了 engines 而产物没有 → 此用例判红）", () => {
  // 这是 PLAN §3.3 要求的缺失场景守卫：预置带 engines（故意区别于默认 >=18）
  // 的 manifest 模拟 CI 覆写仓库 manifest 的真实流程。生成器若丢掉 engines
  // 字段、或只留回退默认而不再读取现有 manifest，本用例都会失败。
  const out = makeTempOut();
  try {
    const pkgDir = path.join(out, "lujo-mcp-win32-x64");
    fs.mkdirSync(pkgDir, { recursive: true });
    fs.writeFileSync(
      path.join(pkgDir, "package.json"),
      JSON.stringify({ name: "@lujoai/lujo-mcp-win32-x64", engines: { node: ">=20" } }, null, 2),
    );

    runGenerator(out);

    const pkg = readPkg(out, "win32-x64");
    assert.deepStrictEqual(
      pkg.engines,
      { node: ">=20" },
      "产物必须继承现有 manifest 的 engines，不得静默丢弃或回退默认",
    );
  } finally {
    cleanup(out);
  }
});

test("现有 manifest 无 engines 字段时回退默认 >=18（不继承空值）", () => {
  const out = makeTempOut();
  try {
    const pkgDir = path.join(out, "lujo-mcp-linux-x64");
    fs.mkdirSync(pkgDir, { recursive: true });
    fs.writeFileSync(
      path.join(pkgDir, "package.json"),
      JSON.stringify({ name: "@lujoai/lujo-mcp-linux-x64", version: "9.9.9" }, null, 2),
    );

    runGenerator(out);

    const pkg = readPkg(out, "linux-x64");
    assert.deepStrictEqual(pkg.engines, { node: ">=18" });
  } finally {
    cleanup(out);
  }
});

test("产物保持确定性格式：JSON 两空格缩进 + 末尾换行，engines 位于 version 之后", () => {
  const out = makeTempOut();
  try {
    runGenerator(out);
    const raw = fs.readFileSync(
      path.join(out, "lujo-mcp-osx-arm64", "package.json"),
      "utf8",
    );
    assert.ok(raw.endsWith("\n"), "产物应以换行结尾");
    assert.strictEqual(raw, JSON.stringify(JSON.parse(raw), null, 2) + "\n", "产物应为稳定 2 空格缩进 JSON");
    const keys = Object.keys(JSON.parse(raw));
    assert.ok(keys.indexOf("engines") === keys.indexOf("version") + 1, "engines 应紧跟 version（字段顺序约定）");
  } finally {
    cleanup(out);
  }
});

// ── 参数校验守卫：异常参数必须非零退出，且在任何文件写入之前拒绝 ──

test("异常参数（--out 缺值 / --out= 空值 / --out 后跟另一个参数 / 未知参数）非零退出且不产生任何写入", () => {
  const out = makeTempOut();
  try {
    seedOutputRoots(out);
    const sandbox = path.dirname(out);
    const before = snapshotTree(sandbox);

    const badArgSets = [
      ["--out"],              // 缺值（行尾）
      ["--out="],             // 等号后空值
      ["--out", ""],          // 独立空字符串值
      ["--out", "--out=x"],   // --out 的值是另一个参数 → 视为缺值
      ["--bogus"],            // 未知参数
      ["--out", out, "extra-positional"], // 未知位置参数
    ];
    for (const badArgs of badArgSets) {
      const res = spawnSync(process.execPath, [generatorFor(out), VERSION, ...badArgs], {
        encoding: "utf8",
        cwd: sandbox,
      });
      assert.ifError(res.error);
      assert.strictEqual(res.signal, null);
      assert.deepStrictEqual(snapshotTree(sandbox), before,
        `异常参数 ${JSON.stringify(badArgs)} 不得改写默认或显式输出目录，也不得创建文件或目录`);
      assert.ok(
        Number.isInteger(res.status) && res.status !== 0,
        `异常参数 ${JSON.stringify(badArgs)} 应非零退出（实际 status=${res.status}）`,
      );
      const message = `${res.stderr || ""}${res.stdout || ""}`;
      assert.ok(
        message.includes("error") || message.includes("Usage"),
        `异常参数 ${JSON.stringify(badArgs)} 应给出错误提示（实际输出：${message.slice(0, 200)}）`,
      );
    }

  } finally {
    cleanup(out);
  }
});

test("合法的 --out=<dir> 等号写法与 --out <dir> 等价", () => {
  const out = makeTempOut();
  try {
    const res = spawnSync(process.execPath, [generatorFor(out), VERSION, `--out=${out}`], {
      encoding: "utf8",
    });
    assert.strictEqual(res.status, 0, `等号写法应正常退出：${res.stderr}`);
    for (const suffix of PLATFORMS) {
      assert.ok(
        fs.existsSync(path.join(out, `lujo-mcp-${suffix}`, "package.json")),
        `等号写法应产出 ${suffix} 的 package.json`,
      );
    }
    assert.strictEqual(readPkg(out, "win32-x64").version, VERSION);
  } finally {
    cleanup(out);
  }
});

test("version 位置误传参数（如 --out dir 但漏了 version）应报错退出", () => {
  const out = makeTempOut();
  try {
    seedOutputRoots(out);
    const sandbox = path.dirname(out);
    const before = snapshotTree(sandbox);
    for (const args of [[], ["--out", out]]) {
      const res = spawnSync(process.execPath, [generatorFor(out), ...args], {
        encoding: "utf8",
        cwd: sandbox,
      });
      assert.ifError(res.error);
      assert.strictEqual(res.signal, null);
      assert.deepStrictEqual(snapshotTree(sandbox), before,
        "缺少 version 不得改写默认或显式输出目录");
      assert.ok(Number.isInteger(res.status) && res.status !== 0, "缺少 version 应非零退出");
      assert.match(res.stderr, /Usage|error/);
    }
  } finally {
    cleanup(out);
  }
});

// 注：完全省略 --out 时默认写 npm/packages 的行为由发布流水线（CI）实际覆盖；
// 默认路径同样位于生成器副本旁的临时 npm/packages，异常参数回归不会污染仓库。
