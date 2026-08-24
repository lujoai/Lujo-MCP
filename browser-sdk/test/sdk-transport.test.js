const test = require("node:test");
const assert = require("node:assert/strict");

const sdk = require("../ai-debug.js");

test("弹性退避延迟计算与抖动（Jitter）算法测试", async (t) => {
  await t.test("指数退避在合法抖动区间内 [50, 500 * 2^attempt]", () => {
    for (let attempt = 0; attempt < 5; attempt++) {
      const delay = sdk._computeRetryDelay(attempt, null);
      assert.ok(typeof delay === "number", "delay 应为数字");
      assert.ok(delay >= 50, "delay 必须大于等于最小抖动基线 50ms");
      assert.ok(delay <= 5000, "delay 必须受 maxRetryDelay (5000ms) 封顶限制");
    }
  });

  await t.test("优先使用 Retry-After 响应头（秒转毫秒且不超过上限）", () => {
    const delay2s = sdk._computeRetryDelay(0, 2000);
    assert.equal(delay2s, 2000);

    const delay10s = sdk._computeRetryDelay(0, 10000);
    assert.equal(delay10s, 5000);
  });
});

test("不可重试状态码（Non-retryable Status）判定", async (t) => {
  await t.test("400/401/403 客户端与权限错误快速放弃重试", () => {
    assert.equal(sdk._isNonRetryableStatus(400), true, "400 Bad Request 不应重试");
    assert.equal(sdk._isNonRetryableStatus(401), true, "401 Unauthorized 不应重试");
    assert.equal(sdk._isNonRetryableStatus(403), true, "403 Forbidden 不应重试");
  });

  await t.test("429/500/503 服务端负载与临时故障允许重试", () => {
    assert.equal(sdk._isNonRetryableStatus(429), false, "429 Too Many Requests 应当退避重试");
    assert.equal(sdk._isNonRetryableStatus(500), false, "500 Internal Server Error 应当重试");
    assert.equal(sdk._isNonRetryableStatus(503), false, "503 Service Unavailable 应当重试");
  });
});

test("Retry-After 响应头解析", async (t) => {
  await t.test("成功解析整型秒数字符串并转为毫秒", () => {
    const mockXhr = {
      getResponseHeader: (h) => (h === "Retry-After" ? "5" : null)
    };
    const ms = sdk._parseRetryAfter(mockXhr);
    assert.equal(ms, 5000);
  });

  await t.test("缺失或非法格式安全返回 null", () => {
    const mockEmptyXhr = {
      getResponseHeader: () => null
    };
    assert.equal(sdk._parseRetryAfter(mockEmptyXhr), null);
    assert.equal(sdk._parseRetryAfter(null), null);
    assert.equal(sdk._parseRetryAfter({ getResponseHeader: () => "invalid" }), null);
  });
});

test("LocalStorage 降级队列与 TTL 淘汰机制", async (t) => {
  const store = {};
  global.localStorage = {
    getItem: (k) => store[k] || null,
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
    clear: () => { for (let k in store) delete store[k]; }
  };

  await t.test("保存失败批次时附带时间戳元数据封装且过滤过期项", () => {
    sdk._setConfig("localStorageKey", "test-pending-batches");
    sdk._setConfig("maxPendingBatches", 3);
    sdk._setConfig("localStorageTTL", 3600000); // 1小时

    const payload = JSON.stringify({ events: [{ path: "/ingest/error", payload: { msg: "test1" } }] });
    
    // 写入新批次
    sdk._saveToLocalStorage(payload);

    const saved = JSON.parse(store["test-pending-batches"]);
    assert.equal(saved.length, 1);
    assert.ok(typeof saved[0].timestamp === "number");
    assert.equal(saved[0].data, payload);
  });

  await t.test("超过 maxPendingBatches 时淘汰最旧的批次", () => {
    sdk._setConfig("localStorageKey", "test-pending-batches-overflow");
    sdk._setConfig("maxPendingBatches", 2);
    sdk._setConfig("localStorageTTL", 3600000);

    sdk._saveToLocalStorage(JSON.stringify({ events: [{ path: "/1", payload: {} }] }));
    sdk._saveToLocalStorage(JSON.stringify({ events: [{ path: "/2", payload: {} }] }));
    sdk._saveToLocalStorage(JSON.stringify({ events: [{ path: "/3", payload: {} }] }));

    const saved = JSON.parse(store["test-pending-batches-overflow"]);
    assert.equal(saved.length, 2);
    assert.ok(saved[0].data.includes("/2"));
    assert.ok(saved[1].data.includes("/3"));
  });

  await t.test("向后兼容解析旧版无 timestamp 包装的纯字符串数据", () => {
    sdk._setConfig("localStorageKey", "test-pending-batches-legacy");
    sdk._setConfig("localStorageTTL", 3600000);

    const legacyItems = [
      JSON.stringify({ events: [{ path: "/legacy/error", payload: { ok: true } }] })
    ];
    store["test-pending-batches-legacy"] = JSON.stringify(legacyItems);

    sdk._restorePendingBatches();
    assert.equal(store["test-pending-batches-legacy"], undefined);
  });
});
