/**
 * lujo-mcp Browser SDK 核心契约单测（Node，无浏览器/无 DOM 依赖）。
 *
 * 背景（TST-3）：SDK 闭包式配置（var cfg）不暴露 _cfg，e2e 曾因此失联；
 * 本文件在 CI 中以 Node 直接加载 UMD 包，守护：
 *   1. 导出 API 面完整（init/reportError/.../flush）
 *   2. V5 传输增强配置契约（gzip 阈值 / 节流 / localStorage 降级）
 *   3. _getPublicConfig 只读视图不含 apiKey（安全关键）
 *   4. _setConfig 测试辅助行为
 *
 * 运行：node --test browser-sdk/test/
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const SDK = require(path.join(__dirname, "..", "ai-debug.js"));

// 必须存在的公开 API 面（与 e2e / demo 页的用法一致）
const PUBLIC_API = [
  "init",
  "flush",
  "reportSilentFailure",
  "reportNetworkError",
  "reportError",
  "reportUIEvent",
  "getSessionId",
  "getTraceId",
  "setTraceId",
];

test("SDK 导出公开 API 面完整", () => {
  for (const name of PUBLIC_API) {
    assert.strictEqual(typeof SDK[name], "function", `缺少公开方法 ${name}`);
  }
  // 测试辅助只读配置视图与运行时改配置（e2e 依赖）
  assert.strictEqual(typeof SDK._getPublicConfig, "function");
  assert.strictEqual(typeof SDK._setConfig, "function");
});

test("初始化状态：_inited 初始为 false", () => {
  assert.strictEqual(SDK._inited, false);
});

test("_getPublicConfig 暴露 V5 传输增强配置契约（gzip/节流/localStorage）", () => {
  const cfg = SDK._getPublicConfig();
  // gzip 压缩（V5）
  assert.strictEqual(cfg.enableCompression, true);
  assert.strictEqual(cfg.compressionThreshold, 4096);
  // 节流控制（V5）
  assert.strictEqual(cfg.throttleWindowMs, 5000);
  assert.strictEqual(cfg.maxBatchesPerWindow, 2);
  // localStorage 降级（V5）
  assert.strictEqual(cfg.enableLocalStorageFallback, true);
  assert.strictEqual(cfg.localStorageKey, "ai-debug-pending-batches");
  assert.strictEqual(cfg.maxPendingBatches, 10);
  // v0.6.2 弹性退避与存储卫生配置
  assert.strictEqual(cfg.maxRetryDelay, 5000);
  assert.strictEqual(cfg.localStorageTTL, 86400000);
  // v0.5.1 Source Map：release 透传（空 = 不发送）
  assert.strictEqual(cfg.release, "");
});

test("_getPublicConfig 只读视图不含 apiKey（安全关键）", () => {
  const cfg = SDK._getPublicConfig();
  assert.ok(!("apiKey" in cfg), "_getPublicConfig 不得泄露 apiKey");
});

test("_setConfig 可运行时改配置（e2e 测试辅助）", () => {
  const before = SDK._getPublicConfig().compressionThreshold;
  SDK._setConfig("compressionThreshold", before + 1);
  assert.strictEqual(SDK._getPublicConfig().compressionThreshold, before + 1);
  // 无效 key 静默忽略
  SDK._setConfig("__not_a_real_key__", 42);
  assert.ok(!("__not_a_real_key__" in SDK._getPublicConfig()));
  // 还原
  SDK._setConfig("compressionThreshold", before);
});

test("trace id：get/set 往返", () => {
  const traceId = SDK.getTraceId();
  assert.ok(typeof traceId === "string" && traceId.length > 0);
  SDK.setTraceId("unit-test-trace");
  assert.strictEqual(SDK.getTraceId(), "unit-test-trace");
});

test("session id：非空且稳定", () => {
  const sid = SDK.getSessionId();
  assert.ok(typeof sid === "string" && sid.length > 0);
  assert.strictEqual(SDK.getSessionId(), sid);
});
