/**
 * Browser SDK 传输层缺陷修复单测（v0.6.7 第三档正确性 Major）：
 *   1. gzip 回退乱码：失败回退/localStorage 降级必须用原始明文，而非 gzip 字节
 *   2. pagehide 丢数据：页面隐藏时同步冲刷暂存队列（sendBeacon / 同步 XHR）
 *   3. 节流齐发：暂存批次由单一定时器按间隔逐条错发，而非同一时刻齐发
 *
 * 运行：node --test browser-sdk/test/sdk-transport-fixes.test.js
 *
 * Node 环境无 FileReader / localStorage / XMLHttpRequest / sendBeacon，
 * 本文件在调用 SDK 前安装这些 Web API 的最小桩，并用真实 CompressionStream
 * 打通 gzip 管道。每个测试重新 require SDK，保证闭包内部队列状态隔离。
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const MODULE_PATH = path.join(__dirname, "..", "ai-debug.js");

// ── Web API 桩 ──
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

// Node 无 FileReader，用 Blob.arrayBuffer() 补最小实现（gzip 管道最后一环）
class NodeFileReader {
  readAsArrayBuffer(blob) {
    blob.arrayBuffer().then((buf) => {
      this.result = buf;
      if (this.onload) this.onload();
    }).catch(() => {});
  }
}
globalThis.XMLHttpRequest = MockXHR;
globalThis.FileReader = NodeFileReader;

function freshSDK() {
  delete require.cache[require.resolve(MODULE_PATH)];
  return require(MODULE_PATH);
}

async function settle() {
  await new Promise((r) => setTimeout(r, 30));
}

test("gzip 失败回退：localStorage 存原始明文而非 gzip 字节", async () => {
  const SDK = freshSDK();
  MockXHR.reset();
  clearLocalStorage();
  SDK._setConfig("endpoint", "http://localhost:8000");
  SDK._setConfig("batchSize", 1);
  SDK._setConfig("maxRetries", 0);
  SDK._setConfig("enableCompression", true);
  SDK._setConfig("compressionThreshold", 1);
  SDK._setConfig("enableLocalStorageFallback", true);
  MockXHR.respond(500); // 压缩发送失败，maxRetries=0 → 直接落 localStorage

  SDK.reportError(new Error("boom"), { big: "x".repeat(5000) });
  await settle();

  assert.ok(MockXHR.instances.length >= 1, "应发生一次压缩 XHR 发送");
  const stored = globalThis.localStorage.getItem("ai-debug-pending-batches");
  assert.ok(stored, "重试耗尽后应写入 localStorage");
  const wrapped = JSON.parse(stored);
  assert.equal(wrapped.length, 1);
  // 关键：data 是原始明文 JSON（可解析出 events），而非 gzip 二进制文本
  const events = JSON.parse(wrapped[0].data).events;
  assert.equal(events.length, 1);
  assert.equal(events[0].path, "/ingest/error");
});

test("接收端拒绝 gzip（400）→ 用原始未压缩数据重发一次", async () => {
  const SDK = freshSDK();
  MockXHR.reset();
  SDK._setConfig("endpoint", "http://localhost:8000");
  SDK._setConfig("batchSize", 1);
  SDK._setConfig("maxRetries", 0);
  SDK._setConfig("enableCompression", true);
  SDK._setConfig("compressionThreshold", 1);
  SDK._setConfig("enableLocalStorageFallback", true);
  MockXHR.respond(400); // 首次压缩请求被拒
  MockXHR.respond(200); // 未压缩重发成功

  SDK.reportError(new Error("boom"), { big: "x".repeat(5000) });
  await settle();

  assert.equal(MockXHR.instances.length, 2, "应发生压缩 + 未压缩共 2 次发送");
  const first = MockXHR.instances[0];
  const second = MockXHR.instances[1];
  assert.equal(first.headers["Content-Encoding"], "gzip");
  assert.ok(!("Content-Encoding" in second.headers), "未压缩重发不得携带 gzip 头");
  assert.ok(typeof second.body === "string");
  assert.equal(JSON.parse(second.body).events.length, 1);
});

test("pagehide：同步冲刷暂存批次与当前批次，不丢数据", () => {
  const SDK = freshSDK();
  MockXHR.reset();
  SDK._setConfig("endpoint", "http://localhost:8000");
  SDK._setConfig("batchSize", 100);           // 不自动 flush，事件先积在队列
  SDK._setConfig("enableCompression", false); // 走直发路径
  SDK._setConfig("maxBatchesPerWindow", 1);   // 1 次发送即占满节流窗口
  SDK._setConfig("throttleWindowMs", 60000);  // 长窗口，测试期间不会自然过期

  SDK.reportError(new Error("e1"));
  SDK.flush(); // 第一批：窗口未满，正常发送（占满窗口）
  assert.equal(MockXHR.instances.length, 1);

  SDK.reportError(new Error("e2"));
  SDK.flush(); // 第二批：窗口已满 → 被延迟入 _pendingBatches

  SDK.reportError(new Error("e3")); // 第三批：留在 _batchQueue
  SDK._flushBatch(true);            // 模拟页面隐藏：同步冲刷

  const syncSends = MockXHR.instances.filter((x) => x.async === false);
  // 1 条暂存批次 + 1 条当前批次，均同步发送（无 sendBeacon 时走同步 XHR）
  assert.equal(syncSends.length, 2, "pagehide 应同步冲刷暂存批次 + 当前批次");
  assert.equal(MockXHR.instances.length, 3, "第二批不应丢失（应被同步补发）");
});

test("节流：暂存批次由单一定时器间隔错发，而非齐发", () => {
  const SDK = freshSDK();
  MockXHR.reset();
  SDK._setConfig("endpoint", "http://localhost:8000");
  SDK._setConfig("batchSize", 1);
  SDK._setConfig("enableCompression", false);
  SDK._setConfig("maxBatchesPerWindow", 2);
  SDK._setConfig("throttleWindowMs", 5000);

  const realSetTimeout = globalThis.setTimeout;
  const realClearTimeout = globalThis.clearTimeout;
  const realNow = Date.now;
  const timers = [];
  globalThis.setTimeout = (fn, delay) => { const t = { fn: fn, delay: delay }; timers.push(t); return t; };
  globalThis.clearTimeout = () => {};
  Date.now = () => 0;

  try {
    SDK.reportError(new Error("e1")); // 窗口未满，直接发
    SDK.reportError(new Error("e2")); // 窗口满（2 条）
    SDK.reportError(new Error("e3")); // 开始暂存 → 调度 1 个 pacer
    SDK.reportError(new Error("e4")); // 继续暂存 → 不再新增定时器

    // 核心：两条暂存批次只调度了 1 个错发送定时器（旧实现会为每条各调度 1 个 → 齐发）
    assert.equal(timers.length, 1, "暂存多条应只调度 1 个错发送定时器");
    assert.equal(timers[0].delay, 5000);

    const before = MockXHR.instances.length;
    timers[0].fn(); // 手动触发 pacer：每次只发 1 条
    assert.equal(MockXHR.instances.length - before, 1, "pacer 每次只发 1 条");
    assert.equal(timers.length, 2, "发完 1 条后应续期 1 个下一轮定时器");
    assert.equal(timers[1].delay, 2500, "错发间隔 = 窗口/批次数");

    timers[1].fn(); // 发最后一条
    assert.equal(MockXHR.instances.length - before, 2, "最后一条单独发送");
    assert.equal(timers.length, 2, "暂存清空后不再续期定时器");
  } finally {
    globalThis.setTimeout = realSetTimeout;
    globalThis.clearTimeout = realClearTimeout;
    Date.now = realNow;
  }
});


// ---------------------------------------------------------------------------
// FIX: R7-G1 —— 压缩路径节流失效：时间戳必须同步登记在发送决策点
// ---------------------------------------------------------------------------

test("压缩×节流：压缩异步回调不再绕过节流配额", async () => {
  const SDK = freshSDK();
  MockXHR.reset();
  clearLocalStorage();
  SDK._setConfig("endpoint", "http://localhost:8000");
  SDK._setConfig("batchSize", 1);              // 每事件同步 flush（4 事件 → 4 批）
  SDK._setConfig("enableCompression", true);   // 压缩路径（真实 CompressionStream）
  SDK._setConfig("compressionThreshold", 1);   // 全部走压缩
  SDK._setConfig("maxBatchesPerWindow", 2);    // 窗口内只允许 2 个请求
  SDK._setConfig("throttleWindowMs", 60000);   // 长窗口，测试期间不自然过期

  // 4 次同步 flush：修复前压缩路径的时间戳只在异步回调里登记，JS 单线程下
  // 同步 while 循环跑完全部分片后回调才执行 → 所有分片看到过期时间戳，
  // maxBatchesPerWindow:2 时 4 个请求齐发（已实测复现）。
  for (let i = 0; i < 4; i++) SDK.reportError(new Error("gzip-throttle-" + i));

  await settle(); // 等压缩回调与 XHR 异步完成

  // 窗口配额 2：只应有 2 个请求发出，其余 2 批进入暂存队列
  assert.equal(MockXHR.instances.length, 2, "压缩路径下节流必须同步生效（修复前 4 个齐发）");

  // 暂存批次不丢数据：pagehide 同步冲刷后补发剩余 2 批
  MockXHR.reset();
  SDK._flushBatch(true);
  assert.equal(MockXHR.instances.length, 2, "pagehide 应同步冲刷暂存批次");
});
