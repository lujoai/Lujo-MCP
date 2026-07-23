/**
 * ai-debug-mcp Browser SDK v0.2.0
 *
 * V2：批量上报 + sendBeacon 降级 + 指数退避重试。
 * 前端自动采集：全局异常捕获、网络请求记录、UI 事件上报、静默失败标记。
 * 无需构建工具，直接 <script> 引入或 import 使用。
 *
 * 用法：
 *   <script src="ai-debug.js"></script>
 *   <script>
 *     AiDebug.init({ endpoint: "http://localhost:8000" });
 *   </script>
 *
 * 或 ES module：
 *   import { init, reportSilentFailure } from "./ai-debug.js";
 *   init({ endpoint: "http://localhost:8000" });
 *
 * ── 路线图 ──
 * V3：网络错误自动标记静默失败
 *     - 对 5xx / 网络错误 / 超时自动生成 silent_failure 上报
 *     - 关联最近的 UI 事件链，减少手动 reportSilentFailure 调用
 *
 * V4：SDK 初始化追踪 + 请求关联
 *     - 初始化时生成 trace_id 并贯穿所有请求 header（X-Trace-Id）
 *     - 后端按 trace_id 关联 SDK 生命周期内的全部事件
 *
 * V5：增强 ingest 端点
 *     - /ingest/batch 支持按事件类型分组批量入库
 *     - 压缩传输（gzip / brotli）
 *     - 端点级 QoS（error 优先级 > network > ui）
 *
 * V6：自动检测 UI 静默失败
 *     - 基于用户行为序列（click → 无网络请求 → 无路由变更）自动推断
 *     - 配合 V3 网络错误标记，实现端到端静默失败自动发现
 */
(function (global) {
  "use strict";

  // ── 内置默认敏感键名列表（用户将 redactFields 设为空时回退使用） ──
  var _DEFAULT_REDACT_FIELDS = [
    "password", "token", "secret", "authorization",
    "cookie", "access_token", "api_key", "apikey",
    "passwd", "pwd", "private_key", "auth_token"
  ];

  // ── 配置 ──
  var cfg = {
    endpoint: "",
    apiKey: "",
    captureErrors: true,
    captureNetwork: true,
    captureUI: true,
    captureConsole: true,
    redactFields: ["password", "token", "secret", "authorization"],
    sampleRate: 1.0,
    networkSampleRate: 1.0,
    networkThrottleMs: 0,
    // reportSilentFailure 自动附加最近 N 条 network/UI 事件链
    silentFailureContextSize: 20,
    // V2 批量上报配置
    batchSize: 20,         // 队列满阈值，达到即 flush
    batchInterval: 1000,   // 定时 flush 间隔（ms）
    maxRetries: 3,         // XHR 失败最大重试次数
  };

  var _inited = false;
  var _sessionId = "sdk-" + Math.random().toString(36).slice(2, 10);
  var _traceId = "sdk-trace-" + Math.random().toString(36).slice(2, 10);

  // ── 静默失败上下文环形缓冲 ──
  // 仅存摘要（method/url/status/duration/timestamp/body 前 512 字符），完整 record 走实时上报
  var _recentNetwork = [];
  var _recentUI = [];
  // body 预览长度上限（约束 3 选项 A）
  var _NETWORK_BODY_PREVIEW = 512;

  // ── V2 批量上报状态 ──
  var _batchQueue = [];       // 批量事件队列：[{ path, payload }, ...]
  var _batchTimer = null;     // 定时 flush 定时器
  var _BEACON_SIZE_LIMIT = 65536; // sendBeacon 64KB 限制

  function _pushRecent(arr, item, maxSize) {
    arr.push(item);
    while (arr.length > maxSize) {
      arr.shift();
    }
  }

  function _summarizeNetworkRecord(record) {
    var body = record && record.request_body;
    if (body === null || body === undefined) {
      body = "";
    } else if (typeof body !== "string") {
      try { body = JSON.stringify(body); } catch (e) { body = String(body); }
    }
    return {
      method: record ? record.method : "",
      url: record ? record.url : "",
      status_code: record ? record.status_code : null,
      duration_ms: record ? record.duration_ms : null,
      timestamp: Date.now() / 1000,
      request_body_preview: body.slice(0, _NETWORK_BODY_PREVIEW),
      error: record && record.error ? record.error : null,
    };
  }

  // ── 工具函数 ──
  function _shouldSample() {
    return Math.random() < cfg.sampleRate;
  }

  function _getRedactFields() {
    return (cfg.redactFields && cfg.redactFields.length > 0) ? cfg.redactFields : _DEFAULT_REDACT_FIELDS;
  }

  function _redact(obj) {
    if (!obj || typeof obj !== "object") return obj;
    var out = Array.isArray(obj) ? [] : {};
    var fields = _getRedactFields();
    // Build lowercase lookup set for case-insensitive matching
    var lowerFields = [];
    for (var i = 0; i < fields.length; i++) {
      lowerFields.push(fields[i].toLowerCase());
    }
    for (var k in obj) {
      if (obj.hasOwnProperty(k)) {
        if (lowerFields.indexOf(k.toLowerCase()) >= 0) {
          out[k] = "***REDACTED***";
        } else if (typeof obj[k] === "string") {
          // Try to parse string as JSON for deep redaction
          try {
            var parsed = JSON.parse(obj[k]);
            if (parsed && typeof parsed === "object") {
              out[k] = JSON.stringify(_redact(parsed));
            } else {
              out[k] = obj[k];
            }
          } catch (e) {
            out[k] = obj[k];
          }
        } else if (typeof obj[k] === "object") {
          out[k] = _redact(obj[k]);
        } else {
          out[k] = obj[k];
        }
      }
    }
    return out;
  }

  // ── V2 批量上报 ──
  function _send(path, payload) {
    if (!cfg.endpoint || !_shouldSample()) return;
    var redacted = _redact(payload);
    _batchQueue.push({ path: path, payload: redacted });

    // 队列满 → 立即 flush
    if (_batchQueue.length >= cfg.batchSize) {
      _flushBatch(false);
    } else if (!_batchTimer) {
      // 启动定时 flush
      _batchTimer = setTimeout(function () {
        _flushBatch(false);
      }, cfg.batchInterval);
    }
  }

  function _hasSendBeacon() {
    return typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function";
  }

  function _flushBatch(useBeacon) {
    if (_batchQueue.length === 0 || !cfg.endpoint) return;

    if (_batchTimer) {
      clearTimeout(_batchTimer);
      _batchTimer = null;
    }

    var batch = _batchQueue.splice(0, _batchQueue.length);
    var body = JSON.stringify({ events: batch });
    var url = cfg.endpoint.replace(/\/+$/, "") + "/ingest/batch";

    // 页面关闭场景：优先 sendBeacon
    if (useBeacon && _hasSendBeacon()) {
      if (body.length <= _BEACON_SIZE_LIMIT) {
        var beaconUrl = url;
        if (cfg.apiKey) {
          beaconUrl += "?api_key=" + encodeURIComponent(cfg.apiKey);
        }
        var blob = new Blob([body], { type: "application/json" });
        if (navigator.sendBeacon(beaconUrl, blob)) {
          return; // sendBeacon 成功
        }
      }
      // 超限或 sendBeacon 失败 → 同步 XHR 降级
      _sendBatchSync(url, body);
      return;
    }

    // 常规 flush：异步 XHR + 指数退避重试
    _sendBatchXhr(url, body, 0);
  }

  function _sendBatchXhr(url, body, attempt) {
    try {
      var xhr = new XMLHttpRequest();
      xhr.open("POST", url, true);
      xhr.setRequestHeader("Content-Type", "application/json");
      if (cfg.apiKey) xhr.setRequestHeader("X-API-Key", cfg.apiKey);
      xhr.onreadystatechange = function () {
        if (xhr.readyState !== 4) return;
        if (xhr.status >= 200 && xhr.status < 300) return; // 成功
        // 失败 → 重试
        if (attempt < cfg.maxRetries) {
          var delay = 500 * Math.pow(2, attempt); // 500 → 1000 → 2000
          setTimeout(function () {
            _sendBatchXhr(url, body, attempt + 1);
          }, delay);
        }
        // 超过重试次数 → 丢弃，避免无限堆积
      };
      xhr.send(body);
    } catch (e) {
      if (attempt < cfg.maxRetries) {
        var delay = 500 * Math.pow(2, attempt);
        setTimeout(function () {
          _sendBatchXhr(url, body, attempt + 1);
        }, delay);
      }
    }
  }

  function _sendBatchSync(url, body) {
    try {
      var xhr = new XMLHttpRequest();
      xhr.open("POST", url, false); // 同步
      xhr.setRequestHeader("Content-Type", "application/json");
      if (cfg.apiKey) xhr.setRequestHeader("X-API-Key", cfg.apiKey);
      xhr.send(body);
    } catch (e) {
      // 页面关闭时同步 XHR 失败，无法重试
    }
  }

  function _installPageHideHook() {
    function _onVisibilityChange() {
      if (document.hidden) {
        _flushBatch(true);
      }
    }

    function _onPageHide() {
      _flushBatch(true);
    }

    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", _onVisibilityChange);
    }
    if (typeof global !== "undefined") {
      global.addEventListener("pagehide", _onPageHide);
    }
  }

  function _isSelfRequest(url) {
    if (!cfg.endpoint) return false;
    url = String(url || "");
    if (!url) return false;
    var endpoint = cfg.endpoint.replace(/\/+$/, "");
    return url.indexOf(endpoint) === 0;
  }

  // ── 全局异常捕获 ──
  function _installErrorHook() {
    // window.onerror → 同步异常
    var _onerror = global.onerror;
    global.onerror = function (msg, file, line, col, error) {
      var frames = [];
      if (error && error.stack) {
        frames = _parseStack(error.stack);
      } else {
        frames = [{ file: file || "", line: line || 0, function: "" }];
      }
      _send("/ingest/error", {
        exc_type: error ? error.name : "Error",
        message: String(msg),
        frames: frames,
        trace_id: _traceId,
        source: "browser-sdk",
        extra: {
          session_id: _sessionId,
          url: global.location ? global.location.href : "",
          user_agent: navigator ? navigator.userAgent : "",
        },
      });
      if (_onerror) _onerror.apply(this, arguments);
    };

    // unhandledrejection → Promise 未捕获
    global.addEventListener("unhandledrejection", function (e) {
      var reason = e.reason;
      _send("/ingest/error", {
        exc_type: reason ? reason.name || "UnhandledRejection" : "UnhandledRejection",
        message: reason ? reason.message || String(reason) : "Promise rejected",
        frames: reason && reason.stack ? _parseStack(reason.stack) : [],
        trace_id: _traceId,
        source: "browser-sdk",
        extra: {
          session_id: _sessionId,
          url: global.location ? global.location.href : "",
        },
      });
    });
  }

  function _parseStack(stack) {
    if (!stack) return [];
    return stack
      .split("\n")
      .slice(1)
      .filter(function (line) { return line.trim(); })
      .map(function (line) {
        var m = line.trim().match(/at\s+(.*?)\s+\(?(.+?):(\d+):(\d+)?\)?/);
        if (m) return { file: m[2], line: parseInt(m[3]) || 0, function: m[1] || "" };
        // Chrome format: at file:line:col
        var m2 = line.trim().match(/at\s+(.+?):(\d+):(\d+)/);
        if (m2) return { file: m2[1], line: parseInt(m2[2]) || 0, function: "" };
        return { file: "", line: 0, function: line.trim() };
      });
  }

  // ── 网络请求拦截 ──
  var _networkThrottle = {};
  var _onNetworkCapture = null;

  function _shouldSampleNetwork() {
    return Math.random() < cfg.networkSampleRate;
  }

  function _shouldThrottle(method, url) {
    if (cfg.networkThrottleMs <= 0) return false;
    var key = method.toUpperCase() + ":" + url;
    var now = Date.now();
    if (_networkThrottle[key] && now - _networkThrottle[key] < cfg.networkThrottleMs) {
      return true;
    }
    _networkThrottle[key] = now;
    _cleanupNetworkThrottle();
    return false;
  }

  function _cleanupNetworkThrottle() {
    var maxSize = 1000;
    var keys = Object.keys(_networkThrottle);
    if (keys.length > maxSize) {
      var sortedKeys = keys.sort(function(a, b) {
        return _networkThrottle[a] - _networkThrottle[b];
      });
      var removeCount = keys.length - maxSize;
      for (var i = 0; i < removeCount; i++) {
        delete _networkThrottle[sortedKeys[i]];
      }
    }
  }

  function _notifyNetworkCapture(record) {
    if (_onNetworkCapture && typeof _onNetworkCapture === 'function') {
      try {
        _onNetworkCapture(record);
      } catch (e) {
      }
    }
  }

  function _serializeRequestBody(body) {
    if (body === null || body === undefined) {
      return null;
    }
    if (typeof body === 'string') {
      return body;
    }
    if (typeof URLSearchParams !== 'undefined' && body instanceof URLSearchParams) {
      try {
        return body.toString();
      } catch (e) {
        return '[URLSearchParams]';
      }
    }
    if (typeof FormData !== 'undefined' && body instanceof FormData) {
      try {
        var params = [];
        body.forEach(function(value, key) {
          params.push(key + '=' + value);
        });
        return params.join('&');
      } catch (e) {
        return '[FormData]';
      }
    }
    if (typeof Blob !== 'undefined' && body instanceof Blob) {
      return '[Blob: ' + body.type + ', ' + body.size + ' bytes]';
    }
    if (typeof ArrayBuffer !== 'undefined' && body instanceof ArrayBuffer) {
      return '[ArrayBuffer: ' + body.byteLength + ' bytes]';
    }
    if (typeof body === 'object') {
      try {
        return JSON.stringify(body);
      } catch (e) {
        return '[object]';
      }
    }
    return String(body);
  }

  function _reportNetworkRecord(record) {
    try {
      // 摘要入环形缓冲，供 reportSilentFailure 拼装 observed_events
      _pushRecent(_recentNetwork, _summarizeNetworkRecord(record), cfg.silentFailureContextSize);
      _notifyNetworkCapture(record);
      _send("/ingest/network", {
        record: record,
        trace_id: _traceId,
        source: "browser-sdk",
        extra: { session_id: _sessionId },
      });
    } catch (e) {
    }
  }

  function _installNetworkHook() {
    if (!cfg.captureNetwork) return;

    var _origFetch = global.fetch;
    if (_origFetch) {
      global.fetch = function () {
        var args = arguments;
        var rawUrl = args[0];
        var url = "";
        if (typeof rawUrl === "string") {
          url = rawUrl;
        } else if (rawUrl && typeof rawUrl === "object") {
          url = rawUrl.url || rawUrl.href || String(rawUrl);
        }

        if (_isSelfRequest(url)) {
          return _origFetch.apply(this, args);
        }

        var method = (args[1] && args[1].method) || "GET";

        if (!_shouldSampleNetwork()) {
          return _origFetch.apply(this, args);
        }

        if (_shouldThrottle(method, url)) {
          return _origFetch.apply(this, args);
        }

        var start = Date.now();
        var reqBody = _serializeRequestBody((args[1] && args[1].body) || null);

        return _origFetch.apply(this, args).then(function (res) {
          var clone = res.clone();
          clone.text().then(function (text) {
            var record = {
              url: url,
              method: method.toUpperCase(),
              status_code: res.status,
              duration_ms: Date.now() - start,
              request_body: reqBody,
              response_body: text.slice(0, 2000),
            };
            _reportNetworkRecord(record);
          }).catch(function () {});
          return res;
        }).catch(function (err) {
          var record = {
            url: url,
            method: method.toUpperCase(),
            status_code: 0,
            error: err.message,
            duration_ms: Date.now() - start,
            request_body: reqBody,
          };
          _reportNetworkRecord(record);
          throw err;
        });
      };
    }
  }

  // ── XMLHttpRequest 拦截 ──
  function _installXhrHook() {
    if (!cfg.captureNetwork) return;

    var _origXhrOpen = XMLHttpRequest.prototype.open;
    var _origXhrSend = XMLHttpRequest.prototype.send;

    XMLHttpRequest.prototype.open = function () {
      var args = arguments;
      this._aiDebugMethod = (args[0] || "GET").toUpperCase();
      this._aiDebugUrl = args[1] || "";
      this._aiDebugSkip = _isSelfRequest(this._aiDebugUrl);
      return _origXhrOpen.apply(this, args);
    };

    XMLHttpRequest.prototype.send = function () {
      var args = arguments;
      var xhr = this;

      if (xhr._aiDebugSkip) {
        return _origXhrSend.apply(xhr, args);
      }

      var method = xhr._aiDebugMethod || "GET";
      var url = xhr._aiDebugUrl || "";

      if (!_shouldSampleNetwork()) {
        return _origXhrSend.apply(xhr, args);
      }

      if (_shouldThrottle(method, url)) {
        return _origXhrSend.apply(xhr, args);
      }

      var start = Date.now();
      var reqBody = _serializeRequestBody(args[0] || null);

      function _onLoad() {
        try {
          var responseText = "";
          try {
            responseText = xhr.responseText ? xhr.responseText.slice(0, 2000) : "";
          } catch (e) {
            responseText = "";
          }
          var record = {
            url: url,
            method: method.toUpperCase(),
            status_code: xhr.status,
            duration_ms: Date.now() - start,
            request_body: reqBody,
            response_body: responseText,
          };
          _reportNetworkRecord(record);
        } catch (e) {
        }
      }

      function _onError() {
        try {
          var record = {
            url: url,
            method: method.toUpperCase(),
            status_code: 0,
            error: "XHR error",
            duration_ms: Date.now() - start,
            request_body: reqBody,
          };
          _reportNetworkRecord(record);
        } catch (e) {
        }
      }

      function _onAbort() {
        try {
          var record = {
            url: url,
            method: method.toUpperCase(),
            status_code: 0,
            error: "XHR aborted",
            duration_ms: Date.now() - start,
            request_body: reqBody,
          };
          _reportNetworkRecord(record);
        } catch (e) {
        }
      }

      function _onTimeout() {
        try {
          var record = {
            url: url,
            method: method.toUpperCase(),
            status_code: 0,
            error: "XHR timeout",
            duration_ms: Date.now() - start,
            request_body: reqBody,
          };
          _reportNetworkRecord(record);
        } catch (e) {
        }
      }

      try {
        xhr.addEventListener("load", _onLoad);
        xhr.addEventListener("error", _onError);
        xhr.addEventListener("abort", _onAbort);
        xhr.addEventListener("timeout", _onTimeout);
      } catch (e) {
      }

      return _origXhrSend.apply(xhr, args);
    };
  }

  // ── UI 事件捕获 ──
  function _installUIHook() {
    if (!cfg.captureUI) return;

    var events = ["click", "input", "change", "submit"];
    var _debounce = {};

    events.forEach(function (evt) {
      document.addEventListener(evt, function (e) {
        // 去重：同一秒内同一元素同类事件只报一次
        var target = e.target;
        if (!target) return;
        var key = evt + ":" + (target.id || target.className || target.tagName);
        var now = Date.now();
        if (_debounce[key] && now - _debounce[key] < 1000) return;
        _debounce[key] = now;

        var uiEvent = {
          event_type: e.type,
          target_selector: _getSelector(target),
          target_text: (target.textContent || "").slice(0, 100),
          timestamp: now / 1000,
          route_path: global.location ? global.location.pathname : "",
        };
        // 入环形缓冲，供 reportSilentFailure 拼装 observed_events
        _pushRecent(_recentUI, uiEvent, cfg.silentFailureContextSize);

        _send("/ingest/ui-event", {
          event: uiEvent,
          trace_id: _traceId,
          source: "browser-sdk",
          extra: { session_id: _sessionId },
        });
      }, true);
    });
  }

  function _getSelector(el) {
    if (!el) return "";
    if (el.id) return "#" + el.id;
    if (el.className && typeof el.className === "string") {
      return el.tagName.toLowerCase() + "." + el.className.split(" ").join(".");
    }
    return el.tagName ? el.tagName.toLowerCase() : "";
  }

  // ── 控制台日志捕获 ──
  var _consoleHookInstalled = false;

  function _installConsoleHook() {
    if (!cfg.captureConsole || _consoleHookInstalled) return;
    _consoleHookInstalled = true;

    var _origError = global.console.error;
    if (_origError) {
      global.console.error = function () {
        _sendConsole("error", Array.prototype.slice.call(arguments));
        _origError.apply(global.console, arguments);
      };
    }

    var _origWarn = global.console.warn;
    if (_origWarn) {
      global.console.warn = function () {
        _sendConsole("warn", Array.prototype.slice.call(arguments));
        _origWarn.apply(global.console, arguments);
      };
    }
  }

  function _sendConsole(level, args) {
    var messages = [];
    for (var i = 0; i < args.length; i++) {
      var arg = args[i];
      try {
        if (typeof arg === "object") {
          messages.push(JSON.stringify(_redact(arg)));
        } else {
          messages.push(String(arg));
        }
      } catch (e) {
        messages.push("[object]");
      }
    }
    _send("/ingest/console", {
      level: level,
      message: messages.join(" "),
      trace_id: _traceId,
      source: "browser-sdk",
      extra: {
        session_id: _sessionId,
        url: global.location ? global.location.href : "",
      },
    });
  }

  // ── 公开 API ──
  /**
   * 初始化 SDK
   * @param {object} opts - { endpoint, apiKey, captureErrors, captureNetwork, captureUI, captureConsole, sampleRate, networkSampleRate, networkThrottleMs, silentFailureContextSize, batchSize, batchInterval, maxRetries }
   */
  function init(opts) {
    if (_inited) return;
    if (opts) {
      for (var k in opts) {
        if (opts.hasOwnProperty(k) && cfg.hasOwnProperty(k)) {
          cfg[k] = opts[k];
        }
      }
    }
    // 空数组回退到内置默认列表，防止关闭脱敏
    if (!cfg.redactFields || (Array.isArray(cfg.redactFields) && cfg.redactFields.length === 0)) {
      cfg.redactFields = _DEFAULT_REDACT_FIELDS;
    }
    if (!cfg.endpoint) {
      console.warn("[ai-debug] endpoint 未配置，SDK 不上报");
      return;
    }
    _inited = true;
    _installErrorHook();
    _installNetworkHook();
    _installXhrHook();
    _installUIHook();
    _installConsoleHook();
    _installPageHideHook();
    console.log("[ai-debug] SDK initialized, session=" + _sessionId);
  }

  /**
   * 手动上报静默失败
   *
   * 自动从环形缓冲取出最近 N 条 network/UI 事件（N = cfg.silentFailureContextSize，默认 20）
   * 拼装为 observed_events 数组与 trace_id 一起上报，服务端会按 kind 分类入库，
   * 保证 AI 调试时通过 get_debug_context 能拿到完整事件链。
   *
   * @param {object} payload
   * @param {string} payload.description - 静默失败描述（必填）
   * @param {string} [payload.observed] - 用户对现象的文字描述，如"点击后无反应"
   * @param {object} [payload.expected] - 期望行为，如 {type:"route_change", to:"/done"}
   * @param {string} [payload.route] - 当前路由（可选，默认取 location.pathname）
   *
   * observed_events 元素结构（SDK 自动附加，非用户传入）：
   *   {
   *     kind: "network" | "ui",   // 事件类型
   *     data: { ... }              // network 摘要或 UI 事件原始结构
   *   }
   * - kind="network" 时 data 形如：
   *     { method, url, status_code, duration_ms, timestamp, request_body_preview, error }
   * - kind="ui" 时 data 形如：
   *     { event_type, target_selector, target_text, timestamp, route_path }
   */
  function reportSilentFailure(payload) {
    payload = payload || {};
    var observedEvents = [];
    for (var i = 0; i < _recentNetwork.length; i++) {
      observedEvents.push({ kind: "network", data: _recentNetwork[i] });
    }
    for (var j = 0; j < _recentUI.length; j++) {
      observedEvents.push({ kind: "ui", data: _recentUI[j] });
    }
    _send("/ingest/silent-failure", {
      message: payload.description,
      expectation: payload.expected,
      observed: payload.observed,
      observed_events: observedEvents,
      trace_id: _traceId,
      source: "browser-sdk",
      extra: {
        session_id: _sessionId,
        url: global.location ? global.location.href : "",
      },
    });
  }

  /**
   * 手动上报异常
   * @param {Error} error
   * @param {object} extra
   */
  function reportError(error, extra) {
    _send("/ingest/error", {
      exc_type: error ? error.name || "Error" : "Error",
      message: error ? error.message || String(error) : "",
      frames: error && error.stack ? _parseStack(error.stack) : [],
      trace_id: _traceId,
      source: "browser-sdk",
      extra: Object.assign({
        session_id: _sessionId,
        url: global.location ? global.location.href : "",
      }, extra || {}),
    });
  }

  /**
   * 手动上报 UI 事件
   * @param {object} event - { event_type, target_selector, route_path }
   */
  function reportUIEvent(event) {
    _send("/ingest/ui-event", {
      event: {
        event_type: event.event_type || "click",
        target_selector: event.target_selector || "",
        target_text: event.target_text || "",
        timestamp: Date.now() / 1000,
        route_path: event.route_path || (global.location ? global.location.pathname : ""),
      },
      trace_id: _traceId,
      source: "browser-sdk",
      extra: { session_id: _sessionId },
    });
  }

  /**
   * 获取当前会话 ID
   */
  function getSessionId() {
    return _sessionId;
  }

  /**
   * 获取当前追踪 ID
   */
  function getTraceId() {
    return _traceId;
  }

  /**
   * 设置追踪 ID（用于关联不同上报到同一业务操作）
   * @param {string} id - 新的追踪 ID
   */
  function setTraceId(id) {
    if (id) _traceId = id;
  }

  /**
   * 手动 flush 批量队列（用于测试或需要立即上报的场景）
   */
  function flush() {
    _flushBatch(false);
  }

  // ── 导出 ──
  var api = {
    init: init,
    flush: flush,
    reportSilentFailure: reportSilentFailure,
    reportError: reportError,
    reportUIEvent: reportUIEvent,
    getSessionId: getSessionId,
    getTraceId: getTraceId,
    setTraceId: setTraceId,
    _cfg: cfg,
    onNetworkCapture: function (callback) {
      _onNetworkCapture = callback;
    },
  };

  // 支持 CommonJS / ES module / 全局变量
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else if (typeof define === "function" && define.amd) {
    define(function () { return api; });
  } else {
    global.AiDebug = api;
  }
})(typeof window !== "undefined" ? window : this);
