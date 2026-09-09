"use strict";

const crypto = require("node:crypto");

const MAX_BATCH_SIZE = 100;
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

  _enqueue(path, payload) {
    this._queue.push({ path, payload: sanitizePayload(payload) });
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
    if (typeof extra !== "undefined") payload.extra = extra;
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
    this._enqueue("/ingest/network", {
      record: networkRecord,
      trace_id: this._traceId,
      session_id: this._sessionId,
    });
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
      const batch = this._queue.splice(0, MAX_BATCH_SIZE);
      const outcome = await this._sendBatch(batch);
      result.batches += 1;
      result.attempts += outcome.attempts;
      if (outcome.ok) result.sent += batch.length;
      else result.failed += batch.length;
    }
    return result;
  }

  async _sendBatch(batch) {
    const body = JSON.stringify({ events: batch });
    let attempt = 0;

    while (true) {
      attempt += 1;
      let status = null;
      try {
        status = await this._sendRequest(body);
      } catch (_) {
        if (attempt > this._maxRetries) return { ok: false, attempts: attempt };
      }

      if (status !== null) {
        if (status >= 200 && status < 300) return { ok: true, attempts: attempt };
        if (!isRetryableStatus(status) || attempt > this._maxRetries) {
          return { ok: false, attempts: attempt, status };
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
      if (typeof response.text === "function") await response.text();
      return response.status;
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
