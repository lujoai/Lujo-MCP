/**
 * Browser SDK unhandledrejection 非标准 reason 堆栈兜底单元测试
 * （v0.7.2：非 Error 拒绝载荷不再丢失全部堆栈帧）
 * 运行：node --test browser-sdk/test/sdk-rejection.test.js
 *
 * 覆盖：
 * - Promise.reject("string") → exc_type=UnhandledRejection，message 保留原文字段
 * - Promise.reject({name,message,stack}) → 结构化解析 type/message/frames
 * - 无堆栈可解析时 → 合成含 location.href 的兜底帧（AI 定位不丢失）
 * - Error 实例 → 行为与旧版一致（name/message/stack 照常解析）
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

// ── 先搭好 window/document 再 require：IIFE 以 (typeof window !== "undefined" ? window : this)
//    捕获 global，node --test 每个文件独立进程，模块缓存不跨文件污染 ──
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

class MockXHR {
  constructor() {
    this.headers = {};
    this.readyState = 0;
    this.status = 0;
    this.body = null;
    MockXHR.instances.push(this);
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
}
MockXHR.instances = [];

const win = makeTarget();
win.onerror = function preExistingOnerror() {};
win.fetch = function preExistingFetch() { return Promise.resolve({}); };
win.location = { href: "https://app.example.com/checkout" };
win.console = { error() {}, warn() {}, log() {} };
const doc = makeTarget();
doc.hidden = false;
doc.body = {};
doc.querySelector = () => doc.body;

globalThis.window = win;
globalThis.document = doc;
globalThis.XMLHttpRequest = MockXHR;

const SDK = require(path.join(__dirname, "..", "ai-debug.js"));

// 全部用例结束后摘除心跳/定时器句柄，否则 node --test 进程不会退出
test.after(() => {
  if (SDK._inited) SDK.destroy({ flush: false });
});

function initSdk() {
  // 心跳/批量定时器会让 node --test 进程挂起，先清理上一轮实例
  if (SDK._inited) SDK.destroy({ flush: false });
  SDK.init({
    endpoint: "http://localhost:8000",
    apiKey: "",
    captureErrors: true,
    captureNetwork: false,
    captureUI: false,
    captureConsole: false,
    sampleRate: 1.0,
    enableCompression: false,
    maxBatchesPerWindow: 1000,
    throttleWindowMs: 60000,
  });
}

function rejectionHandler() {
  const found = win.listeners.find((l) => l.type === "unhandledrejection");
  assert.ok(found, "init 应注册 unhandledrejection 监听");
  return found.handler;
}

function lastErrorPayload() {
  // 事件先进 _batchQueue，主动 flush 后经 /ingest/batch 发出（body 形如 {events:[{path,payload}]})
  SDK.flush();
  const xhr = MockXHR.instances[MockXHR.instances.length - 1];
  assert.ok(xhr && xhr.body, "flush 后应有批次上报");
  const batch = JSON.parse(xhr.body);
  const events = (batch && Array.isArray(batch.events)) ? batch.events : [];
  const errEvent = events.filter((ev) => ev.path === "/ingest/error").pop();
  assert.ok(errEvent, "批次中应包含 /ingest/error 事件");
  return errEvent.payload;
}

test("非 Error 对象 reason：结构化解析 name/message，且不再丢失全部堆栈帧", () => {
  initSdk();
  const handler = rejectionHandler();
  handler({
    reason: { name: "AuthError", message: "token expired", code: 401 },
  });
  const payload = lastErrorPayload();
  assert.equal(payload.exc_type, "AuthError");
  assert.equal(payload.message, "token expired");
  assert.ok(Array.isArray(payload.frames) && payload.frames.length >= 1,
    "无 stack 的对象 reason 也应拿到兜底帧（旧版为空数组）");
  assert.equal(payload.frames[0].file, "https://app.example.com/checkout");
  assert.equal(payload.frames[0].function, "unhandledrejection");
});

test("字符串 reason：message 保留原文并附带兜底帧", () => {
  initSdk();
  const handler = rejectionHandler();
  handler({ reason: "payment gateway timeout" });
  const payload = lastErrorPayload();
  assert.equal(payload.exc_type, "UnhandledRejection");
  assert.equal(payload.message, "payment gateway timeout");
  assert.ok(payload.frames.length >= 1, "字符串 reason 应有兜底帧");
});

test("带 stack 的对象 reason：按 stack 解析真实帧", () => {
  initSdk();
  const handler = rejectionHandler();
  handler({
    reason: {
      name: "ValidationError",
      message: "invalid cart",
      stack: "ValidationError: invalid cart\n    at validate (cart.js:42:15)",
    },
  });
  const payload = lastErrorPayload();
  assert.equal(payload.exc_type, "ValidationError");
  assert.equal(payload.message, "invalid cart");
  assert.equal(payload.frames[0].file, "cart.js");
  assert.equal(payload.frames[0].line, 42);
});

test("Error 实例 reason：行为与旧版一致", () => {
  initSdk();
  const handler = rejectionHandler();
  const err = new TypeError("cannot read properties of undefined");
  handler({ reason: err });
  const payload = lastErrorPayload();
  assert.equal(payload.exc_type, "TypeError");
  assert.equal(payload.message, "cannot read properties of undefined");
  assert.ok(payload.frames.length >= 1, "Error.stack 应照常解析出帧");
});
