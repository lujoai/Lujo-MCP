/**
 * npm 启动器（bin/cli.js）单元测试 —— Node 原生 test runner，无额外依赖。
 *
 * 背景：v0.7.4 曾因平台包名白名单写成无前缀后缀，npm 启动器在所有平台 100%
 * 启动失败并连发多个版本未被发现（当时发布冒烟只跑裸二进制）。静态白名单一致性
 * 已有 tests/unit/test_distribution_smoke.py 守卫；本文件覆盖只有真正执行启动器
 * 才能发现的三类问题：
 *   1. 参数拼装（npm 默认统一 stdio + HTTP，--no-http 显式退回纯 stdio）；
 *   2. 平台二进制在 npm 两种落盘布局（提升 / 嵌套）下的解析；
 *   3. 终止信号转发给子进程（POSIX —— Windows 无法向 node 投递 SIGTERM，跳过）。
 *
 * 运行：node --test npm/test/launcher.test.js
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn, spawnSync } = require("child_process");

const CLI = path.join(__dirname, "..", "packages", "lujo-mcp", "bin", "cli.js");
const EXE = process.platform === "win32" ? "lujo-mcp-server.exe" : "lujo-mcp-server";
const PKG = require(CLI).platformPackageName();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 在临时目录里搭出一棵 npm 安装树，并返回其中 cli.js 副本的模块导出。
// binaryNameFor 以 __dirname 为起点向上走目录树，所以必须在副本上执行才等价。
function makeInstallTree(layout, { binary } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lujo-launcher-"));
  const container =
    layout === "hoisted"
      ? path.join(root, "node_modules", "@lujoai")
      : path.join(root, "node_modules", "@lujoai", "lujo-mcp", "node_modules", "@lujoai");
  const binDir = path.join(container, PKG, "bin");
  fs.mkdirSync(binDir, { recursive: true });
  const binaryPath = path.join(binDir, EXE);
  if (binary === "node") {
    fs.copyFileSync(process.execPath, binaryPath);
    fs.chmodSync(binaryPath, 0o755);
  } else if (binary !== undefined) {
    fs.writeFileSync(binaryPath, binary === "" ? "" : binary);
  }
  const launcherBin = path.join(root, "node_modules", "@lujoai", "lujo-mcp", "bin");
  fs.mkdirSync(launcherBin, { recursive: true });
  const cliCopy = path.join(launcherBin, "cli.js");
  fs.copyFileSync(CLI, cliCopy);
  return { root, cliCopy, binaryPath, mod: require(cliCopy) };
}

function cleanup(root) {
  fs.rmSync(root, { recursive: true, force: true });
}

// ── 1. 参数拼装 ──

test("默认（无参数）注入 --http：npm 用户开箱即得统一 stdio + HTTP", () => {
  assert.deepStrictEqual(require(CLI).translateArgs([]), ["--http"]);
});

test("--no-http 被剥离且不再注入 --http（纯 stdio 逃生门）", () => {
  assert.deepStrictEqual(require(CLI).translateArgs(["--no-http"]), []);
});

test("已显式给出 --http 时不重复注入", () => {
  assert.deepStrictEqual(require(CLI).translateArgs(["--http"]), ["--http"]);
});

test("其余参数原样透传，--http 追加在末尾", () => {
  assert.deepStrictEqual(require(CLI).translateArgs(["--http-port", "9001"]), ["--http-port", "9001", "--http"]);
});

test("--no-http 与 --http 同时出现时显式 --http 生效", () => {
  // 记录既有语义：--no-http 只表达「别让启动器替你加 --http」，
  // 用户手写的 --http 优先。若将来改为 --no-http 优先，此用例应随之更新。
  assert.deepStrictEqual(require(CLI).translateArgs(["--no-http", "--http"]), ["--http"]);
});

test("translateArgs 不修改调用方数组", () => {
  const original = ["--http-port", "9001"];
  require(CLI).translateArgs(original);
  assert.deepStrictEqual(original, ["--http-port", "9001"]);
});

// ── 2. 平台包名与二进制解析 ──

test("platformPackageName 产出带 lujo- 前缀的完整包名", () => {
  // v0.7.4 缺陷的形状：产出带前缀、白名单不带前缀 → 两侧永不相等。
  assert.match(require(CLI).platformPackageName(), /^lujo-mcp-[a-z0-9]+-[a-z0-9]+$/);
});

for (const layout of ["hoisted", "nested"]) {
  test(`平台二进制在 ${layout} 布局下可被解析到`, () => {
    const tree = makeInstallTree(layout, { binary: "" });
    try {
      assert.strictEqual(tree.mod.binaryNameFor(PKG), tree.binaryPath);
    } finally {
      cleanup(tree.root);
    }
  });
}

test("平台二进制缺失时 binaryNameFor 返回 null（由上层给出安装指引）", () => {
  const tree = makeInstallTree("hoisted"); // 不写二进制
  try {
    assert.strictEqual(tree.mod.binaryNameFor(PKG), null);
  } finally {
    cleanup(tree.root);
  }
});

// ── 3. prepublishOnly 守卫必须"活着" ──

test("check-clean-bin.js 真的执行了 SDK 副本比对（而非静默跳过）", () => {
  // 该脚本在 v0.7.2~v0.7.6 期间把仓库源路径上溯少算一级，指向永不存在的
  // npm/browser-sdk/ai-debug.js，existsSync 组合恒 false → 比对从未运行，
  // 守卫只产出绿灯却不守护任何东西。这里用它的输出证明比对确实在跑。
  const metaDir = path.join(__dirname, "..", "packages", "lujo-mcp");
  const r = spawnSync(process.execPath, [path.join(metaDir, "scripts", "check-clean-bin.js")], {
    encoding: "utf8",
  });
  assert.strictEqual(r.status, 0, `守卫应通过，stderr=${r.stderr}`);
  assert.match(r.stdout, /copy matches the repository source/,
    `守卫没有执行比对（可能又找不到被守护的源文件）：stdout=${r.stdout}`);
});

// ── 4. 终止信号转发（POSIX）──

test(
  "启动器收到 SIGTERM 时必须带离子进程，不留孤儿占用 HTTP 端口",
  { skip: process.platform === "win32" ? "Windows 上宿主用 TerminateProcess 结束进程，node 收不到 SIGTERM" : false },
  async () => {
    const tree = makeInstallTree("hoisted", { binary: "node" });
    try {
      // 子进程 = 拷贝的 node，被要求永不自行退出
      const launcherProc = spawn(
        process.execPath,
        [tree.cliCopy, "--no-http", "-e", "setInterval(function(){},1000)"],
        { stdio: ["pipe", "pipe", "pipe"] }
      );
      await sleep(1500);
      assert.strictEqual(launcherProc.exitCode, null, "启动器应仍在运行");

      const grandchildren = () =>
        spawnSync("pgrep", ["-P", String(launcherProc.pid)], { encoding: "utf8" })
          .stdout.trim().split(/\s+/).filter(Boolean);

      const kids = grandchildren();
      assert.strictEqual(kids.length, 1, `应有一个子进程，实际 ${kids.length} 个`);

      launcherProc.kill("SIGTERM");
      await new Promise((r) => launcherProc.once("close", r));
      await sleep(500);

      const survivors = grandchildren().filter((pid) => spawnSync("kill", ["-0", pid]).status === 0);
      assert.deepStrictEqual(survivors, [], "子进程未随启动器退出：留下孤儿会继续监听 HTTP 端口");
    } finally {
      cleanup(tree.root);
    }
  }
);
