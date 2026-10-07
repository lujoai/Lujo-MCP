/**
 * endpoint 合法性校验单测（W15 / P3-SDK-2 + query/fragment 收口）。
 *
 * 缺陷复现口径：
 *   - W15 / P3-SDK-2：init() 只判空不判格式，`endpoint: "localhost:8000"`（漏 scheme）
 *     会通过校验并装完钩子；此后所有上报 URL 是**相对地址**，被浏览器按页面 origin
 *     解析 → 现场数据被静默 POST 到用户自己的业务服务器。
 *   - query/fragment 收口：`_isEndpointUsable` 不拒绝带 `?`/`#` 的 endpoint。
 *     SDK 发送 URL 拼接方式 `cfg.endpoint.replace(/\/+$/, "") + "/ingest/batch"`
 *     会让上报路径进入 query/fragment 而非 pathname，导致投递地址错误且
 *     `_isSelfRequest` pathname 精确匹配失败（SDK 自身 POST 被再次采集形成递归）。
 *
 * 关键技术事实：`new URL('http://h/p?')` 和 `new URL('http://h/p#')` 解析后
 * `search`/`hash` 均为空字符串——URL 构造器丢弃空的 `?`/`#`。
 * 因此必须检查**原始字符串**中的 `?`/`#`，不能依赖 `URL.search`/`URL.hash`。
 *
 * 浏览器 SDK 的修法与 Node SDK 不同口径：SDK 注入宿主页面，init() 抛异常会
 * 直接打断宿主脚本，因此保持既有「告警 + 拒绝初始化」的失败安全语义
 * （与空 endpoint 同路径），只在装钩子**之前**把格式判掉。
 *
 * 本文件独立成篇：behavioural 用例会真正调用 init()，单独进程可把副作用
 * （钩子安装 / console 包装 / 定时器）限制在文件内。
 * 测试桩和全局变量必须可靠还原，不污染同文件后续用例。
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

// ══ 纯函数判定表（无副作用） ════════════════════════════════════════════

test("_isEndpointUsable：只接受 http(s) 绝对地址，不含 query/fragment", () => {
  const SDK = freshSDK();
  assert.equal(typeof SDK._isEndpointUsable, "function", "缺少 _isEndpointUsable 判定函数");

  // 合法
  assert.equal(SDK._isEndpointUsable("http://127.0.0.1:8000"), true);
  assert.equal(SDK._isEndpointUsable("https://lujo.example.com"), true);
  assert.equal(SDK._isEndpointUsable("http://localhost:8000/base/path"), true);
  assert.equal(SDK._isEndpointUsable("http://localhost:8000/"), true);
  // 编码字符 %3F(%3F) / %23(#) 在路径中是合法的——它们不是裸 ? 或 #
  assert.equal(SDK._isEndpointUsable("http://localhost:8000/base/%3Fquery/%23fragment"), true);

  // 非法：漏 scheme（本缺陷的主形态，会被当相对地址解析）
  assert.equal(SDK._isEndpointUsable("localhost:8000"), false);
  assert.equal(SDK._isEndpointUsable("127.0.0.1:8000"), false);
  assert.equal(SDK._isEndpointUsable("/ingest"), false);

  // 非法：非 http(s) scheme
  assert.equal(SDK._isEndpointUsable("ftp://example.com"), false);
  assert.equal(SDK._isEndpointUsable("file:///tmp/x"), false);
  assert.equal(SDK._isEndpointUsable("ws://127.0.0.1:8000"), false);

  // 非法：endpoint 自身带 query（非空 / 空）
  assert.equal(SDK._isEndpointUsable("http://localhost:8000/lujo?x=1"), false);
  assert.equal(SDK._isEndpointUsable("http://localhost:8000/lujo?"), false);
  assert.equal(SDK._isEndpointUsable("http://localhost:8000?api_key=secret"), false);
  assert.equal(SDK._isEndpointUsable("http://localhost:8000?"), false);

  // 非法：endpoint 自身带 fragment（非空 / 空）
  assert.equal(SDK._isEndpointUsable("http://localhost:8000/lujo#x"), false);
  assert.equal(SDK._isEndpointUsable("http://localhost:8000/lujo#"), false);
  assert.equal(SDK._isEndpointUsable("http://localhost:8000#section"), false);
  assert.equal(SDK._isEndpointUsable("http://localhost:8000#"), false);

  // 非法：空 / 非字符串 / 畸形
  assert.equal(SDK._isEndpointUsable(""), false);
  assert.equal(SDK._isEndpointUsable(null), false);
  assert.equal(SDK._isEndpointUsable(undefined), false);
  assert.equal(SDK._isEndpointUsable(8000), false);
  assert.equal(SDK._isEndpointUsable({}), false);
  assert.equal(SDK._isEndpointUsable("http://"), false);
});

// ══ 行为：非法 endpoint 必须拒绝初始化（先于装钩子） ══════════════════════

// 浏览器环境桩：init() 在校验通过后会安装全局监听器，Node 环境下会抛异常。
// 校验生效时 init 不会走到安装阶段，但测试需要确保即使环境不完整也不会
// 因 DOM 缺失而混淆断言。
function makeBrowserStub() {
  const listeners = [];
  const win = {
    console: { error() {}, warn() {}, log() {} },
    fetch: function origFetch() {
      return Promise.resolve({ status: 200, clone() { return { text: () => Promise.resolve("") }; } });
    },
    addEventListener(type, fn) { listeners.push({ type, fn }); },
    removeEventListener(type, fn) {
      const i = listeners.findIndex((l) => l.type === type && l.fn === fn);
      if (i >= 0) listeners.splice(i, 1);
    },
  };
  win.document = {
    hidden: false,
    addEventListener(type, fn) { listeners.push({ type, fn, target: "document" }); },
    removeEventListener(type, fn) {
      const i = listeners.findIndex((l) => l.type === type && l.fn === fn && l.target === "document");
      if (i >= 0) listeners.splice(i, 1);
    },
    querySelector() { return null; },
    body: null,
  };
  return win;
}

function withBrowserEnv(fn) {
  const origWindow = globalThis.window;
  const origDocument = globalThis.document;
  const origFetch = globalThis.fetch;
  const origXhr = globalThis.XMLHttpRequest;
  const origAddEventListener = globalThis.addEventListener;
  const origRemoveEventListener = globalThis.removeEventListener;
  // Node 22 navigator 是只读属性——尝试 defineProperty，失败则沿用原生
  const origNavigatorDesc = Object.getOwnPropertyDescriptor(globalThis, "navigator");

  const win = makeBrowserStub();
  globalThis.window = win;
  globalThis.document = win.document;
  globalThis.fetch = win.fetch;
  globalThis.XMLHttpRequest = function MockXHR() {
    this.open = function() {};
    this.send = function() {};
    this.setRequestHeader = function() {};
    this.addEventListener = function() {};
    this.removeEventListener = function() {};
  };
  globalThis.addEventListener = win.addEventListener;
  globalThis.removeEventListener = win.removeEventListener;
  try {
    Object.defineProperty(globalThis, "navigator", {
      value: { userAgent: "node-test" },
      configurable: true,
      writable: true,
    });
  } catch (e) { /* Node 22+ navigator 已存在且不可配置，直接沿用 */ }

  try {
    return fn();
  } finally {
    globalThis.window = origWindow;
    globalThis.document = origDocument;
    globalThis.fetch = origFetch;
    globalThis.XMLHttpRequest = origXhr;
    globalThis.addEventListener = origAddEventListener;
    globalThis.removeEventListener = origRemoveEventListener;
    try {
      if (origNavigatorDesc) {
        Object.defineProperty(globalThis, "navigator", origNavigatorDesc);
      } else {
        delete globalThis.navigator;
      }
    } catch (e) { /* navigator 不可配置时无法还原，不影响测试 */ }
  }
}

test("init：漏 scheme endpoint 拒绝初始化并告警，不抛异常", () => {
  withBrowserEnv(() => {
    const SDK = freshSDK();
    const warnings = [];
    const origWarn = console.warn;
    const origLog = console.log;
    console.warn = function () { warnings.push(Array.prototype.join.call(arguments, " ")); };
    console.log = function () {};

    let initError = null;
    let initedAfter = null;
    try {
      SDK.init({ endpoint: "localhost:8000" });
      initedAfter = SDK._inited;
    } catch (e) {
      initError = e;
      initedAfter = SDK._inited;
    } finally {
      try { SDK.destroy({ flush: false }); } catch (_) {}
      console.warn = origWarn;
      console.log = origLog;
    }

    assert.equal(initError, null, "校验必须先于钩子安装，不得抛异常：" + (initError && initError.message));
    assert.equal(initedAfter, false, "非法 endpoint 必须拒绝初始化");
    assert.equal(warnings.length, 1, "必须恰好一条告警，实际：" + JSON.stringify(warnings));
    assert.match(warnings[0], /\[ai-debug\]/);
    assert.match(warnings[0], /endpoint/);
    assert.equal(warnings[0].includes("localhost:8000"), false, "告警不得回显 endpoint 原文");
  });
});

test("init：空 endpoint 仍走既有拒绝路径（回归护栏）", () => {
  withBrowserEnv(() => {
    const SDK = freshSDK();
    const warnings = [];
    const origWarn = console.warn;
    console.warn = function () { warnings.push(Array.prototype.join.call(arguments, " ")); };
    let initedAfter = null;
    try {
      SDK.init({ endpoint: "" });
      initedAfter = SDK._inited;
    } finally {
      console.warn = origWarn;
    }
    assert.equal(initedAfter, false);
    assert.equal(warnings.length, 1);
    assert.match(warnings[0], /endpoint/);
  });
});

// ══ query/fragment endpoint 初始化行为 ═══════════════════════════════════

test("init：非空 query endpoint 拒绝初始化，不抛异常 / 不装钩子 / 不发请求", () => {
  const requests = [];
  withBrowserEnv(() => {
    const SDK = freshSDK();
    const warnings = [];
    const origWarn = console.warn;
    const origLog = console.log;
    console.warn = function () { warnings.push(Array.prototype.join.call(arguments, " ")); };
    console.log = function () {};

    // 用 FakeXHR 拦截——如果 init 穿过校验就会发出 beacon-token 请求
    const origXhr = globalThis.XMLHttpRequest;
    globalThis.XMLHttpRequest = class FakeXHR {
      open(method, url) { requests.push({ method, url }); }
      setRequestHeader() {}
      send(body) { requests.push({ body }); }
    };

    // captureNetwork 保持默认 true：验证 hooks 未被安装
    const origFetch = globalThis.fetch;
    let initError = null;
    let initedAfter = null;
    try {
      SDK.init({
        endpoint: "http://localhost:8710/lujo?api_key=endpoint-secret",
        apiKey: "header-secret",
      });
      initedAfter = SDK._inited;
    } catch (e) {
      initError = e;
      initedAfter = SDK._inited;
    } finally {
      try { SDK.destroy({ flush: false }); } catch (_) {}
      console.warn = origWarn;
      console.log = origLog;
      globalThis.XMLHttpRequest = origXhr;
    }

    assert.equal(initError, null, "非法 endpoint 必须告警拒绝，不得抛异常");
    assert.equal(initedAfter, false, "query endpoint 必须在安装 hooks 前拒绝");
    assert.equal(warnings.length, 1, "必须恰好一条通用告警");
    assert.match(warnings[0], /endpoint/);
    assert.equal(warnings[0].includes("endpoint-secret"), false, "告警不得回显 query 内容");
    assert.equal(warnings[0].includes("header-secret"), false, "告警不得回显 apiKey");
    assert.deepEqual(requests, [], "拒绝初始化后不得发送任何 beacon-token 请求");
    // captureNetwork 默认 true，但 hooks 不应被安装——fetch 应保持原始值
    assert.equal(globalThis.fetch, origFetch, "非法 endpoint 不得替换 fetch");
  });
});

test("init：空 query endpoint (?) 拒绝初始化", () => {
  withBrowserEnv(() => {
    const SDK = freshSDK();
    const warnings = [];
    const origWarn = console.warn;
    const origLog = console.log;
    console.warn = function () { warnings.push(Array.prototype.join.call(arguments, " ")); };
    console.log = function () {};

    let initError = null;
    let initedAfter = null;
    try {
      SDK.init({ endpoint: "http://localhost:8710/lujo?" });
      initedAfter = SDK._inited;
    } catch (e) {
      initError = e;
      initedAfter = SDK._inited;
    } finally {
      try { SDK.destroy({ flush: false }); } catch (_) {}
      console.warn = origWarn;
      console.log = origLog;
    }

    assert.equal(initError, null, "空 query endpoint 必须告警拒绝，不得抛异常");
    assert.equal(initedAfter, false, "空 ? endpoint 必须拒绝初始化");
    assert.equal(warnings.length, 1, "必须恰好一条告警");
    assert.match(warnings[0], /endpoint/);
  });
});

test("init：非空 fragment endpoint 拒绝初始化", () => {
  withBrowserEnv(() => {
    const SDK = freshSDK();
    const warnings = [];
    const origWarn = console.warn;
    const origLog = console.log;
    console.warn = function () { warnings.push(Array.prototype.join.call(arguments, " ")); };
    console.log = function () {};

    let initError = null;
    let initedAfter = null;
    try {
      SDK.init({ endpoint: "http://localhost:8710/lujo#section" });
      initedAfter = SDK._inited;
    } catch (e) {
      initError = e;
      initedAfter = SDK._inited;
    } finally {
      try { SDK.destroy({ flush: false }); } catch (_) {}
      console.warn = origWarn;
      console.log = origLog;
    }

    assert.equal(initError, null, "fragment endpoint 必须告警拒绝，不得抛异常");
    assert.equal(initedAfter, false, "fragment endpoint 必须拒绝初始化");
    assert.equal(warnings.length, 1, "必须恰好一条告警");
    assert.match(warnings[0], /endpoint/);
  });
});

test("init：空 fragment endpoint (#) 拒绝初始化", () => {
  withBrowserEnv(() => {
    const SDK = freshSDK();
    const warnings = [];
    const origWarn = console.warn;
    const origLog = console.log;
    console.warn = function () { warnings.push(Array.prototype.join.call(arguments, " ")); };
    console.log = function () {};

    let initError = null;
    let initedAfter = null;
    try {
      SDK.init({ endpoint: "http://localhost:8710/lujo#" });
      initedAfter = SDK._inited;
    } catch (e) {
      initError = e;
      initedAfter = SDK._inited;
    } finally {
      try { SDK.destroy({ flush: false }); } catch (_) {}
      console.warn = origWarn;
      console.log = origLog;
    }

    assert.equal(initError, null, "空 fragment endpoint 必须告警拒绝，不得抛异常");
    assert.equal(initedAfter, false, "空 # endpoint 必须拒绝初始化");
    assert.equal(warnings.length, 1, "必须恰好一条告警");
    assert.match(warnings[0], /endpoint/);
  });
});

// ══ 非法 endpoint 下 fetch/XHR 原始函数未被替换 ═══════════════════════════

test("init：非法 endpoint 下 captureNetwork 默认开启时 fetch/XHR 原始函数未被替换", () => {
  withBrowserEnv(() => {
    const SDK = freshSDK();
    const origWarn = console.warn;
    const origLog = console.log;
    console.warn = function () {};
    console.log = function () {};

    const origFetch = globalThis.fetch;
    const origXhrOpen = XMLHttpRequest.prototype.open;
    const origXhrSend = XMLHttpRequest.prototype.send;

    // 在 init 前后都检查：非法 endpoint 不得安装 hooks，fetch/XHR 必须保持原始值
    // captureNetwork 默认 true——不通过 captureNetwork=false 代替验证
    let fetchReplacedDuringInit = false;
    let xhrOpenReplacedDuringInit = false;
    try {
      SDK.init({
        endpoint: "http://localhost:8710/lujo?token=leaked",
      });
      fetchReplacedDuringInit = (globalThis.fetch !== origFetch);
      xhrOpenReplacedDuringInit = (XMLHttpRequest.prototype.open !== origXhrOpen);
    } finally {
      try { SDK.destroy({ flush: false }); } catch (_) {}
      console.warn = origWarn;
      console.log = origLog;
    }

    assert.equal(fetchReplacedDuringInit, false, "init 后 fetch 不得被替换");
    assert.equal(xhrOpenReplacedDuringInit, false, "init 后 XHR.open 不得被替换");
    // destroy 后也必须保持原始值（不被替换也不被残留包装器污染）
    assert.equal(globalThis.fetch, origFetch, "destroy 后 fetch 必须还原");
    assert.equal(XMLHttpRequest.prototype.open, origXhrOpen, "destroy 后 XHR.open 必须还原");
    assert.equal(XMLHttpRequest.prototype.send, origXhrSend, "destroy 后 XHR.send 必须还原");
  });
});

// ══ 正常 endpoint 不回归 ══════════════════════════════════════════════════

test("init：正常 endpoint（origin / 子路径 / 尾斜杠 / 编码路径）继续生效", () => {
  const validEndpoints = [
    "http://127.0.0.1:8710",
    "http://127.0.0.1:8710/",
    "http://127.0.0.1:8710/lujo",
    "http://127.0.0.1:8710/lujo/",
    "http://127.0.0.1:8710/base/%3Fquery/%23fragment",
  ];

  for (const ep of validEndpoints) {
    withBrowserEnv(() => {
      const SDK = freshSDK();
      const origWarn = console.warn;
      const origLog = console.log;
      const warnings = [];
      console.warn = function () { warnings.push(Array.prototype.join.call(arguments, " ")); };
      console.log = function () {};

      try {
        SDK.init({ endpoint: ep, captureUI: false, captureConsole: false });
        assert.equal(SDK._inited, true, "合法 endpoint 应初始化成功: " + ep);
        assert.equal(warnings.length, 0, "合法 endpoint 不应有告警: " + ep);
      } finally {
        try { SDK.destroy({ flush: false }); } catch (_) {}
        console.warn = origWarn;
        console.log = origLog;
      }
    });
  }
});

test("init：合法 endpoint 下上报 URL 的 query 参数继续正确自排除", () => {
  withBrowserEnv(() => {
    const SDK = freshSDK();
    SDK._setConfig("endpoint", "http://127.0.0.1:8710");
    // sendBeacon 的 ?token= 等 query 参数不影响自排除判定
    assert.equal(SDK._isSelfRequest("http://127.0.0.1:8710/ingest/batch?token=tk-1"), true, "上报路径带 query 仍排除");
    assert.equal(SDK._isSelfRequest("http://127.0.0.1:8710/ingest/batch?token=tk-1&x=1"), true, "多 query 参数不改变判定");
    assert.equal(SDK._isSelfRequest("http://127.0.0.1:8710/auth/beacon-token?token=tk-1"), true, "令牌路径带 query 仍排除");
    // 业务路径带 query 仍采集
    assert.equal(SDK._isSelfRequest("http://127.0.0.1:8710/api/orders?limit=1"), false, "业务请求带 query 应被采集");
  });
});
