/**
 * _isSelfRequest 自请求排除范围收窄 —— 红测试（工作单 A，先红后绿第二步）。
 *
 * 缺陷口径（第一步改前分析已实证）：ai-debug.js `_isSelfRequest`（:861-879）按
 * 「与 endpoint 同 scheme+host 即排除（endpoint 带路径时按路径前缀排除）」整域判定，
 * 而 SDK 自身实际只会发出两类路径：
 *   - {endpoint去尾斜杠}/ingest/batch（_flushBatch:375 / _drainPendingBatches:441 /
 *     localStorage 恢复 :800 汇入 _flushBatch；sendBeacon 场景同路径带 ?token= query :533）
 *   - {endpoint去尾斜杠}/auth/beacon-token（_refreshBeaconToken:333）
 * 判定过宽 → 与 endpoint 同源的业务请求（demo 页 /api/debug/*、/mcp 等）被静默排除采集。
 *
 * 新契约（本文件锁定的目标行为，旧实现下"不排除"组断言应红）：
 *   1. scheme/host 匹配 endpoint 且 pathname 恰为 <base>/ingest/batch 或
 *      <base>/auth/beacon-token → true（排除，防递归）；
 *   2. 其余同源路径（含裸 origin）→ false（采集）；
 *   3. base path 与 URL 生成处 `cfg.endpoint.replace(/\/+$/, "")` 同口径（尾斜杠归一）；
 *   4. query/hash 不参与匹配；pathname 匹配大小写敏感（host 按 URL 规范大小写不敏感）；
 *   5. 跨源与相似域仍拒绝（既有回归守护，保持绿）。
 *
 * 运行：node --test browser-sdk/test/sdk-self-request-scope.test.js
 * 套路对齐 sdk-transport-fixes.test.js：node:test + MockXHR 无关（纯判定函数）、
 * 每用例 freshSDK() 重新 require 隔离闭包、SDK._setConfig 免 init 改 cfg。
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
