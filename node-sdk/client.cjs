"use strict";

const crypto = require("node:crypto");

const MAX_BATCH_SIZE = 100;
// W15 / P3-SDK-3：网络记录字段的客户端体积上限。服务端
// app/runtime/collectors/network.py 对 request_body/response_body 截到 10240、
// url 截到 2048，但那发生在**入库前**：/ingest/batch 的非 gzip 分支没有任何体积
// 上限（只有 gzip 分支受 _MAX_DECOMPRESSED_SIZE=10MiB 保护），兆级响应体会原样
// 穿过网络、被整体读进内存、被 6 条脱敏正则各扫一遍，最后才丢掉 99%。
// 客户端上限必须**严格小于**服务端上限，留出截断标记的位置：等长会让服务端二次
// 截断把标记切掉，入库内容退化成"静默截断"，看不出数据不完整。
// 浏览器 SDK 的同位阈值是 512（request_body_preview）/ 2000（response_body）。
const MAX_BODY_CHARS = 10176;
const MAX_URL_CHARS = 2000;
const BODY_TRUNCATED_SUFFIX = "\n...（客户端已截断）";
const URL_TRUNCATED_SUFFIX = "...（客户端已截断）";
const DEFAULT_ENDPOINT = "http://127.0.0.1:8000";
const DEFAULT_BATCH_SIZE = 100;
const DEFAULT_BATCH_INTERVAL_MS = 1000;
const DEFAULT_MAX_RETRIES = 3;
const DEFAULT_RETRY_DELAY_MS = 100;
const DEFAULT_MAX_RETRY_DELAY_MS = 2000;
const DEFAULT_REQUEST_TIMEOUT_MS = 10000;
const REDACTED = "***REDACTED***";

const SENSITIVE_KEY_RE =
  /(?:authorization|proxy[-_]?authorization|cookie|api[-_]?key|access[-_]?token|refresh[-_]?token|auth[-_]?token|token|password|passwd|pwd|secret|private[-_]?key|client[-_]?secret)/i;
const SENSITIVE_ASSIGNMENT_RE =
  /\b(?:authorization|proxy-authorization|cookie|x-api-key|api[-_]?key|access[-_]?token|refresh[-_]?token|auth[-_]?token|token|password|passwd|pwd|secret|private[-_]?key|client[-_]?secret)\b\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^,\s;&}]+)/gi;
const SENSITIVE_JSON_RE =
  /(["'])(?:authorization|proxy-authorization|cookie|x-api-key|api[-_]?key|access[-_]?token|refresh[-_]?token|auth[-_]?token|token|password|passwd|pwd|secret|private[-_]?key|client[-_]?secret)\1\s*:\s*("[^"]*"|'[^']*'|[^,}\]]+)/gi;
const SENSITIVE_QUERY_RE =
  /([?&](?:api[-_]?key|access[-_]?token|refresh[-_]?token|auth[-_]?token|token|password|secret|key)=)([^&#\s]+)/gi;
const AUTHORIZATION_VALUE_RE =
  /\b((?:proxy-)?authorization)\s*[:=]\s*((?:bearer|basic)\s+)([^\s,;]+)/gi;
const AUTH_SCHEME_RE = /\b((?:bearer|basic)\s+)([^\s,;]+)/gi;
const URL_PASSWORD_RE = /(https?:\/\/[^/\s:@]+:)([^@/\s]+)(@)/gi;

function newId() {
  return crypto.randomUUID();
}

function isSensitiveKey(key) {
  const normalized = String(key).replace(/([a-z])([A-Z])/g, "$1_$2");
  return SENSITIVE_KEY_RE.test(normalized);
}

function redactString(value) {
  let text = String(value);
  text = text.replace(AUTHORIZATION_VALUE_RE, (_, name, scheme) => `${name}: ${scheme}${REDACTED}`);
  text = text.replace(SENSITIVE_ASSIGNMENT_RE, (match) => {
    const separator = match.match(/\s*[:=]\s*/);
    const prefix = separator ? match.slice(0, separator.index + separator[0].length) : match;
    const rawValue = separator ? match.slice(separator.index + separator[0].length) : "";
    if (!separator) return REDACTED;
    const quote = rawValue[0] === "\"" || rawValue[0] === "'" ? rawValue[0] : "";
    return `${prefix}${quote ? `${quote}${REDACTED}${quote}` : REDACTED}`;
  });
  text = text.replace(SENSITIVE_JSON_RE, (match, quote, rawValue) => {
    const valueQuote = rawValue[0] === "\"" || rawValue[0] === "'" ? rawValue[0] : "";
    const prefix = match.slice(0, match.length - rawValue.length);
    return `${prefix}${valueQuote ? `${valueQuote}${REDACTED}${valueQuote}` : REDACTED}`;
  });
  text = text.replace(SENSITIVE_QUERY_RE, `$1${REDACTED}`);
  text = text.replace(URL_PASSWORD_RE, `$1${REDACTED}$3`);
  return text.replace(AUTH_SCHEME_RE, `$1${REDACTED}`);
}

function redactValue(value, active = new WeakSet(), depth = 0) {
  if (value === null) return null;
  if (typeof value === "string") return redactString(value);
  if (typeof value === "number" || typeof value === "boolean") return value;
  if (typeof value === "bigint") return String(value);
  if (typeof value === "undefined") return null;
  if (typeof value === "function" || typeof value === "symbol") return `[unsupported ${typeof value}]`;
  if (depth >= 12) return "[max-depth]";
  if (active.has(value)) return "[circular]";

  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? "[invalid-date]" : value.toISOString();
  }
  if (Buffer.isBuffer(value)) return "[binary-data]";
  if (value instanceof Error) {
    const errorValue = {
      name: typeof value.name === "string" ? value.name : "Error",
      message: typeof value.message === "string" ? value.message : String(value.message || ""),
    };
    if (typeof value.stack === "string") errorValue.stack = value.stack;
    return redactValue(errorValue, active, depth + 1);
  }

  active.add(value);
  try {
    if (Array.isArray(value)) {
      return value.map((item) => redactValue(item, active, depth + 1));
    }

    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      try {
        if (typeof value.toJSON === "function") {
          return redactValue(value.toJSON(), active, depth + 1);
        }
      } catch (_) {
        return "[unserializable-object]";
      }
      return redactString(String(value));
    }

    const output = {};
    for (const key of Object.keys(value)) {
      if (isSensitiveKey(key)) {
        output[key] = REDACTED;
        continue;
      }
      try {
        output[key] = redactValue(value[key], active, depth + 1);
      } catch (_) {
        output[key] = "[unreadable]";
      }
    }
    return output;
  } finally {
    active.delete(value);
  }
}

function sanitizePayload(payload) {
  return redactValue(payload);
}

function truncateText(value, limit, suffix) {
  if (typeof value !== "string" || value.length <= limit) return value;
  return value.slice(0, limit) + suffix;
}

// 只对**已存在**的字段动手：补一个 `url: undefined` 会被 redactValue 归一成
// null，凭空给上报载荷加键。
function boundNetworkRecord(record) {
  if (!record || typeof record !== "object" || Array.isArray(record)) return record;
  const bounded = { ...record };
  if ("url" in bounded) {
    bounded.url = truncateText(bounded.url, MAX_URL_CHARS, URL_TRUNCATED_SUFFIX);
  }
  if ("request_body" in bounded) {
    bounded.request_body = truncateText(bounded.request_body, MAX_BODY_CHARS, BODY_TRUNCATED_SUFFIX);
  }
  if ("response_body" in bounded) {
    bounded.response_body = truncateText(
      bounded.response_body,
      MAX_BODY_CHARS,
      BODY_TRUNCATED_SUFFIX,
    );
  }
  return bounded;
}

function parseStack(stack) {
  if (typeof stack !== "string" || !stack) return [];

  const frames = [];
  for (const rawLine of stack.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;

    let body = line.startsWith("at ") ? line.slice(3).trim() : line;
    let functionName = "unknown";
    if (body.endsWith(")")) {
      const opening = body.lastIndexOf(" (");
      if (opening >= 0) {
        functionName = body.slice(0, opening).trim() || "unknown";
        body = body.slice(opening + 2, -1);
      }
    }

    let file;
    let lineNumber;
    let columnNumber;
    if (!line.startsWith("at ")) {
      const firefoxMatch = body.match(/^(.*?)@(.+):(\d+):(\d+)$/);
      if (firefoxMatch) {
        functionName = firefoxMatch[1].trim() || "unknown";
        file = firefoxMatch[2].trim();
        lineNumber = Number.parseInt(firefoxMatch[3], 10);
        columnNumber = Number.parseInt(firefoxMatch[4], 10);
      }
    }

    if (!file) {
      const nodeMatch = body.match(/^(.*):(\d+):(\d+)$/);
      if (!nodeMatch) continue;
      file = nodeMatch[1].trim();
      lineNumber = Number.parseInt(nodeMatch[2], 10);
      columnNumber = Number.parseInt(nodeMatch[3], 10);
      if (functionName === "unknown" && file.startsWith("async ")) {
        functionName = "async";
        file = file.slice("async ".length).trim();
      }
    }
    if (!file || !Number.isInteger(lineNumber) || !Number.isInteger(columnNumber)) continue;
    frames.push({
      file,
      line: lineNumber,
      column: columnNumber,
      function: functionName,
    });
  }
  return frames;
}

function errorDetails(error) {
  if (error === null || typeof error === "undefined") {
    return { type: "Error", message: "", frames: [] };
  }

  if (typeof error === "string") {
    return { type: "Error", message: error, frames: [] };
  }

  let type = "Error";
  let message = "";
  let stack = "";
  try {
    if (error && typeof error.name === "string" && error.name) type = error.name;
    else if (error && error.constructor && typeof error.constructor.name === "string") type = error.constructor.name;
  } catch (_) {
    // Keep the safe defaults if a foreign error object has hostile accessors.
  }
  try {
    if (error && typeof error.message === "string") message = error.message;
    else message = String(error);
  } catch (_) {
    message = "[unreadable error]";
  }
  try {
    if (error && typeof error.stack === "string") stack = error.stack;
  } catch (_) {
    stack = "";
  }

  let frames = parseStack(stack);
  if (frames.length === 0 && error && typeof error === "object") {
    try {
      if (error.fileName && Number.isFinite(Number(error.lineNumber))) {
        frames = [{
          file: String(error.fileName),
          line: Number(error.lineNumber),
          column: Number.isFinite(Number(error.columnNumber)) ? Number(error.columnNumber) : 0,
          function: typeof error.functionName === "string" && error.functionName ? error.functionName : "unknown",
        }];
      }
    } catch (_) {
      frames = [];
    }
  }
  return { type, message, frames };
}

function integerOption(value, fallback, minimum, maximum) {
  if (value === undefined || value === null || value === "") return fallback;
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.min(maximum, Math.max(minimum, Math.floor(number)));
}

function normalizeEndpoint(endpoint) {
  const configured = endpoint || process.env.LUJO_MCP_ENDPOINT || DEFAULT_ENDPOINT;
  if (typeof configured !== "string" || !configured.trim()) {
    throw new TypeError("endpoint must be a non-empty HTTP(S) URL");
  }

  let url;
  try {
    url = new URL(configured);
  } catch (_) {
    throw new TypeError("endpoint must be a valid HTTP(S) URL");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new TypeError("endpoint must use http:// or https://");
  }

  url.hash = "";
  const path = url.pathname.replace(/\/+$/, "");
  if (path.endsWith("/ingest/batch")) url.pathname = path;
  else if (path.endsWith("/ingest")) url.pathname = `${path}/batch`;
  else url.pathname = `${path}/ingest/batch`;
  return url.toString();
}

function isRetryableStatus(status) {
  return status === 408 || status === 425 || status === 429 || (status >= 500 && status <= 599);
}

function waitFor(ms) {
  if (ms <= 0) return Promise.resolve();
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const EMPTY_RESULT = Object.freeze({ sent: 0, failed: 0, batches: 0, attempts: 0 });

class NodeSdkClient {
  constructor(options = {}) {
    if (!options || typeof options !== "object" || Array.isArray(options)) {
      throw new TypeError("createClient options must be an object");
    }

    this._batchUrl = normalizeEndpoint(options.endpoint);
    this._fetch = options.fetch || globalThis.fetch;
    if (typeof this._fetch !== "function") {
      throw new TypeError("Node 18+ global fetch is required");
    }

    const apiKey = options.apiKey ?? options.api_key ?? process.env.LUJO_MCP_API_KEY ?? "";
    if (typeof apiKey !== "string") throw new TypeError("apiKey must be a string");
    this._apiKey = apiKey;
    this._source = typeof options.source === "string" && options.source ? options.source : "node-sdk";
    this._release = this._normalizeId(options.release);
    this._batchSize = integerOption(options.batchSize, DEFAULT_BATCH_SIZE, 1, MAX_BATCH_SIZE);
    this._batchIntervalMs = integerOption(
      options.batchIntervalMs ?? options.batchInterval,
      DEFAULT_BATCH_INTERVAL_MS,
      0,
      3600000,
    );
    this._maxRetries = integerOption(options.maxRetries, DEFAULT_MAX_RETRIES, 0, 10);
    this._retryDelayMs = integerOption(options.retryDelayMs, DEFAULT_RETRY_DELAY_MS, 0, 60000);
    this._maxRetryDelayMs = integerOption(
      options.maxRetryDelayMs,
      DEFAULT_MAX_RETRY_DELAY_MS,
      0,
      120000,
    );
    this._requestTimeoutMs = integerOption(
      options.requestTimeoutMs,
      DEFAULT_REQUEST_TIMEOUT_MS,
      0,
      120000,
    );

    this._sessionId = this._normalizeId(options.sessionId ?? options.session_id) || newId();
    this._traceId = this._normalizeId(options.traceId ?? options.trace_id) || newId();
    this._queue = [];
    this._timer = null;
    this._flushPromise = null;
    this._closePromise = null;
    this._lastFlushResult = EMPTY_RESULT;
    this._closing = false;
    this._closed = false;
  }

  _normalizeId(value) {
    return typeof value === "string" && value.trim() ? value : null;
  }

  _assertOpen() {
    if (this._closed || this._closing) throw new Error("Node SDK client is closed");
  }

  // bound: 可选的**脱敏后**收敛钩子。顺序不能反 —— 先截断会把敏感值从中间切开，
  // 留下正则匹配不到的半截秘密。
  _enqueue(path, payload, bound) {
    const sanitized = sanitizePayload(payload);
    this._queue.push({ path, payload: bound ? bound(sanitized) : sanitized });
    if (this._queue.length >= this._batchSize) {
      this._startAutomaticFlush();
    } else {
      this._scheduleTimer();
    }
  }

  _scheduleTimer() {
    if (this._timer !== null || this._queue.length === 0 || this._closed || this._closing) return;
    if (this._batchIntervalMs === 0) {
      this._startAutomaticFlush();
      return;
    }
    this._timer = setTimeout(() => {
      this._timer = null;
      this.flush().catch(() => {
        // The flush result carries the failed count; background reporting must not
        // create an unhandled rejection in the host application.
      });
    }, this._batchIntervalMs);
    if (typeof this._timer.unref === "function") this._timer.unref();
  }

  _startAutomaticFlush() {
    if (this._closed || this._closing || this._queue.length === 0) return;
    this.flush().catch(() => {
      // See the timer callback: explicit callers can still observe a rejection
      // from flush(), while automatic reporting stays isolated from host errors.
    });
  }

  _clearTimer() {
    if (this._timer !== null) {
      clearTimeout(this._timer);
      this._timer = null;
    }
  }

  reportError(error, extra) {
    this._assertOpen();
    const details = errorDetails(error);
    const payload = {
      exc_type: details.type,
      message: details.message,
      frames: details.frames,
      source: this._source,
      trace_id: this._traceId,
      session_id: this._sessionId,
    };
    if (this._release) {
      if (extra && typeof extra === "object" && !Array.isArray(extra)) {
        payload.extra = { ...extra, release: this._release };
      } else {
        payload.extra = { release: this._release };
        if (typeof extra !== "undefined") payload.extra.context = extra;
      }
    } else if (typeof extra !== "undefined") {
      payload.extra = extra;
    }
    this._enqueue("/ingest/error", payload);
    return {
      trace_id: this._traceId,
      session_id: this._sessionId,
      frame_count: details.frames.length,
    };
  }

  reportNetworkError(record) {
    this._assertOpen();
    if (!record || typeof record !== "object" || Array.isArray(record)) {
      throw new TypeError("network record must be an object");
    }
    const networkRecord = { ...record };
    if (typeof networkRecord.source === "undefined") networkRecord.source = this._source;
    this._enqueue(
      "/ingest/network",
      {
        record: networkRecord,
        trace_id: this._traceId,
        session_id: this._sessionId,
      },
      (payload) => ({ ...payload, record: boundNetworkRecord(payload.record) }),
    );
    return { trace_id: this._traceId, session_id: this._sessionId };
  }

  flush() {
    this._clearTimer();
    if (this._closed) return Promise.resolve(this._lastFlushResult);
    if (this._flushPromise) return this._flushPromise;

    const promise = this._flushQueue().then(
      (result) => {
        this._lastFlushResult = result;
        this._flushPromise = null;
        if (this._queue.length > 0 && !this._closing && !this._closed) {
          this._startAutomaticFlush();
        }
        return result;
      },
      (error) => {
        this._flushPromise = null;
        throw error;
      },
    );
    this._flushPromise = promise;
    return promise;
  }

  async _flushQueue() {
    const result = { sent: 0, failed: 0, batches: 0, attempts: 0 };
    while (this._queue.length > 0) {
      const batch = this._queue.splice(0, Math.min(this._batchSize, MAX_BATCH_SIZE));
      const outcome = await this._sendBatch(batch);
      result.batches += 1;
      result.attempts += outcome.attempts;
      result.sent += outcome.sent;
      result.failed += outcome.failed;
      // W15 / P3-SDK-4：_sendBatch 一直返回 status，但这里把它丢掉了 —— 调用方
      // 只能看到 failed>0，无法区分「服务端明确拒了（401 密钥错 / 413 体积超限 /
      // 5xx）」与「根本没连上」（status 缺席）。仅在**非 2xx** 时补这个键：
      // 2xx 但部分 item.ok=false 属业务级失败，把它记成 lastErrorStatus 会误导。
      if (
        typeof outcome.status === "number" &&
        (outcome.status < 200 || outcome.status >= 300)
      ) {
        result.lastErrorStatus = outcome.status;
      }
    }
    return result;
  }

  async _sendBatch(batch) {
    const body = JSON.stringify({ events: batch });
    let attempt = 0;

    while (true) {
      attempt += 1;
      let response = null;
      try {
        response = await this._sendRequest(body);
      } catch (_) {
        if (attempt > this._maxRetries) {
          return { sent: 0, failed: batch.length, attempts: attempt };
        }
      }

      if (response !== null) {
        const { status, payload } = response;
        if (status >= 200 && status < 300) {
          if (Array.isArray(payload?.results) && payload.results.length === batch.length) {
            const sent = payload.results.filter((item) => item?.ok === true).length;
            return { sent, failed: batch.length - sent, attempts: attempt, status };
          }
          return { sent: batch.length, failed: 0, attempts: attempt, status };
        }
        if (!isRetryableStatus(status) || attempt > this._maxRetries) {
          return { sent: 0, failed: batch.length, attempts: attempt, status };
        }
      }

      const exponentialDelay = this._retryDelayMs * 2 ** (attempt - 1);
      await waitFor(Math.min(this._maxRetryDelayMs, exponentialDelay));
    }
  }

  async _sendRequest(body) {
    const controller = new AbortController();
    let timeout = null;
    if (this._requestTimeoutMs > 0) {
      timeout = setTimeout(() => controller.abort(), this._requestTimeoutMs);
    }

    try {
      const headers = {
        Accept: "application/json",
        "Content-Type": "application/json",
      };
      if (this._apiKey) headers["X-API-Key"] = this._apiKey;
      const response = await this._fetch(this._batchUrl, {
        method: "POST",
        headers,
        body,
        signal: controller.signal,
      });
      if (!response || typeof response.status !== "number") {
        throw new TypeError("fetch returned an invalid response");
      }
      let payload = null;
      if (typeof response.text === "function") {
        try {
          const raw = await response.text();
          if (raw) payload = JSON.parse(raw);
        } catch (_) {
          // Headers prove the server accepted or rejected the request. A body
          // read/parse failure must not retry a non-idempotent accepted batch.
        }
      } else if (response.body && typeof response.body.cancel === "function") {
        try {
          await response.body.cancel();
        } catch (_) {
          // Best-effort cleanup only; status remains authoritative.
        }
      }
      return { status: response.status, payload };
    } finally {
      if (timeout !== null) clearTimeout(timeout);
    }
  }

  close() {
    if (this._closePromise) return this._closePromise;
    this._closing = true;
    this._clearTimer();
    this._closePromise = (async () => {
      try {
        return await this.flush();
      } finally {
        this._clearTimer();
        this._closed = true;
        this._closing = false;
      }
    })();
    return this._closePromise;
  }

  getSessionId() {
    return this._sessionId;
  }

  getTraceId() {
    return this._traceId;
  }

  setTraceId(traceId) {
    const normalized = this._normalizeId(traceId);
    if (!normalized) throw new TypeError("traceId must be a non-empty string");
    this._traceId = normalized;
    return this._traceId;
  }
}

function createClient(options) {
  return new NodeSdkClient(options);
}

let defaultClient = null;

function getDefaultClient() {
  if (!defaultClient) {
    defaultClient = createClient({
      endpoint: process.env.LUJO_MCP_ENDPOINT,
      apiKey: process.env.LUJO_MCP_API_KEY,
    });
  }
  return defaultClient;
}

function reportError(error, extra) {
  return getDefaultClient().reportError(error, extra);
}

function reportNetworkError(record) {
  return getDefaultClient().reportNetworkError(record);
}

function flush() {
  return defaultClient ? defaultClient.flush() : Promise.resolve(EMPTY_RESULT);
}

function close() {
  return defaultClient ? defaultClient.close() : Promise.resolve(EMPTY_RESULT);
}

function getSessionId() {
  return getDefaultClient().getSessionId();
}

function getTraceId() {
  return getDefaultClient().getTraceId();
}

function setTraceId(traceId) {
  return getDefaultClient().setTraceId(traceId);
}

module.exports = {
  createClient,
  reportError,
  reportNetworkError,
  flush,
  close,
  getSessionId,
  getTraceId,
  setTraceId,
};
