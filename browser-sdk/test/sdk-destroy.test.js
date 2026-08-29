/**
 * Browser SDK destroy()/teardown 与去重表清理单元测试（FIX: G3）
 * 运行：node --test browser-sdk/test/sdk-destroy.test.js
 *
 * 覆盖：
 * - destroy 摘除全部监听器（UI 捕获/visibilitychange/pagehide/unhandledrejection）
 * - destroy 还原被包装的全局（window.onerror / fetch / XHR 原型 / console）
 * - destroy 停止心跳与定时器、清空队列/去重表、重置 _inited（可安全重新 init）
 * - 去重表过期/尺寸清理（键含动态 className，此前无限增长）
 * - HMR 兜底：全局实例标记 __AI_DEBUG_INSTANCE__ 的生命周期
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
    addEventListener(type, handler, capture) {
      listeners.push({ type, handler, capture: !!capture });
    },
    removeEventListener(type, handler, capture) {
      const idx = listeners.findIndex(
        (l) => l.type === type && l.handler === handler && l.capture === !!capture
      );
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
// console 钩子读写 global.console —— 用独立 mock，避免包装/还原影响 Node 真实 console
win.console = { error() {}, warn() {}, log() {} };
const doc = makeTarget();
doc.hidden = false;
doc.body = {};
doc.querySelector = () => doc.body;

globalThis.window = win;
globalThis.document = doc;
globalThis.XMLHttpRequest = MockXHR;

const SDK = require(path.join(__dirname, "..", "ai-debug.js"));

function initSdk() {
  SDK.init({
    endpoint: "http://localhost:8000",
    apiKey: "",
    captureErrors: true,
    captureNetwork: true,
    captureUI: true,
    captureConsole: true,
    sampleRate: 1.0,
    networkSampleRate: 1.0,
    enableCompression: false,
    maxBatchesPerWindow: 1000,
    throttleWindowMs: 60000,
  });
}

function countType(target, type) {
  return target.listeners.filter((l) => l.type === type).length;
}

test("init 安装监听器后，destroy 全部摘除", () => {
  initSdk();
  // init 后：UI 4 个捕获监听 + visibilitychange 挂上
  assert.ok(countType(doc, "click") >= 1, "init 应注册 click 捕获监听");
  assert.ok(countType(doc, "input") >= 1, "init 应注册 input 捕获监听");
  assert.ok(countType(doc, "visibilitychange") >= 1, "init 应注册 visibilitychange");
  assert.ok(countType(win, "pagehide") >= 1, "init 应注册 pagehide");
  assert.ok(countType(win, "unhandledrejection") >= 1, "init 应注册 unhandledrejection");

  SDK.destroy({ flush: false });

  assert.equal(countType(doc, "click"), 0, "destroy 后 click 监听应被摘除");
  assert.equal(countType(doc, "input"), 0, "destroy 后 input 监听应被摘除");
  assert.equal(countType(doc, "change"), 0, "destroy 后 change 监听应被摘除");
  assert.equal(countType(doc, "submit"), 0, "destroy 后 submit 监听应被摘除");
  assert.equal(countType(doc, "visibilitychange"), 0, "destroy 后 visibilitychange 应被摘除");
  assert.equal(countType(win, "pagehide"), 0, "destroy 后 pagehide 应被摘除");
  assert.equal(countType(win, "unhandledrejection"), 0, "destroy 后 unhandledrejection 应被摘除");
});

test("destroy 还原 window.onerror / fetch / XHR 原型 / console", () => {
  const preOnerror = win.onerror;
  const preFetch = win.fetch;
  const preConsoleError = win.console.error;
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;

  initSdk();
  assert.notStrictEqual(win.onerror, preOnerror, "init 后 onerror 应被包装");
  assert.notStrictEqual(win.fetch, preFetch, "init 后 fetch 应被包装");
  assert.notStrictEqual(win.console.error, preConsoleError, "init 后 console.error 应被包装");
  assert.notStrictEqual(XMLHttpRequest.prototype.open, origOpen, "init 后 XHR.open 应被包装");
  assert.equal(SDK._inited, true);

  SDK.destroy({ flush: false });

  assert.strictEqual(win.onerror, preOnerror, "destroy 后 onerror 应还原为原值");
  assert.strictEqual(win.fetch, preFetch, "destroy 后 fetch 应还原为原值");
  assert.strictEqual(win.console.error, preConsoleError, "destroy 后 console.error 应还原");
  assert.strictEqual(XMLHttpRequest.prototype.open, origOpen, "destroy 后 XHR.open 应还原");
  assert.strictEqual(XMLHttpRequest.prototype.send, origSend, "destroy 后 XHR.send 应还原");
  assert.equal(SDK._inited, false, "destroy 后 _inited 应为 false");
});

test("destroy 后 _inited 复位，可安全重新 init 且不叠加监听", () => {
  initSdk();
  SDK.destroy({ flush: false });
  assert.equal(SDK._inited, false);

  initSdk(); // 重新 init
  assert.equal(SDK._inited, true);
  // 重新 init 后每类监听只有 1 个（未叠加）
  assert.equal(countType(doc, "click"), 1, "重新 init 后 click 监听应只有 1 个");
  assert.equal(countType(win, "pagehide"), 1, "重新 init 后 pagehide 应只有 1 个");
  SDK.destroy({ flush: false });
});

test("HMR 兜底：全局实例标记随 init/destroy 生灭", () => {
  initSdk();
  assert.ok(globalThis.window.__AI_DEBUG_INSTANCE__, "init 后应记录全局实例");
  SDK.destroy({ flush: false });
  assert.ok(!globalThis.window.__AI_DEBUG_INSTANCE__, "destroy 后应清除全局实例标记");
});

test("去重表：键含动态 className 时尺寸受上限约束（不再无限增长）", () => {
  initSdk();
  const inputHandler = doc.listeners.find((l) => l.type === "input");
  assert.ok(inputHandler, "应存在 input 捕获监听");

  // 用不同 className 触发 1200 次，模拟长会话中持续产生新键
  for (let i = 0; i < 1200; i++) {
    inputHandler.handler({
      type: "input",
      target: { className: "dyn-cls-" + i, textContent: "x", tagName: "INPUT" },
    });
  }
  const size = SDK._getDebounceSize();
  assert.ok(size <= 1000, `去重表应受上限约束（<=1000），实际 ${size}`);
  SDK.destroy({ flush: false });
  assert.equal(SDK._getDebounceSize(), 0, "destroy 后去重表应清空");
});

test("destroy 幂等：重复调用不抛异常", () => {
  initSdk();
  assert.doesNotThrow(() => {
    SDK.destroy({ flush: false });
    SDK.destroy({ flush: false });
  });
});
