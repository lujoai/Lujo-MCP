"use strict";

const path = require("node:path");

const { createClient } = require(path.resolve(__dirname, "../../node-sdk"));

async function main() {
  const [endpoint, apiKey, traceId, sessionId] = process.argv.slice(2);
  if (!endpoint || !traceId || !sessionId) {
    throw new Error("usage: node_sdk_report.cjs <endpoint> <apiKey> <traceId> <sessionId>");
  }

  const client = createClient({
    endpoint,
    apiKey,
    traceId,
    sessionId,
    release: "node-sdk-e2e@0.7.9",
    batchIntervalMs: 60000,
    retryDelayMs: 10,
  });

  try {
    const error = new TypeError("node-sdk-e2e-error");
    error.stack = [
      "TypeError: node-sdk-e2e-error",
      "    at runJob (file:///srv/jobs.mjs:17:23)",
    ].join("\n");
    client.reportError(error, { operation: "e2e", password: "must-not-leak" });
    client.reportNetworkError({
      method: "GET",
      url: "https://service.test/node-sdk-e2e",
      status_code: 503,
      duration_ms: 42,
    });
    const result = await client.flush();
    process.stdout.write(`${JSON.stringify(result)}\n`);
  } finally {
    await client.close();
  }
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
