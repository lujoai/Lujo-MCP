/**
 * v0.7.1 Minor 批次1 SDK 回归测试
 * 运行：node --test browser-sdk/test/sdk-minor-reuse.test.js
 *
 * - FIX(v0.7.1-b1-9): XHR 对象复用时网络监听器累积（第 3 次复用后单个请求被记录 3 次）
 * - FIX(v0.7.1-b1-10): window.onerror 包装器丢弃原 handler 返回值（抑制默认上报失效）
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const MODULE_PATH = path.join(__dirname, "..", "ai-debug.js");

// ── 先搭好 window/document 再 require：IIFE 以 (typeof window !== "undefined" ? window : this)
//    捕获 global（同 sdk-destroy.test.js 的既定手法）──
function makeTarget() {
  const listeners = [];
  return {
    listeners,
    addEventListener(type, handler) {
      listeners.push({ type, handler });
    },
    removeEventListener(type, handler) {
      const idx = listeners.findIndex((l) => l.type === type && l.handler === handler);
      if (idx >= 0) listeners.splice(idx, 1);
    },
  };
}

// 带事件监听器的 MockXHR：网络捕获走 addEventListener("load"/"error"/...)
class EventedMockXHR {
  constructor() {
    this.headers = {};
    this.listeners = {};
    this.readyState = 0;
    this.status = 0;
    this.responseText = "";
    this.body = null;
    EventedMockXHR.instances.push(this);
  }
  open(method, url) { this.method = method; this.url = url; }
  setRequestHeader(k, v) { this.headers[k] = v; }
  getResponseHeader() { return null; }
  send(body) {
    this.body = body;
    this.status = 200;
    this.readyState = 4;
    if (this.onreadystatechange) this.onreadystatechange();
  }
  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }
  removeEventListener(type, fn) {
    const list = this.listeners[type];
    if (!list) return;
    const i = list.indexOf(fn);
    if (i >= 0) list.splice(i, 1);
  }
  listenerCount(type) {
    return (this.listeners[type] || []).length;
  }
  fire(type) {
    for (const fn of (this.listeners[type] || []).slice()) fn();
  }
}
EventedMockXHR.instances = [];
EventedMockXHR.reset = function () { EventedMockXHR.instances = []; };

const win = makeTarget();
const doc = makeTarget();
doc.body = {};
doc.querySelector = () => doc.body;
doc.hidden = false;
win.console = { error() {}, warn() {}, log() {} };

const localStorageStore = {};
globalThis.window = win;
globalThis.document = doc;
globalThis.XMLHttpRequest = EventedMockXHR;
globalThis.localStorage = {
  getItem: (k) => (k in localStorageStore ? localStorageStore[k] : null),
  setItem: (k, v) => { localStorageStore[k] = String(v); },
  removeItem: (k) => { delete localStorageStore[k]; },
};
// onerror 载荷构造引用 navigator（Node 20 无全局 navigator，Node 21+ 有且可能不可赋值）
try {
  Object.defineProperty(globalThis, "navigator", {
    value: { userAgent: "node-test" },
    configurable: true,
  });
} catch (e) { /* Node 21+ 原生 navigator 已存在，直接沿用 */ }

function freshSDK() {
  delete require.cache[require.resolve(MODULE_PATH)];
  return require(MODULE_PATH);
}

function sdkInit(SDK) {
  SDK.init({
    endpoint: "http://localhost:8000",
    captureUI: false,
    captureConsole: false,
    autoDetectUISilentFailures: false,
    sampleRate: 1,
    networkSampleRate: 1,
    batchSize: 1,           // 每条网络记录立即成批发送，便于按 body 计数
    maxRetries: 0,
    enableCompression: false,
    // 默认 maxBatchesPerWindow=2 会把第 3 批推迟到节流窗口后（与断言无关的
    // 干扰项），与既有测试一致调高
    maxBatchesPerWindow: 1000,
    throttleWindowMs: 60000,
  });
}

function collectNetworkEvents() {
  const events = [];
  for (const x of EventedMockXHR.instances) {
    if (!x.body || typeof x.body !== "string") continue;
    let parsed;
    try { parsed = JSON.parse(x.body); } catch (e) { continue; }
    for (const ev of (parsed.events || [])) events.push(ev);
  }
  return events.filter((e) => e.path === "/ingest/network");
}

function networkEventUrl(e) {
  return e && e.payload && e.payload.record && e.payload.record.url;
}

test("b1-9: XHR 对象复用 3 轮后单次 load 只记录 1 次（不叠加监听器）", async () => {
  const SDK = freshSDK();
  EventedMockXHR.reset();
  sdkInit(SDK);

  try {
    const xhr = new XMLHttpRequest();
    const urls = ["/api/a", "/api/b", "/api/c"];
    for (const u of urls) {
      xhr.open("GET", "http://app.example" + u);
      xhr.send();
      assert.equal(xhr.listenerCount("load"), 1,
        `复用第 ${urls.indexOf(u) + 1} 轮：load 监听器必须恰为 1 个（旧实现逐轮叠加）`);
      xhr.fire("load");
      // 终态 handler 自摘除：本轮结束后监听器应清零
      assert.equal(xhr.listenerCount("load"), 0, "终态 handler 触发后应自摘除");
    }

    await new Promise((r) => setTimeout(r, 30));
    const records = collectNetworkEvents();
    assert.equal(records.length, urls.length,
      `3 轮复用应恰好记录 3 条网络事件，实际 ${records.length}（叠加 bug 会记 6 条）`);
    for (const u of urls) {
      const hits = records.filter((e) => networkEventUrl(e) === "http://app.example" + u);
      assert.equal(hits.length, 1, `每个 URL 恰好 1 条记录，${u} 实际 ${hits.length}`);
    }
  } finally {
    // finally 保证断言失败也销毁实例：否则残留实例会经 HMR 兜底污染下一个用例
    SDK.destroy({ flush: false });
  }
});

test("b1-10: window.onerror 包装器透传原 handler 的返回值", async () => {
  const SDK = freshSDK();
  EventedMockXHR.reset();

  let origCalled = false;
  win.onerror = function () {
    origCalled = true;
    return true; // 返回 true = 抑制浏览器默认错误上报
  };

  sdkInit(SDK);
  const wrapped = win.onerror;
  assert.notStrictEqual(wrapped, null, "init 后 onerror 应被包装");

  const err = new Error("boom");
  const ret = wrapped.call(win, "boom", "app.js", 1, 10, err);

  assert.equal(origCalled, true, "原 handler 必须仍被调用");
  assert.strictEqual(ret, true,
    "包装器必须透传原 handler 返回值 true（旧实现恒 undefined，抑制语义失效）");

  await new Promise((r) => setTimeout(r, 30));
  SDK.destroy({ flush: false });
  win.onerror = null;
});
