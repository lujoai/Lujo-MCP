# @lujoai/lujo-mcp-node-sdk

Minimal Node.js SDK for reporting errors and failed network requests to a
Lujo-MCP server. It uses the native `fetch` available in Node 18+, keeps an
in-memory queue, and never installs process-wide exception handlers or request
interceptors.

```js
const { createClient } = require("@lujoai/lujo-mcp-node-sdk");

const client = createClient({
  endpoint: "http://127.0.0.1:8000",
  apiKey: process.env.LUJO_MCP_API_KEY,
});

client.reportError(new Error("database unavailable"), { operation: "read" });
client.reportNetworkError({
  method: "GET",
  url: "https://example.test/orders",
  status_code: 503,
});

await client.flush();
await client.close();
```

The client sends `/ingest/batch` events with `source: "node-sdk"` by default.
Each HTTP request contains at most 100 events. `400`/`401`/`403` and other
permanent client errors are sent once; `429`, `5xx`, timeouts, and network
failures use a bounded retry policy controlled by `maxRetries`.
