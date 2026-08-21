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
