"use strict";

const assert = require("node:assert/strict");
const http = require("node:http");
const { test } = require("node:test");

const cjsApi = require("..");

const API_NAMES = [
  "createClient",
  "reportError",
  "reportNetworkError",
  "flush",
  "close",
  "getSessionId",
  "getTraceId",
  "setTraceId",
];

function respond(response, status, body = {}) {
  response.statusCode = status;
  response.setHeader("Connection", "close");
  response.setHeader("Content-Type", "application/json");
  response.end(JSON.stringify(body));
}

function startServer(handler) {
  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", async () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      let body = null;
      try {
        body = raw ? JSON.parse(raw) : null;
      } catch (_) {
        body = null;
      }
      try {
        await handler({
          request,
          response,
          body,
          raw,
          path: new URL(request.url, "http://127.0.0.1").pathname,
        });
      } catch (error) {
        if (!response.headersSent) respond(response, 500, { error: String(error) });
        else response.destroy();
      }
    });
  });

  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.removeListener("error", reject);
      resolve({
        server,
        endpoint: `http://127.0.0.1:${server.address().port}`,
      });
    });
  });
}

function closeServer(server) {
  if (!server.listening) return Promise.resolve();
  return new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}

test("the package exposes real conditional CJS and ESM root entry points", async () => {
  const esmApi = await import("@lujoai/lujo-mcp-node-sdk");

  for (const name of API_NAMES) {
    assert.equal(typeof cjsApi[name], "function", `CJS export ${name}`);
    assert.equal(typeof esmApi[name], "function", `ESM export ${name}`);
  }
  assert.equal(esmApi.default.createClient, esmApi.createClient);
  assert.equal(cjsApi.createClient, require("@lujoai/lujo-mcp-node-sdk").createClient);
});

test("type declarations cover every runtime export", () => {
  // W15 / P3-SDK-5：包里没有 types/.d.ts 时 TS 使用者拿到的全是 any。
  // 本用例不校验类型正确性（仓库不装 TypeScript），只守住**声明与运行时导出
  // 不漂移**：新增运行时导出而忘了补声明，这里就红。
  const fs = require("node:fs");
  const path = require("node:path");
  const dtsPath = path.join(__dirname, "..", "index.d.ts");
  assert.ok(fs.existsSync(dtsPath), "缺少 index.d.ts");
  const dts = fs.readFileSync(dtsPath, "utf8");

  for (const name of API_NAMES) {
    assert.ok(
      new RegExp(`export declare function ${name}\\b`).test(dts),
      `index.d.ts 缺少运行时导出 ${name} 的声明`,
    );
  }
  assert.ok(/export interface FlushResult\b/.test(dts), "缺少 FlushResult 声明");
  assert.ok(/lastErrorStatus\?: number/.test(dts), "FlushResult 缺少 lastErrorStatus");

  const pkg = require("../package.json");
  assert.equal(pkg.types, "./index.d.ts", "package.json 缺少 types 字段");
  assert.equal(pkg.exports["."].types, "./index.d.ts", "exports 缺少 types 条件");
  assert.ok(
    pkg.files.includes("index.d.ts"),
    "发布产物 files 必须包含 index.d.ts，否则装完仍然没有类型",
  );
});

test("reports error frames and network records through a real local HTTP server", async () => {
  const requests = [];
  const { server, endpoint } = await startServer(async ({ request, response, body, path }) => {
    requests.push({ request, body, path });
    respond(response, 200, { count: body.events.length, results: [] });
  });
  const client = cjsApi.createClient({
    endpoint,
    apiKey: "local-api-key",
    release: "orders-service@1.4.0",
    batchIntervalMs: 60000,
  });

  try {
    client.setTraceId("trace-node-local");
    const error = new TypeError("password=should-not-leak");
    error.stack = [
      "TypeError: password=should-not-leak",
      "    at handleRequest (file:///srv/app.mjs:17:23)",
      "    at async file:///srv/boot.mjs:20:7",
    ].join("\n");

    const errorReport = client.reportError(error, {
      authorization: "Bearer secret-auth",
      nested: { token: "secret-token", password: "secret-password" },
      note: "Authorization: Bearer another-secret",
      serialized: '{"token":"serialized-secret","visible":"ok"}',
    });
    client.reportNetworkError({
      method: "GET",
      url: "https://service.test/orders?token=query-secret",
      status_code: 503,
      headers: {
        Cookie: "session=secret-cookie",
        "X-API-Key": "secret-key",
      },
    });

    const result = await client.flush();
    assert.deepEqual(result, { sent: 2, failed: 0, batches: 1, attempts: 1 });
    assert.equal(requests.length, 1);
    assert.equal(requests[0].path, "/ingest/batch");
    assert.equal(requests[0].request.headers["x-api-key"], "local-api-key");
    assert.equal(requests[0].request.headers.authorization, undefined);

    const events = requests[0].body.events;
    assert.equal(events.length, 2);
    assert.equal(events[0].path, "/ingest/error");
    assert.equal(events[0].payload.source, "node-sdk");
    assert.equal(events[0].payload.trace_id, "trace-node-local");
    assert.equal(events[0].payload.session_id, client.getSessionId());
    assert.deepEqual(events[0].payload.frames, [
      {
        file: "file:///srv/app.mjs",
        line: 17,
        column: 23,
        function: "handleRequest",
      },
      {
        file: "file:///srv/boot.mjs",
        line: 20,
        column: 7,
        function: "async",
      },
    ]);
    assert.equal(errorReport.frame_count, 2);
    assert.equal(events[0].payload.extra.authorization, "***REDACTED***");
    assert.equal(events[0].payload.extra.nested.token, "***REDACTED***");
    assert.equal(events[0].payload.extra.nested.password, "***REDACTED***");
    assert.equal(events[0].payload.extra.release, "orders-service@1.4.0");
    assert.doesNotMatch(
      JSON.stringify(events[0].payload),
      /secret-auth|secret-token|another-secret|serialized-secret/,
    );
    assert.equal(events[1].path, "/ingest/network");
    assert.equal(events[1].payload.trace_id, "trace-node-local");
    assert.equal(events[1].payload.session_id, client.getSessionId());
    assert.equal(events[1].payload.record.source, "node-sdk");
    assert.equal(events[1].payload.record.headers.Cookie, "***REDACTED***");
    assert.equal(events[1].payload.record.headers["X-API-Key"], "***REDACTED***");
    assert.doesNotMatch(JSON.stringify(events[1].payload), /query-secret|secret-cookie|secret-key/);
  } finally {
    await client.close();
    await closeServer(server);
  }
});

test("honors a configured batch size below the server maximum", async () => {
  const batchLengths = [];
  const { server, endpoint } = await startServer(async ({ response, body }) => {
    batchLengths.push(body.events.length);
    respond(response, 200, { count: body.events.length });
  });
  const client = cjsApi.createClient({ endpoint, batchSize: 2, batchIntervalMs: 60000 });

  try {
    for (let index = 0; index < 5; index += 1) {
      client.reportError(new Error(`event-${index}`));
    }
    assert.deepEqual(await client.flush(), { sent: 5, failed: 0, batches: 3, attempts: 3 });
    assert.deepEqual(batchLengths, [2, 2, 1]);
  } finally {
    await client.close();
    await closeServer(server);
  }
});

test("does not retry a persisted response when only body cleanup fails", async () => {
  let requestCount = 0;
  const client = cjsApi.createClient({
    endpoint: "http://127.0.0.1:1",
    maxRetries: 3,
    batchIntervalMs: 60000,
    fetch: async () => {
      requestCount += 1;
      return {
        status: 202,
        body: { cancel: async () => { throw new Error("cleanup failed"); } },
      };
    },
  });

  try {
    client.reportError(new Error("accepted once"));
    assert.deepEqual(await client.flush(), { sent: 1, failed: 0, batches: 1, attempts: 1 });
    assert.equal(requestCount, 1);
  } finally {
    await client.close();
  }
});

test("reports per-event batch failures without retrying successful events", async () => {
  let requestCount = 0;
  const { server, endpoint } = await startServer(async ({ response, body }) => {
    requestCount += 1;
    respond(response, 200, {
      count: body.events.length,
      results: [
        { path: body.events[0].path, ok: true, result: { saved: true } },
        { path: body.events[1].path, ok: false, error: "Invalid request payload" },
      ],
    });
  });
  const client = cjsApi.createClient({ endpoint, batchIntervalMs: 60000, maxRetries: 3 });

  try {
    client.reportError(new Error("accepted"));
    client.reportNetworkError({ method: "GET", url: "not-a-url" });
    assert.deepEqual(await client.flush(), { sent: 1, failed: 1, batches: 1, attempts: 1 });
    assert.equal(requestCount, 1, "partial batch failures must not duplicate successful events");
  } finally {
    await client.close();
    await closeServer(server);
  }
});

test("never sends more than 100 events in one batch and splits 101 events", async () => {
  const batchLengths = [];
  const { server, endpoint } = await startServer(async ({ response, body }) => {
    batchLengths.push(body.events.length);
    respond(response, 200, { count: body.events.length });
  });
  const client = cjsApi.createClient({ endpoint, batchSize: 100, batchIntervalMs: 60000 });

  try {
    for (let index = 0; index < 101; index += 1) {
      client.reportError(new Error(`event-${index}`));
    }
    const result = await client.flush();
    assert.deepEqual(batchLengths, [100, 1]);
    assert.ok(batchLengths.every((length) => length <= 100));
    assert.deepEqual(result, { sent: 101, failed: 0, batches: 2, attempts: 2 });
  } finally {
    await client.close();
    await closeServer(server);
  }
});

test("retries 429 and 5xx responses with a bounded retry count", async () => {
  const statuses = [429, 500, 204];
  let requestCount = 0;
  const { server, endpoint } = await startServer(async ({ response }) => {
    const status = statuses[requestCount] || 204;
    requestCount += 1;
    respond(response, status);
  });
  const client = cjsApi.createClient({
    endpoint,
    maxRetries: 3,
    retryDelayMs: 1,
    maxRetryDelayMs: 2,
    batchIntervalMs: 60000,
  });

  try {
    client.reportError(new Error("retry me"));
    assert.deepEqual(await client.flush(), { sent: 1, failed: 0, batches: 1, attempts: 3 });
    assert.equal(requestCount, 3);
  } finally {
    await client.close();
    await closeServer(server);
  }
});

test("drops an exhausted transient batch after the configured bound", async () => {
  let requestCount = 0;
  const { server, endpoint } = await startServer(async ({ response }) => {
    requestCount += 1;
    respond(response, 503);
  });
  const client = cjsApi.createClient({
    endpoint,
    maxRetries: 2,
    retryDelayMs: 1,
    maxRetryDelayMs: 1,
    batchIntervalMs: 60000,
  });

  try {
    client.reportError(new Error("eventually unavailable"));
    assert.deepEqual(await client.flush(), {
      sent: 0,
      failed: 1,
      batches: 1,
      attempts: 3,
      lastErrorStatus: 503,
    });
    assert.equal(requestCount, 3);
    assert.deepEqual(await client.flush(), { sent: 0, failed: 0, batches: 0, attempts: 0 });
    assert.equal(requestCount, 3, "an exhausted batch must not be retried by a later flush");
  } finally {
    await client.close();
    await closeServer(server);
  }
});

test("flush surfaces the HTTP status that made a batch fail", async () => {
  // W15 / P3-SDK-4：401（密钥错）与 413（体积超限）对使用者的处置完全不同，
  // 而 failed 计数区分不了它们 —— status 此前在 _flushQueue 里被直接丢掉。
  for (const status of [401, 413]) {
    const { server, endpoint } = await startServer(async ({ response }) => {
      respond(response, status);
    });
    const client = cjsApi.createClient({ endpoint, maxRetries: 0, retryDelayMs: 1 });
    try {
      client.reportError(new Error(`rejected-${status}`));
      const result = await client.flush();
      assert.equal(result.lastErrorStatus, status);
      assert.equal(result.failed, 1);
    } finally {
      await client.close();
      await closeServer(server);
    }
  }
});

test("flush omits lastErrorStatus when no HTTP response rejected the batch", async () => {
  // 键缺席 = 「没有服务端拒绝」：连不上（无 HTTP 响应）与全部成功都不该带它，
  // 否则调用方会把网络不可达误读成某个状态码。
  const client = cjsApi.createClient({
    endpoint: "http://127.0.0.1:1",
    maxRetries: 0,
    retryDelayMs: 1,
    requestTimeoutMs: 2000,
  });
  try {
    client.reportError(new Error("unreachable"));
    const result = await client.flush();
    assert.equal(result.failed, 1);
    assert.ok(
      !("lastErrorStatus" in result),
      `network failure must not carry a status, got ${result.lastErrorStatus}`,
    );
  } finally {
    await client.close();
  }
});

test("does not retry permanent 4xx responses", async () => {
  const statuses = [400, 401, 403, 422];
  for (const status of statuses) {
    let requestCount = 0;
    const { server, endpoint } = await startServer(async ({ response }) => {
      requestCount += 1;
      respond(response, status);
    });
    const client = cjsApi.createClient({ endpoint, maxRetries: 5, retryDelayMs: 1 });
    try {
      client.reportError(new Error(`permanent-${status}`));
      assert.deepEqual(await client.flush(), {
        sent: 0,
        failed: 1,
        batches: 1,
        attempts: 1,
        lastErrorStatus: status,
      });
      assert.equal(requestCount, 1, `HTTP ${status} must not be retried`);
    } finally {
      await client.close();
      await closeServer(server);
    }
  }
});

test("retries network exceptions and succeeds without process-wide hooks", async () => {
  let requestCount = 0;
  const uncaughtExceptionListeners = process.listenerCount("uncaughtException");
  const unhandledRejectionListeners = process.listenerCount("unhandledRejection");
  const client = cjsApi.createClient({
    endpoint: "http://127.0.0.1:1",
    maxRetries: 2,
    retryDelayMs: 1,
    maxRetryDelayMs: 1,
    batchIntervalMs: 60000,
    fetch: async () => {
      requestCount += 1;
      if (requestCount < 3) throw new Error("temporary network failure");
      return { status: 200, text: async () => "{}" };
    },
  });

  try {
    client.reportError(new Error("network retry"));
    assert.deepEqual(await client.flush(), { sent: 1, failed: 0, batches: 1, attempts: 3 });
    assert.equal(requestCount, 3);
    assert.equal(process.listenerCount("uncaughtException"), uncaughtExceptionListeners);
    assert.equal(process.listenerCount("unhandledRejection"), unhandledRejectionListeners);
  } finally {
    await client.close();
  }
});

test("close waits for in-flight flush, is idempotent, and rejects later reports", async () => {
  let startedResolve;
  let releaseRequest;
  const started = new Promise((resolve) => {
    startedResolve = resolve;
  });
  const requestFinished = new Promise((resolve) => {
    releaseRequest = resolve;
  });
  const { server, endpoint } = await startServer(async ({ response }) => {
    startedResolve();
    await requestFinished;
    respond(response, 200, { ok: true });
  });
  const client = cjsApi.createClient({ endpoint, batchIntervalMs: 60000 });

  try {
    client.reportError(new Error("wait for close"));
    const firstClose = client.close();
    const secondClose = client.close();
    assert.strictEqual(firstClose, secondClose);

    await started;
    let settled = false;
    firstClose.then(() => {
      settled = true;
    });
    await Promise.resolve();
    assert.equal(settled, false);

    releaseRequest();
    await firstClose;
    assert.equal(settled, true);
    assert.equal(client._timer, null);
    assert.equal(client._flushPromise, null);
    assert.throws(() => client.reportError(new Error("after close")), /client is closed/);
    assert.deepEqual(await client.flush(), { sent: 1, failed: 0, batches: 1, attempts: 1 });
  } finally {
    await client.close();
    await closeServer(server);
  }
});

test("bounds oversized network fields before they hit the wire (W15 / P3-SDK-3)", async () => {
  // 缺陷复现口径：Node SDK 全文无截断。服务端 parse_network_record 对
  // request_body/response_body 截到 10240、url 截到 2048，但那发生在**入库前**；
  // /ingest/batch 的非 gzip 分支没有任何体积上限（只有 gzip 分支受
  // _MAX_DECOMPRESSED_SIZE=10MiB 保护），所以一个 1MiB 的响应体会原样穿过网络、
  // 被 request.json() 整体读进内存，再被 6 条脱敏正则各扫一遍，最后才丢掉 99%。
  // 浏览器 SDK 早就在客户端截（request_body_preview 512 / response_body 2000）。
  const rawSizes = [];
  let record = null;
  const { server, endpoint } = await startServer(async ({ response, body, raw }) => {
    rawSizes.push(raw.length);
    record = body.events[0].payload.record;
    respond(response, 200, { count: body.events.length });
  });
  const client = cjsApi.createClient({ endpoint, batchIntervalMs: 60000 });

  const hugeBody = "x".repeat(1024 * 1024);
  const hugeUrl = "http://service.test/search?q=" + "y".repeat(64 * 1024);

  try {
    client.reportNetworkError({
      method: "POST",
      url: hugeUrl,
      status_code: 500,
      request_body: hugeBody,
      response_body: hugeBody,
      duration_ms: 12,
    });
    await client.flush();
  } finally {
    await client.close();
    await closeServer(server);
  }

  // 客户端上限必须**严格小于**服务端上限，否则服务端会二次截断并把客户端留下的
  // 截断标记切掉 → 入库内容变成"静默截断"，看不出数据不完整。
  assert.ok(
    record.request_body.length <= 10240,
    `request_body 未在客户端收敛：${record.request_body.length} > 10240`,
  );
  assert.ok(
    record.response_body.length <= 10240,
    `response_body 未在客户端收敛：${record.response_body.length} > 10240`,
  );
  assert.ok(record.url.length <= 2048, `url 未在客户端收敛：${record.url.length} > 2048`);
  // 用 ok+test 而不是 assert.match：失败时 match 会把整段兆级字符串打进 TAP 输出
  assert.ok(/（客户端已截断）$/.test(record.request_body), "request_body 截断必须留下可诊断标记");
  assert.ok(/（客户端已截断）$/.test(record.response_body), "response_body 截断必须留下可诊断标记");
  assert.ok(/（客户端已截断）$/.test(record.url), "url 截断必须留下可诊断标记");
  // 整包体积有界：一条记录不许把批次撑到兆级
  assert.ok(rawSizes[0] < 64 * 1024, `上报体积未收敛：${rawSizes[0]} bytes`);
  // 非字符串/未超限字段不得被改写
  assert.equal(record.method, "POST");
  assert.equal(record.status_code, 500);
  assert.equal(record.duration_ms, 12);
});

test("leaves in-limit network fields byte-identical and truncates after redaction", async () => {
  let record = null;
  const { server, endpoint } = await startServer(async ({ response, body }) => {
    record = body.events[0].payload.record;
    respond(response, 200, { count: body.events.length });
  });
  const client = cjsApi.createClient({ endpoint, batchIntervalMs: 60000 });

  try {
    client.reportNetworkError({
      method: "GET",
      url: "http://service.test/orders/1",
      status_code: 200,
      request_body: "small body",
      response_body: '{"ok":true}',
      headers: { Cookie: "session=secret-cookie" },
    });
    // 敏感值必须**先脱敏再截断**：反过来会把 token 从中间切开，
    // 留下正则匹配不到的半截秘密。
    client.reportNetworkError({
      method: "GET",
      url: "http://service.test/orders/2",
      request_body: "a".repeat(20000) + ' password="hunter2-secret"',
    });
    await client.flush();
  } finally {
    await client.close();
    await closeServer(server);
  }

  assert.equal(record.request_body, "small body");
  assert.equal(record.response_body, '{"ok":true}');
  assert.equal(record.url, "http://service.test/orders/1");
  assert.equal(record.request_body.includes("（客户端已截断）"), false, "未超限不得加标记");
});

test("redaction still runs on oversized bodies before truncation", async () => {
  let record = null;
  const { server, endpoint } = await startServer(async ({ response, body }) => {
    record = body.events[0].payload.record;
    respond(response, 200, { count: body.events.length });
  });
  const client = cjsApi.createClient({ endpoint, batchIntervalMs: 60000 });

  try {
    client.reportNetworkError({
      method: "POST",
      url: "http://service.test/login",
      // 秘密放在**截断点之前**：若先截断后脱敏，它会被完整保留；
      // 放在截断点之后则会被切掉一半 —— 两种顺序都不允许漏原文。
      request_body: 'password="hunter2-secret" ' + "b".repeat(20000),
    });
    await client.flush();
  } finally {
    await client.close();
    await closeServer(server);
  }

  assert.equal(record.request_body.includes("hunter2-secret"), false, "脱敏必须在截断前完成");
  assert.ok(/\*\*\*REDACTED\*\*\*/.test(record.request_body), "脱敏结果必须留下 REDACTED 占位");
  assert.ok(/（客户端已截断）$/.test(record.request_body), "截断标记必须在末尾保留");
});
