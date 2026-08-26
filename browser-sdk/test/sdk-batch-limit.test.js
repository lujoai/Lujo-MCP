/**
 * Browser SDK 批量上限回归测试（CR-3 毒批循环修复）：
 *   1. 分片发送：单次 flush 队列 >100 条时按服务端上限（_MAX_BATCH_EVENTS=100）分片
 *   2. 413 拆分：批次被服务端 413（超 100 条上限）拒绝时对半拆分重发，而非整批重试
 *   3. 恢复路径：localStorage 暂存批次恢复时不得合并为单个 >100 条的请求
 *      （旧实现：10 批 × 20 条恢复后单次 flush → 必然 413 → 整批重试失败 →
 *       整批回写 localStorage → 下次启动重复，事件永远无法送达）
 *
 * 运行：node --test browser-sdk/test/sdk-batch-limit.test.js
 *
 * Node 环境无 XHR/localStorage，安装最小桩；每个测试重新 require SDK 隔离闭包状态
 * （与 sdk-transport-fixes.test.js 同一套路）。
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const MODULE_PATH = path.join(__dirname, "..", "ai-debug.js");

// ── Web API 桩（与 sdk-transport-fixes.test.js 相同的最小实现）──
class MockXHR {
  constructor() {
    this.headers = {};
    this.onreadystatechange = null;
    this.readyState = 0;
    this.status = 0;
    this.async = true;
    this.body = null;
    MockXHR.instances.push(this);
  }
  open(method, url, async) { this.method = method; this.url = url; this.async = async; }
  setRequestHeader(k, v) { this.headers[k] = v; }
  getResponseHeader(name) {
    const resp = this._resp || {};
    return (resp.headers && resp.headers[name]) || null;
  }
  send(body) {
    this.body = body;
    const resp = MockXHR.queue.length > 0 ? MockXHR.queue.shift() : { status: 200 };
    this._resp = resp;
    this.status = resp.status;
    this.responseText = resp.responseText || "";
    this.readyState = 4;
    if (this.onreadystatechange) this.onreadystatechange();
  }
}
MockXHR.instances = [];
MockXHR.queue = [];
MockXHR.reset = function () { MockXHR.instances = []; MockXHR.queue = []; };
MockXHR.respond = function (status) { MockXHR.queue.push({ status: status }); };

const localStorageStore = {};
globalThis.localStorage = {
  getItem: (k) => (k in localStorageStore ? localStorageStore[k] : null),
  setItem: (k, v) => { localStorageStore[k] = String(v); },
  removeItem: (k) => { delete localStorageStore[k]; },
};
function clearLocalStorage() {
  for (const k in localStorageStore) delete localStorageStore[k];
}

globalThis.XMLHttpRequest = MockXHR;

function freshSDK() {
  delete require.cache[require.resolve(MODULE_PATH)];
  return require(MODULE_PATH);
}

async function settle() {
  await new Promise((r) => setTimeout(r, 30));
}

// 服务端单请求事件上限（与 app/api/ingest.py _MAX_BATCH_EVENTS 对齐）
const SERVER_MAX_EVENTS = 100;

test("分片发送：150 条事件 → 2 个请求（100 + 50），不丢事件", async () => {
  const SDK = freshSDK();
  MockXHR.reset();
  SDK._setConfig("endpoint", "http://localhost:8000");
  SDK._setConfig("batchSize", 100000);          // 不自动 flush，攒满 150 条
  SDK._setConfig("enableCompression", false);
  SDK._setConfig("maxBatchesPerWindow", 1000);  // 节流不干预分片
  SDK._setConfig("throttleWindowMs", 60000);

  for (let i = 0; i < 150; i++) SDK.reportError(new Error("e" + i));
  SDK.flush();
  await settle();

  const bodies = MockXHR.instances
    .filter((x) => x.body)
    .map((x) => JSON.parse(x.body));
  assert.equal(bodies.length, 2, "150 条应分片为 2 个请求");
  assert.equal(bodies[0].events.length, SERVER_MAX_EVENTS);
  assert.equal(bodies[1].events.length, 50);
  const total = bodies.reduce((s, b) => s + b.events.length, 0);
  assert.equal(total, 150, "分片后事件不得丢失");
});

test("413 毒批：60 条批次被拒 → 对半拆分为 2 个请求重发，而非整批重试", async () => {
  const SDK = freshSDK();
  MockXHR.reset();
  SDK._setConfig("endpoint", "http://localhost:8000");
  SDK._setConfig("batchSize", 60);              // 60 条自动 flush 成 1 批
  SDK._setConfig("enableCompression", false);
  SDK._setConfig("maxBatchesPerWindow", 1000);
  SDK._setConfig("maxRetries", 3);
  SDK._setConfig("enableLocalStorageFallback", true);
  MockXHR.respond(413); // 服务端上限被调低的极端场景：批次超限
  MockXHR.respond(200);
  MockXHR.respond(200);

  for (let i = 0; i < 60; i++) SDK.reportError(new Error("e" + i));
  await settle();

  // 旧实现：413 走可重试路径 → 同样 60 条整批重试 3 次（同步桩下为 1 次内联重试链）
  // 新实现：413 → 立即拆分为 30 + 30 两个独立请求
  const bodies = MockXHR.instances
    .filter((x) => x.body)
    .map((x) => JSON.parse(x.body));
  assert.equal(bodies.length, 3, "原始 1 次 + 拆分重发 2 次");
  assert.equal(bodies[0].events.length, 60);
  assert.equal(bodies[1].events.length, 30);
  assert.equal(bodies[2].events.length, 30);
  // 不写回 localStorage（旧实现重试耗尽后整批回写 → 毒批滚大）
  assert.equal(globalThis.localStorage.getItem("ai-debug-pending-batches"), null);
});

test("恢复路径：3 个暂存批次共 150 条 → 分片为 ≤100 的请求，打破毒批循环", async () => {
  // 模拟 endpoint 宕机过夜后 localStorage 留下的暂存批次（旧格式：{timestamp, data}）
  const mkBody = (n, tag) => JSON.stringify({
    events: Array.from({ length: n }, (_, i) => ({
      path: "/ingest/error",
      payload: { message: tag + "-" + i },
    })),
  });
  localStorageStore["ai-debug-pending-batches"] = JSON.stringify([
    { timestamp: Date.now(), data: mkBody(50, "a") },
    { timestamp: Date.now(), data: mkBody(50, "b") },
    { timestamp: Date.now(), data: mkBody(50, "c") },
  ]);

  const SDK = freshSDK();
  MockXHR.reset();
  SDK._setConfig("endpoint", "http://localhost:8000");
  SDK._setConfig("enableCompression", false);
  SDK._setConfig("maxBatchesPerWindow", 1000);
  SDK._setConfig("batchSize", 100000);

  // 直接调用恢复（init 的 hook 安装在 Node 桩环境下有副作用）
  SDK._restorePendingBatches();
  await settle();

  const bodies = MockXHR.instances
    .filter((x) => x.body)
    .map((x) => JSON.parse(x.body));

  // 核心断言：任何请求都不得超过服务端上限（旧实现单请求 150 条必然 413）
  assert.ok(bodies.length >= 2, "150 条应分片为多个请求");
  for (const b of bodies) {
    assert.ok(
      b.events.length <= SERVER_MAX_EVENTS,
      `单请求 ${b.events.length} 条超过服务端上限 ${SERVER_MAX_EVENTS}`
    );
  }
  const total = bodies.reduce((s, b) => s + b.events.length, 0);
  assert.equal(total, 150, "恢复的 150 条事件应全部分片送达");
  assert.equal(
    globalThis.localStorage.getItem("ai-debug-pending-batches"),
    null,
    "恢复后 localStorage 应被消费清空"
  );
});

test("恢复 + 持续 413：事件量收敛，不无限循环", async () => {
  // 极端场景：服务端上限被误调低，恢复的批次反复 413 → 拆分应指数收敛
  const mkBody = (n) => JSON.stringify({
    events: Array.from({ length: n }, (_, i) => ({
      path: "/ingest/error",
      payload: { message: "x" + i },
    })),
  });
  localStorageStore["ai-debug-pending-batches"] = JSON.stringify([
    { timestamp: Date.now(), data: mkBody(20) },
    { timestamp: Date.now(), data: mkBody(20) },
  ]);

  const SDK = freshSDK();
  MockXHR.reset();
  SDK._setConfig("endpoint", "http://localhost:8000");
  SDK._setConfig("enableCompression", false);
  SDK._setConfig("maxBatchesPerWindow", 1000);
  SDK._setConfig("batchSize", 100000);
  SDK._setConfig("maxRetries", 0);
  SDK._setConfig("enableLocalStorageFallback", true);
  // 恢复的 40 条分片为 1 个请求（≤100）；该请求 413 → 拆 20+20 → 各 413 → 拆 10+10 ...
  // 队列响应按序消耗：413, 413, 413, 413, 200, 200, 200, 200（后续小批次被接受）
  for (let i = 0; i < 4; i++) MockXHR.respond(413);
  for (let i = 0; i < 8; i++) MockXHR.respond(200);

  SDK._restorePendingBatches();
  await settle();

  const sizes = MockXHR.instances
    .filter((x) => x.body)
    .map((x) => JSON.parse(x.body).events.length);
  assert.ok(sizes.length >= 5, "应发生原始 + 多轮拆分请求");
  // 拆分链上不存在"与原始批次同大小"的整批重试
  assert.ok(!sizes.slice(1).includes(sizes[0]), "413 后不得整批原样重试");
  // 所有已发出请求均 ≤ 上限
  for (const n of sizes) assert.ok(n <= SERVER_MAX_EVENTS);
});
