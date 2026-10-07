/**
 * _isSelfRequest 自请求排除范围 —— 同源采集回归测试（已转绿，作为回归守护常驻）。
 *
 * 历史（工作单 A，TDD 先红后绿，缺陷已修复）：ai-debug.js `_isSelfRequest` 旧实现按
 * 「与 endpoint 同 scheme+host 即排除（endpoint 带路径时按路径前缀排除）」整域判定，
 * 而 SDK 自身实际只会发出两类路径：
 *   - {endpoint去尾斜杠}/ingest/batch（_flushBatch / _drainPendingBatches /
 *     localStorage 恢复 / sendBeacon?token= 全部汇入此路径）
 *   - {endpoint去尾斜杠}/auth/beacon-token（_refreshBeaconToken）
 * 判定过宽 → 与 endpoint 同源的业务请求（demo 页 /api/debug/*、/mcp 等）被静默排除
 * 采集。已收窄为路径精确匹配（工作单 A 组测试守护该行为）。
 *
 * 契约（本文件锁定）：
 *   1. scheme/host 匹配 endpoint 且 pathname 恰为 <base>/ingest/batch 或
 *      <base>/auth/beacon-token → true（排除，防递归）；
 *   2. 其余同源路径（含裸 origin）→ false（采集）；
 *   3. base path 与 URL 生成处 `cfg.endpoint.replace(/\/+$/, "")` 同口径（尾斜杠归一）；
 *   4. query/hash 不参与匹配；pathname 匹配大小写敏感（host 按 URL 规范大小写不敏感）；
 *   5. 跨源与相似域仍拒绝（既有回归守护）。
 *
 * 工作单 C 扩展（方法匹配 + 真实 hook 路径）：SDK 自身两条上报固定 POST，自排除带
 * method 判定——GET 等非 POST 请求即使路径恰与上报路径相同也不排除（采集侧
 * fail-open，防路径撞车误杀业务请求）。工作单 C 组不只测纯函数，而是经实际包装后的
 * fetch / fetch(new Request(...)) / XHR open-send 链路验证：
 *   - 同源业务请求经包装 hook 被 onNetworkCapture 采集；
 *   - SDK 自身 /ingest/batch、/auth/beacon-token 上报经包装 hook 不触发业务网络采集
 *     （防递归；不排除则会形成"上报被再次采集"的自增强循环）；
 *   - 方法匹配在普通 fetch、fetch(Request)、XHR 三类调用路径上均生效。
 *
 * 运行：node --test browser-sdk/test/sdk-self-request-scope.test.js
 * （已列入 browser-sdk/package.json 的 npm test 与 .github/workflows/ci.yml
 *  SDK core contract tests 清单，两份清单保持一致。）
 * 套路对齐 sdk-minor-reuse.test.js / sdk-transport-fixes.test.js：node:test +
 * 每用例 freshSDK() 重新 require 隔离闭包、window 桩承接 IIFE 的 global 捕获。
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const MODULE_PATH = path.join(__dirname, "..", "ai-debug.js");

function freshSDK() {
  delete require.cache[require.resolve(MODULE_PATH)];
  return require(MODULE_PATH);
}

// ── 工作单 A-1：根 endpoint 下普通同源路径不判为自请求 ──────────────────

test("根 endpoint：同源业务路径不判为自请求（旧实现整域排除，应红）", () => {
  const SDK = freshSDK();
  SDK._setConfig("endpoint", "http://127.0.0.1:8000");
  const f = SDK._isSelfRequest;
  assert.equal(f("http://127.0.0.1:8000/api/orders"), false, "同源业务路径 /api/orders 应被采集");
  assert.equal(f("http://127.0.0.1:8000/api/debug/health"), false, "同源业务路径 /api/debug/health 应被采集");
});

test("根 endpoint：裸 origin 请求不判为自请求（行为变更断言，旧实现 true，应红）", () => {
  const SDK = freshSDK();
  SDK._setConfig("endpoint", "http://127.0.0.1:8000");
  const f = SDK._isSelfRequest;
  assert.equal(f("http://127.0.0.1:8000"), false, "裸 origin 页面请求不是 SDK 上报路径");
  assert.equal(f("http://127.0.0.1:8000/"), false, "根路径请求不是 SDK 上报路径");
});

// ── 工作单 A-2：SDK 上报路径仍排除（防递归回归守护，应保持绿） ──────────

test("根 endpoint：/ingest/batch 与 /auth/beacon-token 仍排除", () => {
  const SDK = freshSDK();
  SDK._setConfig("endpoint", "http://127.0.0.1:8000");
  const f = SDK._isSelfRequest;
  assert.equal(f("http://127.0.0.1:8000/ingest/batch"), true, "SDK 批量上报路径必须排除（防递归）");
  assert.equal(f("http://127.0.0.1:8000/auth/beacon-token"), true, "SDK 令牌换取路径必须排除");
});

test("根 endpoint：上报路径精确匹配而非前缀（/ingest/batch/extra 不排除，旧实现 true，应红）", () => {
  const SDK = freshSDK();
  SDK._setConfig("endpoint", "http://127.0.0.1:8000");
  const f = SDK._isSelfRequest;
  assert.equal(f("http://127.0.0.1:8000/ingest/batch/extra"), false, "精确匹配，深度路径不是上报路径");
  assert.equal(f("http://127.0.0.1:8000/auth/beacon-token/renew"), false, "精确匹配，子路径不是令牌路径");
});

// ── 工作单 A-3：带子路径 endpoint（http://host:port/lujo/） ─────────────

test("子路径 endpoint：/lujo/ingest/batch 与 /lujo/auth/beacon-token 排除", () => {
  const SDK = freshSDK();
  SDK._setConfig("endpoint", "http://127.0.0.1:8000/lujo/");
  const f = SDK._isSelfRequest;
  assert.equal(f("http://127.0.0.1:8000/lujo/ingest/batch"), true, "base path + 上报路径必须排除");
  assert.equal(f("http://127.0.0.1:8000/lujo/auth/beacon-token"), true, "base path + 令牌路径必须排除");
});

test("子路径 endpoint：/lujo/api/orders 采集（旧实现前缀整域排除，应红）", () => {
  const SDK = freshSDK();
  SDK._setConfig("endpoint", "http://127.0.0.1:8000/lujo/");
  const f = SDK._isSelfRequest;
  assert.equal(f("http://127.0.0.1:8000/lujo/api/orders"), false, "base path 下业务路径应被采集");
});

test("子路径 endpoint：/lujo/ingest2 不排除（旧实现前缀匹配误判 self，应红）", () => {
  const SDK = freshSDK();
  SDK._setConfig("endpoint", "http://127.0.0.1:8000/lujo/");
  const f = SDK._isSelfRequest;
  assert.equal(f("http://127.0.0.1:8000/lujo/ingest2"), false, "相似路径段必须精确区分");
});

test("子路径 endpoint：/lujoevil/ingest/batch 不排除（斜杠边界回归守护）", () => {
  const SDK = freshSDK();
  SDK._setConfig("endpoint", "http://127.0.0.1:8000/lujo/");
  const f = SDK._isSelfRequest;
  assert.equal(f("http://127.0.0.1:8000/lujoevil/ingest/batch"), false, "路径段边界必须按 / 划分");
});

test("子路径 endpoint：base 之外的同 host 上报路径不判为自请求", () => {
  const SDK = freshSDK();
  SDK._setConfig("endpoint", "http://127.0.0.1:8000/lujo/");
  const f = SDK._isSelfRequest;
  assert.equal(f("http://127.0.0.1:8000/ingest/batch"), false, "endpoint 配置在 /lujo 下，根上报路径不是本实例的");
});

// ── 工作单 A-4：尾斜杠归一化（与 URL 生成处 replace(/\/+$/,"") 同口径） ──

test("尾斜杠归一化：根 endpoint 尾斜杠数量不影响判定", () => {
  const f1 = freshSDK();
  f1._setConfig("endpoint", "http://127.0.0.1:8000/");
  assert.equal(f1._isSelfRequest("http://127.0.0.1:8000/ingest/batch"), true);
  assert.equal(f1._isSelfRequest("http://127.0.0.1:8000/api/orders"), false);

  const f3 = freshSDK();
  f3._setConfig("endpoint", "http://127.0.0.1:8000///");
  assert.equal(f3._isSelfRequest("http://127.0.0.1:8000/ingest/batch"), true);
  assert.equal(f3._isSelfRequest("http://127.0.0.1:8000/api/orders"), false);
});

test("尾斜杠归一化：子路径 endpoint 带或不带尾斜杠判定一致", () => {
  const bare = freshSDK();
  bare._setConfig("endpoint", "http://127.0.0.1:8000/lujo");
  assert.equal(bare._isSelfRequest("http://127.0.0.1:8000/lujo/ingest/batch"), true, "无尾斜杠 endpoint 的上报路径仍排除");
  assert.equal(bare._isSelfRequest("http://127.0.0.1:8000/lujo/api/orders"), false, "无尾斜杠 endpoint 的业务路径仍采集");

  const slashes = freshSDK();
  slashes._setConfig("endpoint", "http://127.0.0.1:8000/lujo///");
  assert.equal(slashes._isSelfRequest("http://127.0.0.1:8000/lujo/ingest/batch"), true, "多尾斜杠 endpoint 的上报路径仍排除");
  assert.equal(slashes._isSelfRequest("http://127.0.0.1:8000/lujo/api/orders"), false, "多尾斜杠 endpoint 的业务路径仍采集");
});

test("尾斜杠归一化：子路径前缀边界不受尾斜杠缺失影响（/lujoevil 不误判）", () => {
  const bare = freshSDK();
  bare._setConfig("endpoint", "http://127.0.0.1:8000/lujo");
  assert.equal(bare._isSelfRequest("http://127.0.0.1:8000/lujoevil/ingest/batch"), false, "无尾斜杠时相似前缀也不得误判");
});

// ── 工作单 A-5：beacon query 参数、query/hash 不参与匹配 ────────────────

test("beacon 场景：/ingest/batch?token=… 排除（query 不参与路径匹配）", () => {
  const SDK = freshSDK();
  SDK._setConfig("endpoint", "http://127.0.0.1:8000");
  const f = SDK._isSelfRequest;
  assert.equal(f("http://127.0.0.1:8000/ingest/batch?token=tk-1"), true, "sendBeacon URL 带 ?token= 仍须排除");
  assert.equal(f("http://127.0.0.1:8000/ingest/batch?token=tk-1&x=1"), true, "多 query 参数不改变判定");
  assert.equal(f("http://127.0.0.1:8000/auth/beacon-token?token=tk-1"), true, "令牌路径带 query 仍排除");
});

test("query/hash 不参与匹配：业务路径带 query/hash 仍采集（旧实现 true，应红）", () => {
  const SDK = freshSDK();
  SDK._setConfig("endpoint", "http://127.0.0.1:8000");
  const f = SDK._isSelfRequest;
  assert.equal(f("http://127.0.0.1:8000/api/orders?limit=1&offset=0"), false, "业务请求带 query 应被采集");
  assert.equal(f("http://127.0.0.1:8000/api/orders?next=/ingest/batch"), false, "query 值含上报路径字样不得误判");
  assert.equal(f("http://127.0.0.1:8000/api/orders#ingest/batch"), false, "hash 不参与路径匹配");
});

test("query/hash 不参与匹配：上报路径带 query/hash 仍排除", () => {
  const SDK = freshSDK();
  SDK._setConfig("endpoint", "http://127.0.0.1:8000");
  const f = SDK._isSelfRequest;
  assert.equal(f("http://127.0.0.1:8000/ingest/batch?token=tk-1#frag"), true, "query+hash 不改变上报路径判定");
});

// ── 工作单 A-6：大小写敏感（pathname 大小写敏感；host 按 URL 规范不敏感） ─

test("pathname 大小写敏感：/INGEST/BATCH、/Ingest/Batch 不排除（旧实现 true，应红）", () => {
  const SDK = freshSDK();
  SDK._setConfig("endpoint", "http://127.0.0.1:8000");
  const f = SDK._isSelfRequest;
  assert.equal(f("http://127.0.0.1:8000/INGEST/BATCH"), false, "路径匹配大小写敏感（URL 规范）");
  assert.equal(f("http://127.0.0.1:8000/Ingest/Batch"), false, "混合大小写路径不误判为上报路径");
  assert.equal(f("http://127.0.0.1:8000/lujo/INGEST/batch"), false, "子路径 endpoint 同样大小写敏感");
});

test("host 大小写不敏感：URL 规范化后 host 相等即比对成功（守护，应保持绿）", () => {
  const SDK = freshSDK();
  SDK._setConfig("endpoint", "http://localhost:8000");
  const f = SDK._isSelfRequest;
  assert.equal(f("http://LOCALHOST:8000/ingest/batch"), true, "host 按 URL 规范大小写不敏感");
  assert.equal(f("http://LOCALHOST:8000/api/orders"), false, "host 归一化不影响业务路径判定");
});

// ── 工作单 A-7：浏览器相对 URL（经 location.href 解析） ─────────────────

test("相对 URL：demo 场景下同源业务请求不判为自请求（旧实现 true，应红）", () => {
  const SDK = freshSDK();
  SDK._setConfig("endpoint", "http://127.0.0.1:8000");
  globalThis.location = { href: "http://127.0.0.1:8000/demo" };
  try {
    const f = SDK._isSelfRequest;
    assert.equal(f("/api/debug/health"), false, "XHR hook 收到相对 url，解析后是业务请求应采集");
    assert.equal(f("/ingest/batch"), true, "相对形式的上报路径仍排除");
    assert.equal(f("/ingest/batch?token=tk-1"), true, "相对形式 + beacon query 仍排除");
  } finally {
    delete globalThis.location;
  }
});

// ── 回归守护：跨源 / 相似域 / 空 / 畸形（既有行为保持，应保持绿） ────────

test("跨源与相似域仍拒绝（对齐 sdk-transport-fixes.test.js 既有守护）", () => {
  const SDK = freshSDK();
  SDK._setConfig("endpoint", "http://localhost:8000");
  const f = SDK._isSelfRequest;
  assert.equal(f("http://localhost:8000.evil.com/x"), false, "相似域名（前缀绕过）仍拒绝");
  assert.equal(f("http://evil.com/ingest/batch"), false, "跨 host 的上报路径字样仍拒绝");
  assert.equal(f("https://localhost:8000/ingest/batch"), false, "scheme 不同仍拒绝");
  assert.equal(f("http://localhost:9999/ingest/batch"), false, "port 不同仍拒绝");
});

test("空 / 畸形 URL 与未配置 endpoint 返回 false（fail-open 到采集侧无副作用）", () => {
  const SDK = freshSDK();
  SDK._setConfig("endpoint", "http://127.0.0.1:8000");
  const f = SDK._isSelfRequest;
  assert.equal(f(""), false);
  assert.equal(f(null), false);
  assert.equal(f(undefined), false);
  assert.equal(f("not a url ::"), false);

  const noEndpoint = freshSDK(); // 不设置 endpoint
  assert.equal(noEndpoint._isSelfRequest("http://127.0.0.1:8000/ingest/batch"), false, "endpoint 未配置时不判自请求");
});

// ══ 工作单 C：方法匹配 + 真实 hook 路径 ═══════════════════════════════════
//
// Node 无 window/document/XMLHttpRequest 等 DOM 实现。与 sdk-minor-reuse.test.js
// 同手法：IIFE 以 (typeof window !== "undefined" ? window : this) 捕获 global，
// 先装 window 桩再 require，SDK 的 fetch/XHR 钩子落到桩上——测试驱动的是
// 「实际包装后的」fetch 与 XMLHttpRequest.prototype.open/send，而非纯函数。

function makeTarget() {
  const listeners = [];
  return {
    listeners,
    addEventListener(type, handler) { listeners.push({ type, handler }); },
    removeEventListener(type, handler) {
      const idx = listeners.findIndex((l) => l.type === type && l.handler === handler);
      if (idx >= 0) listeners.splice(idx, 1);
    },
  };
}

// 带事件监听的 MockXHR：SDK 的 send 包装经 addEventListener("load") 等挂监听，
// 测试手动 fire("load") 模拟请求完成（同 sdk-minor-reuse.test.js 手法）。
// instances 同时收集 SDK 自身上报 XHR 与业务 XHR，供断言「上报确实经过包装 hook」。
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
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener(type, fn) {
    const list = this.listeners[type];
    if (!list) return;
    const i = list.indexOf(fn);
    if (i >= 0) list.splice(i, 1);
  }
  fire(type) { for (const fn of (this.listeners[type] || []).slice()) fn(); }
}
EventedMockXHR.instances = [];
EventedMockXHR.reset = function () { EventedMockXHR.instances = []; };

const localStorageStore = {};
const localStorageStub = {
  getItem: (k) => (k in localStorageStore ? localStorageStore[k] : null),
  setItem: (k, v) => { localStorageStore[k] = String(v); },
  removeItem: (k) => { delete localStorageStore[k]; },
};
function clearLocalStorage() { for (const k in localStorageStore) delete localStorageStore[k]; }

// 每个用例独立 win：避免上一个 SDK 实例包装的 fetch 残留在共享桩上。
// win.fetch 即 SDK 捕获的 _origFetch（mock，返回带 clone().text() 的假 Response），
// init 后 win.fetch 被替换为包装版——测试调用的是真实包装链路。
function makeWin() {
  const win = makeTarget();
  win.console = { error() {}, warn() {}, log() {} };
  win.fetch = function () {
    return Promise.resolve({
      status: 200,
      clone() { return { text: () => Promise.resolve('{"ok":true}') }; },
    });
  };
  return win;
}

// 安装全局桩 + 全新 SDK（require 前必须 window 桩就位，IIFE 闭包才指向它）
function freshHookEnv(extraOpts) {
  const win = makeWin();
  globalThis.window = win;
  globalThis.localStorage = localStorageStub;
  globalThis.XMLHttpRequest = EventedMockXHR;
  EventedMockXHR.reset();
  clearLocalStorage();
  try {
    Object.defineProperty(globalThis, "navigator", {
      value: { userAgent: "node-test" },
      configurable: true,
    });
  } catch (e) { /* Node 21+ 原生 navigator 已存在，直接沿用 */ }
  const SDK = freshSDK();
  const captures = [];
  SDK.init(Object.assign({
    endpoint: "http://localhost:8000",
    captureUI: false,
    captureConsole: false,
    autoDetectUISilentFailures: false,
    sampleRate: 1,
    networkSampleRate: 1,   // 采样全通过：本组断言的是"是否进入采集链路"，与采样无关
    networkThrottleMs: 0,   // 不节流：排除节流对记录数的干扰
    batchSize: 1,           // 每条事件立即成批，便于检查 SDK 自身上报 XHR 是否发出
    maxRetries: 0,
    enableCompression: false,
    enableLocalStorageFallback: false,
    maxBatchesPerWindow: 1000,
    throttleWindowMs: 60000,
  }, extraOpts || {}));
  SDK.onNetworkCapture(function (record) { captures.push(record); });
  return { SDK, win, captures };
}

// 从全部 mock XHR 实例的请求体中抽取 /ingest/network 事件（SDK 网络采集的上报形态）。
// 若发生递归（SDK 上报被再次采集），会出现 payload.record.url 指向上报路径的事件。
function collectIngestNetworkEvents() {
  const events = [];
  for (const x of EventedMockXHR.instances) {
    if (!x.body || typeof x.body !== "string") continue;
    let parsed;
    try { parsed = JSON.parse(x.body); } catch (e) { continue; }
    for (const ev of (parsed.events || [])) events.push(ev);
  }
  return events.filter((e) => e.path === "/ingest/network");
}

async function settle(ms) { await new Promise((r) => setTimeout(r, ms || 30)); }

// ── 工作单 C-0：纯函数层 method 匹配契约 ──────────────────────────────────

test("C-0 纯函数：method=POST 排除，非 POST 不排除，缺省按路径判定（兼容）", () => {
  const SDK = freshSDK();
  SDK._setConfig("endpoint", "http://127.0.0.1:8000");
  const f = SDK._isSelfRequest;
  // SDK 自身两条上报路径固定 POST（_refreshBeaconToken / _sendBatchXhr /
  // _sendBatchSync / _sendBatchXhrCompressed 均 open("POST", ...)，sendBeacon 同 POST 语义）
  assert.equal(f("http://127.0.0.1:8000/ingest/batch", "POST"), true, "POST + 上报路径 → 排除");
  assert.equal(f("http://127.0.0.1:8000/ingest/batch", "post"), true, "method 大小写归一后匹配");
  assert.equal(f("http://127.0.0.1:8000/auth/beacon-token", "POST"), true, "POST + 令牌路径 → 排除");
  // 非 POST 同路径：业务方对同路径的 GET（如代理健康检查）不得误排除（采集侧 fail-open）
  assert.equal(f("http://127.0.0.1:8000/ingest/batch", "GET"), false, "GET 同路径 → 采集");
  assert.equal(f("http://127.0.0.1:8000/ingest/batch", "DELETE"), false, "DELETE 同路径 → 采集");
  assert.equal(f("http://127.0.0.1:8000/auth/beacon-token", "GET"), false, "GET 同路径 → 采集");
  // method 缺省（纯函数调用方兼容）：仅按路径判定，工作单 A 行为不变
  assert.equal(f("http://127.0.0.1:8000/ingest/batch"), true, "缺省 method → 按路径排除（A 组兼容）");
  assert.equal(f("http://127.0.0.1:8000/ingest/batch", ""), true, "空 method 视为未知 → 按路径判定");
  // 业务路径不因 method 而变化
  assert.equal(f("http://127.0.0.1:8000/api/orders", "POST"), false, "业务路径 → 采集");
  assert.equal(f("http://127.0.0.1:8000/api/orders", "GET"), false, "业务路径 → 采集");
});

// ── 工作单 C-1：真实 fetch hook 采集同源业务请求 ──────────────────────────

test("C-1 真实 fetch hook：同源业务请求被采集，onNetworkCapture 收到完整记录", async () => {
  const { SDK, win, captures } = freshHookEnv();
  try {
    assert.notStrictEqual(win.fetch, null, "init 后 fetch 应被包装");
    await win.fetch("http://localhost:8000/api/orders", { method: "GET" });
    await settle();

    assert.equal(captures.length, 1, "同源业务 fetch 必须恰好产生 1 条采集记录");
    assert.equal(captures[0].url, "http://localhost:8000/api/orders");
    assert.equal(captures[0].method, "GET");
    assert.equal(captures[0].status_code, 200);
  } finally {
    SDK.destroy({ flush: false });
  }
});

// ── 工作单 C-2/C-3：SDK 自身上报经实际包装 hook 不触发业务网络采集 ─────────

test("C-2 真实 XHR hook：SDK 自身 /ingest/batch 上报经包装 hook 不触发采集（防递归）", async () => {
  const { SDK, captures } = freshHookEnv();
  try {
    SDK.reportError(new Error("self-report-probe"));
    await settle(50);

    // 前置：SDK 上报确实发生了，且这条 XHR 走的是包装后的 open/send（否则断言无意义）
    const batchXhr = EventedMockXHR.instances.find(
      (x) => x.url === "http://localhost:8000/ingest/batch" && x.method === "POST");
    assert.ok(batchXhr, "SDK 批量上报 XHR 必须真实发出且经包装后的 open（method=POST）");
    assert.equal(batchXhr._aiDebugSkip, true, "上报 XHR 在包装 open 中被标记 skip");

    // 核心断言：自排除生效——上报本身不得触发业务网络采集（否则递归）
    assert.equal(captures.length, 0, "SDK 自身 /ingest/batch 上报不得触发 onNetworkCapture");
    const netEvents = collectIngestNetworkEvents();
    assert.equal(netEvents.length, 0, "不得产生任何 /ingest/network 事件（递归即自增强循环）");
  } finally {
    SDK.destroy({ flush: false });
  }
});

test("C-3 真实 XHR hook：SDK 自身 /auth/beacon-token 上报经包装 hook 不触发采集", async () => {
  const { SDK, captures } = freshHookEnv({ apiKey: "test-key" });
  try {
    // init 内 _refreshBeaconToken 同步发出令牌换取 XHR（经包装后的 open/send）
    await settle();
    const tokenXhr = EventedMockXHR.instances.find(
      (x) => x.url === "http://localhost:8000/auth/beacon-token");
    assert.ok(tokenXhr, "beacon 令牌换取 XHR 必须真实发出（经包装后的 open）");
    assert.equal(tokenXhr.method, "POST");
    assert.equal(tokenXhr._aiDebugSkip, true, "令牌 XHR 在包装 open 中被标记 skip");

    assert.equal(captures.length, 0, "SDK 自身 /auth/beacon-token 上报不得触发 onNetworkCapture");
    const netEvents = collectIngestNetworkEvents();
    assert.equal(netEvents.length, 0, "不得产生任何 /ingest/network 事件");
  } finally {
    SDK.destroy({ flush: false });
  }
});

// ── 工作单 C-4/C-5/C-6：方法匹配在三类 hook 调用路径上生效 ─────────────────

test("C-4 普通 fetch：GET 同路径采集、POST 同路径排除（方法匹配，旧实现 GET 也被排除应红）", async () => {
  const { SDK, win, captures } = freshHookEnv();
  try {
    // 业务方对 /ingest/batch 的 GET（如反代健康检查打到同路径）——非 SDK 上报，应采集
    await win.fetch("http://localhost:8000/ingest/batch"); // fetch 默认 GET
    await settle();
    assert.equal(captures.length, 1, "GET /ingest/batch 是业务请求，必须被采集");
    assert.equal(captures[0].method, "GET", "记录 method 应为 GET（fetch 默认）");
    assert.equal(captures[0].url, "http://localhost:8000/ingest/batch");

    // SDK 形态的 POST 同路径——排除（防递归），不得新增采集
    await win.fetch("http://localhost:8000/ingest/batch", { method: "POST" });
    await settle();
    assert.equal(captures.length, 1, "POST /ingest/batch 是 SDK 上报形态，必须排除");
  } finally {
    SDK.destroy({ flush: false });
  }
});

test("C-5 fetch(new Request(...))：method 从 Request 对象读取，GET 采集 / POST 排除", async () => {
  const { SDK, win, captures } = freshHookEnv();
  try {
    // Request 对象携带 method（args[1] 为 undefined 的调用形态，v0.7.1-b7-1 修复路径）
    await win.fetch(new Request("http://localhost:8000/ingest/batch", { method: "GET" }));
    await settle();
    assert.equal(captures.length, 1, "Request(GET) 同路径是业务请求，必须被采集");
    assert.equal(captures[0].method, "GET", "method 必须从 Request 对象正确读取");
    assert.equal(captures[0].url, "http://localhost:8000/ingest/batch");

    await win.fetch(new Request("http://localhost:8000/ingest/batch", { method: "POST" }));
    await settle();
    assert.equal(captures.length, 1, "Request(POST) 同路径是 SDK 上报形态，必须排除");

    // 守护 v0.7.1-b7-1：Request 对象的 method 提取前移后仍正确（POST 业务请求记 POST）
    await win.fetch(new Request("http://localhost:8000/api/orders", { method: "POST" }));
    await settle();
    assert.equal(captures.length, 2, "Request(POST) 业务请求应被采集");
    assert.equal(captures[1].method, "POST", "业务 POST 不得被误记为 GET");
    assert.equal(captures[1].url, "http://localhost:8000/api/orders");
  } finally {
    SDK.destroy({ flush: false });
  }
});

test("C-6 XHR open-send：GET 同路径采集、POST 同路径排除（方法经 open 参数读取）", async () => {
  const { SDK, captures } = freshHookEnv();
  try {
    const xhrGet = new XMLHttpRequest();
    xhrGet.open("GET", "http://localhost:8000/ingest/batch");
    xhrGet.send();
    xhrGet.fire("load");
    await settle();
    assert.equal(captures.length, 1, "XHR GET 同路径是业务请求，必须被采集");
    assert.equal(captures[0].method, "GET", "method 必须从 open 第一个参数正确读取");
    assert.equal(captures[0].url, "http://localhost:8000/ingest/batch");

    const xhrPost = new XMLHttpRequest();
    xhrPost.open("POST", "http://localhost:8000/ingest/batch");
    xhrPost.send();
    xhrPost.fire("load");
    await settle();
    assert.equal(captures.length, 1, "XHR POST 同路径是 SDK 上报形态，必须排除");

    // 令牌路径同理：GET 采集（业务），POST 排除（SDK）
    const xhrToken = new XMLHttpRequest();
    xhrToken.open("GET", "http://localhost:8000/auth/beacon-token");
    xhrToken.send();
    xhrToken.fire("load");
    await settle();
    assert.equal(captures.length, 2, "XHR GET /auth/beacon-token 是业务请求，必须被采集");
    assert.equal(captures[2 - 1].url, "http://localhost:8000/auth/beacon-token");
  } finally {
    SDK.destroy({ flush: false });
  }
});
