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

test("reports error frames and network records through a real local HTTP server", async () => {
  const requests = [];
  const { server, endpoint } = await startServer(async ({ request, response, body, path }) => {
    requests.push({ request, body, path });
    respond(response, 200, { count: body.events.length, results: [] });
  });
  const client = cjsApi.createClient({
    endpoint,
    apiKey: "local-api-key",
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
    assert.deepEqual(await client.flush(), { sent: 0, failed: 1, batches: 1, attempts: 3 });
    assert.equal(requestCount, 3);
    assert.deepEqual(await client.flush(), { sent: 0, failed: 0, batches: 0, attempts: 0 });
    assert.equal(requestCount, 3, "an exhausted batch must not be retried by a later flush");
  } finally {
    await client.close();
    await closeServer(server);
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
      assert.deepEqual(await client.flush(), { sent: 0, failed: 1, batches: 1, attempts: 1 });
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
