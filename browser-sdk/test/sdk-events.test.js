/**
 * Browser SDK 事件上报、队列缓冲与敏感字段脱敏单元测试
 * 运行：node --test browser-sdk/test/sdk-events.test.js
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const SDK = require(path.join(__dirname, "..", "ai-debug.js"));

test("reportError: 正常捕获错误并生成结构体", () => {
  assert.doesNotThrow(() => {
    SDK.reportError(new Error("test error"), { extraInfo: "detail" });
  });
});

test("reportNetworkError: 正常记录网络错误", () => {
  assert.doesNotThrow(() => {
    SDK.reportNetworkError({
      method: "POST",
      url: "http://example.com/api",
      status: 500,
      duration_ms: 120,
      request_body: { token: "secret_123", name: "test" },
    });
  });
});

test("reportSilentFailure: 支持附带上下文上报", () => {
  assert.doesNotThrow(() => {
    SDK.reportSilentFailure({
      reason: "user clicked button but nothing happened",
      component: "SubmitButton",
      trace_id: SDK.getTraceId(),
    });
  });
});

test("reportUIEvent: 记录 UI 交互事件", () => {
  assert.doesNotThrow(() => {
    SDK.reportUIEvent({
      type: "click",
      target: "button#checkout",
      timestamp: Date.now(),
    });
  });
});

test("flush: 手动触发队列 flush 不抛出异常", () => {
  assert.doesNotThrow(() => {
    SDK.flush();
  });
});

// ── FIX: P1-G2 —— 错误类上报豁免采样 ──────────────────────────────
// sampleRate 此前对所有事件统一门控：sampleRate=0.5 时手动
// reportError/reportSilentFailure/reportNetworkError 与全局异常捕获
// 有一半概率被无提示丢弃。现错误类路径 force=true 绕过采样，
// 遥测类（ui-event/console/network 自动捕获）保持原有采样行为。

class MockXHR {
  constructor() {
    this.headers = {};
    this.onreadystatechange = null;
    this.readyState = 0;
    this.status = 0;
    this.body = null;
    MockXHR.instances.push(this);
  }
  open(method, url, async) { this.method = method; this.url = url; this.async = async; }
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
MockXHR.reset = function () { MockXHR.instances = []; };
globalThis.XMLHttpRequest = MockXHR;

function _g2_config() {
  SDK._setConfig("endpoint", "http://localhost:8000");
  SDK._setConfig("sampleRate", 0);
  SDK._setConfig("batchSize", 1000);
  SDK._setConfig("enableCompression", false);
  SDK._setConfig("maxBatchesPerWindow", 1000);
  SDK._setConfig("throttleWindowMs", 60000);
}

test("G2: sampleRate=0 时错误类上报豁免采样（全部送达）", () => {
  _g2_config();
  MockXHR.reset();

  SDK.reportError(new Error("must not be sampled"));
  SDK.reportSilentFailure({ description: "silent must not be sampled" });
  SDK.reportNetworkError({ method: "GET", url: "http://x/y", error: "boom" });
  SDK.flush();

  const paths = MockXHR.instances
    .filter((x) => x.body)
    .map((x) => JSON.parse(x.body).events.map((e) => e.path))
    .flat();

  // 三类错误上报（+网络错误自动触发的 silent-failure）全部绕过采样
  assert.ok(paths.includes("/ingest/error"), `应包含 /ingest/error，实际: ${paths}`);
  assert.ok(paths.includes("/ingest/silent-failure"), `应包含 /ingest/silent-failure，实际: ${paths}`);
  assert.ok(paths.includes("/ingest/network"), `应包含 /ingest/network（reportNetworkError 豁免），实际: ${paths}`);
});

test("G2: sampleRate=0 时遥测类事件仍被采样过滤", () => {
  _g2_config();
  MockXHR.reset();

  SDK.reportUIEvent({ event_type: "click", target_selector: "button" });
  SDK.flush();

  // UI 遥测参与采样：sampleRate=0 下不产生任何请求
  const sends = MockXHR.instances.filter((x) => x.body);
  assert.equal(sends.length, 0, `遥测不应绕过采样，实际发送 ${sends.length} 个请求`);
});

// ── W15 / P3-SDK-3：上报体积必须在客户端收敛 ──────────────────────────────
// 服务端 parse_network_record 对 request_body/response_body 截到 10240、url 截到
// 2048，但那发生在**入库前**：兆级字符串照样穿过网络、被整体读进内存、被脱敏正则
// 各扫一遍，还会挤爆 localStorage 降级队列（浏览器 ~5MB 配额）。浏览器 SDK 此前
// 只在 fetch/XHR 钩子里把 response_body 截到 2000，url 与 request_body 完全不设限
// （_serializeRequestBody 对字符串原样返回），Node SDK 侧同一缺陷已一并修。
//
// 这两个用例放在文件末尾并显式清空 endpoint：_send 在 endpoint 为空时直接返回，
// 因此不会产生批次定时器（node --test 会等空事件循环），也不依赖 MockXHR。
test("P3-SDK-3: 超长 url / body 在客户端截断并留下可诊断标记", () => {
  SDK._setConfig("endpoint", "");
  const captured = [];
  SDK.onNetworkCapture((record) => captured.push(record));
  try {
    SDK.reportNetworkError({
      method: "POST",
      url: "http://example.com/search?q=" + "y".repeat(64 * 1024),
      status_code: 500,
      request_body: "x".repeat(1024 * 1024),
      response_body: "z".repeat(1024 * 1024),
    });
  } finally {
    SDK.onNetworkCapture(null);
  }

  assert.equal(captured.length, 1, "onNetworkCapture 应收到 1 条记录");
  const record = captured[0];
  // 客户端上限必须**严格小于**服务端上限（10240 / 2048），否则服务端二次截断会
  // 把客户端标记切掉，入库内容退化成"静默截断"。
  assert.ok(record.request_body.length <= 10240, `request_body 未收敛: ${record.request_body.length}`);
  assert.ok(record.response_body.length <= 10240, `response_body 未收敛: ${record.response_body.length}`);
  assert.ok(record.url.length <= 2048, `url 未收敛: ${record.url.length}`);
  assert.ok(/（客户端已截断）$/.test(record.request_body), "request_body 缺截断标记");
  assert.ok(/（客户端已截断）$/.test(record.response_body), "response_body 缺截断标记");
  assert.ok(/（客户端已截断）$/.test(record.url), "url 缺截断标记");
  assert.equal(record.method, "POST", "非字符串字段不得被改写");
  assert.equal(record.status_code, 500);
});

test("P3-SDK-3: 未超限字段逐字节不变，且脱敏先于截断", () => {
  SDK._setConfig("endpoint", "");
  const captured = [];
  SDK.onNetworkCapture((record) => captured.push(record));
  try {
    SDK.reportNetworkError({
      method: "GET",
      url: "http://example.com/orders/1",
      request_body: "small body",
      response_body: '{"ok":true}',
    });
    // 秘密放在截断点之前：先截断后脱敏会把它完整保留；放在截断点之后则会被切
    // 成半截、正则匹配不到 —— 两种顺序都不允许漏原文，故必须是"脱敏 → 截断"。
    SDK.reportNetworkError({
      method: "POST",
      url: "http://example.com/login",
      request_body: 'password="hunter2-secret" ' + "b".repeat(20000),
    });
  } finally {
    SDK.onNetworkCapture(null);
  }

  assert.equal(captured.length, 2);
  assert.equal(captured[0].request_body, "small body");
  assert.equal(captured[0].response_body, '{"ok":true}');
  assert.equal(captured[0].url, "http://example.com/orders/1");
  assert.equal(captured[0].request_body.includes("（客户端已截断）"), false, "未超限不得加标记");

  const big = captured[1].request_body;
  assert.equal(big.includes("hunter2-secret"), false, "脱敏必须在截断之前完成");
  assert.ok(big.includes("***REDACTED***"), "脱敏结果应留下 REDACTED 占位");
  assert.ok(/（客户端已截断）$/.test(big), "截断标记必须保留在末尾");
  assert.ok(big.length <= 10240, `request_body 未收敛: ${big.length}`);
});
