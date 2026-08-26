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
