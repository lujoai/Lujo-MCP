/**
 * ai-debug-mcp Browser SDK v0.5.0
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
 * V5：增强 ingest 端点 + 传输优化
 *     - /ingest/batch 支持按事件类型分组批量入库
 *     - 压缩传输（gzip，payload > 4KB 时自动启用）
 *     - 节流控制（5秒内最多2批，防止高频上报）
 *     - 失败降级（超过重试次数后暂存 localStorage，下次启动重试）
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
    autoDetectNetworkErrors: true,
    autoDetectUISilentFailures: true,
    uiSilentFailureTimeoutMs: 1800,
    uiSilentFailureObserveSelector: "body",
    // reportSilentFailure 自动附加最近 N 条 network/UI 事件链
    silentFailureContextSize: 20,
    // V2 批量上报配置
    batchSize: 20,         // 队列满阈值，达到即 flush
    batchInterval: 1000,   // 定时 flush 间隔（ms）
    maxRetries: 3,         // XHR 失败最大重试次数
    // V5 传输优化配置
    enableCompression: true,           // 是否启用 gzip 压缩
    compressionThreshold: 4096,        // 压缩阈值（字节），payload > 4KB 时自动压缩
    throttleWindowMs: 5000,            // 节流窗口（ms）
    maxBatchesPerWindow: 2,            // 每个节流窗口内最多发送批次数
    enableLocalStorageFallback: true,  // 超过重试次数后是否暂存 localStorage
    localStorageKey: "ai-debug-pending-batches",  // localStorage 键名
    maxPendingBatches: 10,             // 最多暂存的批次数
  };

  var _inited = false;
  var _sessionId = "sdk-" + Math.random().toString(36).slice(2, 10);
  var _traceId = "sdk-trace-" + Math.random().toString(36).slice(2, 10);

  // ── 静默失败上下文环形缓冲 ──
  // 仅存摘要（method/url/status/duration/timestamp/body 前 512 字符），完整 record 走实时上报
  var _recentNetwork = [];
  var _recentUI = [];
  var _pendingUISilentFailure = null;
  var _uiSilentFailureTimer = null;
  var _uiMutationObserver = null;
  var _lastDomMutationAt = 0;
  var _lastRoutePath = "";
  // body 预览长度上限（约束 3 选项 A）
  var _NETWORK_BODY_PREVIEW = 512;

  // ── V2 批量上报状态 ──
  var _batchQueue = [];       // 批量事件队列：[{ path, payload }, ...]
  var _batchTimer = null;     // 定时 flush 定时器
  var _BEACON_SIZE_LIMIT = 65536; // sendBeacon 64KB 限制
  var _onSilentFailureReport = null;

  // ── V5 传输优化状态 ──
  var _batchTimestamps = [];  // 记录每次发送的时间戳，用于节流控制
  var _pendingBatches = [];   // 待发送的批次（节流延迟时暂存）

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
      timestamp: _nowSeconds(),
      request_body_preview: body.slice(0, _NETWORK_BODY_PREVIEW),
      error: !!(record && record.error),
      error_message: record && record.error ? String(record.error) : null,
    };
  }

  // ── 工具函数 ──
  function _shouldSample() {
    return Math.random() < cfg.sampleRate;
  }

  function _getRedactFields() {
    return (cfg.redactFields && cfg.redactFields.length > 0) ? cfg.redactFields : _DEFAULT_REDACT_FIELDS;
  }

  function _escapeRegex(str) {
    return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function _nowSeconds() {
    return Date.now() / 1000;
  }

  function _redactString(value) {
    if (typeof value !== "string" || !value) return value;
    var text = value;
    var fields = _getRedactFields();
    for (var i = 0; i < fields.length; i++) {
      var field = fields[i];
      var escaped = _escapeRegex(field);
      var queryPattern = new RegExp("([?&]" + escaped + "=)([^&#]+)", "ig");
      var kvPattern = new RegExp("(" + escaped + "\\s*[:=]\\s*)([^\\s,;&]+)", "ig");
      text = text.replace(queryPattern, "$1***REDACTED***");
      text = text.replace(kvPattern, "$1***REDACTED***");
    }
    text = text.replace(/(authorization\s*[:=]\s*bearer\s+)([^\s,;]+)/ig, "$1***REDACTED***");
    return text;
  }

  function _redact(obj) {
    if (typeof obj === "string") return _redactString(obj);
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
              out[k] = _redactString(obj[k]);
            }
          } catch (e) {
            out[k] = _redactString(obj[k]);
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

    // V5 节流控制：检查是否在节流窗口内
    var now = Date.now();
    _batchTimestamps = _batchTimestamps.filter(function(ts) {
      return now - ts < cfg.throttleWindowMs;
    });
    
    if (_batchTimestamps.length >= cfg.maxBatchesPerWindow) {
      // 超过节流限制，延迟到窗口结束后发送
      var delay = cfg.throttleWindowMs - (now - _batchTimestamps[0]);
      _pendingBatches.push(body);
      setTimeout(function() {
        var pendingBody = _pendingBatches.shift();
        if (pendingBody) {
          _sendBatchWithCompression(url, pendingBody, useBeacon);
        }
      }, delay);
      return;
    }

    _sendBatchWithCompression(url, body, useBeacon);
  }

  // V5 压缩传输：根据 payload 大小决定是否压缩
  function _sendBatchWithCompression(url, body, useBeacon) {
    var shouldCompress = cfg.enableCompression && 
                         body.length > cfg.compressionThreshold &&
                         typeof CompressionStream !== "undefined";

    if (shouldCompress) {
      _compressAndSend(url, body, useBeacon);
    } else {
      _sendBatchDirect(url, body, useBeacon);
    }
  }

  // V5 gzip 压缩实现（使用 Compression Streams API）
  function _compressAndSend(url, body, useBeacon) {
    try {
      var blob = new Blob([body]);
      var cs = new CompressionStream("gzip");
      var compressedStream = blob.stream().pipeThrough(cs);
      
      new Response(compressedStream).blob().then(function(compressedBlob) {
        var reader = new FileReader();
        reader.onload = function() {
          var compressedBody = reader.result;
          _batchTimestamps.push(Date.now());
          
          // 页面关闭场景：优先 sendBeacon
          if (useBeacon && _hasSendBeacon()) {
            if (compressedBody.byteLength <= _BEACON_SIZE_LIMIT) {
              var beaconUrl = url;
              if (cfg.apiKey) {
                beaconUrl += "?api_key=" + encodeURIComponent(cfg.apiKey);
              }
              var beaconBlob = new Blob([compressedBody], { type: "application/json" });
              if (navigator.sendBeacon(beaconUrl, beaconBlob)) {
                return; // sendBeacon 成功
              }
            }
            // 超限或 sendBeacon 失败 → 同步 XHR 降级
            _sendBatchSyncCompressed(url, compressedBody);
            return;
          }

          // 常规 flush：异步 XHR + 指数退避重试
          _sendBatchXhrCompressed(url, compressedBody, 0);
        };
        reader.readAsArrayBuffer(compressedBlob);
      }).catch(function(err) {
        // 压缩失败，降级为未压缩发送
        console.warn("[ai-debug] Compression failed, falling back to uncompressed:", err);
        _sendBatchDirect(url, body, useBeacon);
      });
    } catch (e) {
      // CompressionStream 不可用，降级为未压缩发送
      _sendBatchDirect(url, body, useBeacon);
    }
  }

  // 未压缩直接发送
  function _sendBatchDirect(url, body, useBeacon) {
    _batchTimestamps.push(Date.now());
    
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
        } else if (cfg.enableLocalStorageFallback) {
          // V5 失败降级：超过重试次数后暂存 localStorage
          _saveToLocalStorage(body);
        }
      };
      xhr.send(body);
    } catch (e) {
      if (attempt < cfg.maxRetries) {
        var delay = 500 * Math.pow(2, attempt);
        setTimeout(function () {
          _sendBatchXhr(url, body, attempt + 1);
        }, delay);
      } else if (cfg.enableLocalStorageFallback) {
        _saveToLocalStorage(body);
      }
    }
  }

  // V5 压缩版本的 XHR 发送
  function _sendBatchXhrCompressed(url, compressedBody, attempt) {
    try {
      var xhr = new XMLHttpRequest();
      xhr.open("POST", url, true);
      xhr.setRequestHeader("Content-Type", "application/json");
      xhr.setRequestHeader("Content-Encoding", "gzip");
      if (cfg.apiKey) xhr.setRequestHeader("X-API-Key", cfg.apiKey);
      xhr.onreadystatechange = function () {
        if (xhr.readyState !== 4) return;
        if (xhr.status >= 200 && xhr.status < 300) return; // 成功
        // 失败 → 重试
        if (attempt < cfg.maxRetries) {
          var delay = 500 * Math.pow(2, attempt);
          setTimeout(function () {
            _sendBatchXhrCompressed(url, compressedBody, attempt + 1);
          }, delay);
        } else if (cfg.enableLocalStorageFallback) {
          // 压缩版本失败降级：转为未压缩存储
          var textDecoder = new TextDecoder();
          var text = textDecoder.decode(compressedBody);
          _saveToLocalStorage(text);
        }
      };
      xhr.send(compressedBody);
    } catch (e) {
      if (attempt < cfg.maxRetries) {
        var delay = 500 * Math.pow(2, attempt);
        setTimeout(function () {
          _sendBatchXhrCompressed(url, compressedBody, attempt + 1);
        }, delay);
      } else if (cfg.enableLocalStorageFallback) {
        var textDecoder = new TextDecoder();
        var text = textDecoder.decode(compressedBody);
        _saveToLocalStorage(text);
      }
    }
  }

  // V5 压缩版本的同步 XHR 发送（页面关闭场景）
  function _sendBatchSyncCompressed(url, compressedBody) {
    try {
      var xhr = new XMLHttpRequest();
      xhr.open("POST", url, false); // 同步
      xhr.setRequestHeader("Content-Type", "application/json");
      xhr.setRequestHeader("Content-Encoding", "gzip");
      if (cfg.apiKey) xhr.setRequestHeader("X-API-Key", cfg.apiKey);
      xhr.send(compressedBody);
    } catch (e) {
      // 页面关闭时同步 XHR 失败，无法重试
    }
  }

  // V5 失败降级：暂存到 localStorage
  function _saveToLocalStorage(body) {
    try {
      if (typeof localStorage === "undefined") return;
      
      var pending = [];
      var stored = localStorage.getItem(cfg.localStorageKey);
      if (stored) {
        try {
          pending = JSON.parse(stored);
        } catch (e) {
          pending = [];
        }
      }
      
      // 限制暂存数量
      if (pending.length >= cfg.maxPendingBatches) {
        pending.shift(); // 移除最旧的批次
      }
      
      pending.push(body);
      localStorage.setItem(cfg.localStorageKey, JSON.stringify(pending));
    } catch (e) {
      // localStorage 不可用或已满，静默失败
    }
  }

  // V5 启动时恢复暂存的批次
  function _restorePendingBatches() {
    try {
      if (typeof localStorage === "undefined") return;
      
      var stored = localStorage.getItem(cfg.localStorageKey);
      if (!stored) return;
      
      var pending = [];
      try {
        pending = JSON.parse(stored);
      } catch (e) {
        return;
      }
      
      // 清空 localStorage
      localStorage.removeItem(cfg.localStorageKey);
      
      // 逐个重新发送
      pending.forEach(function(body) {
        _batchQueue.push(JSON.parse(body));
      });
      
      // 立即 flush
      if (_batchQueue.length > 0) {
        _flushBatch(false);
      }
    } catch (e) {
      // 恢复失败，静默处理
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
      if (record && record.url) {
        record.url = _redact(record.url);
      }
      if (record && record.request_body) {
        record.request_body = _redact(record.request_body);
      }
      if (record && record.response_body) {
        record.response_body = _redact(record.response_body);
      }
      if (record && record.error) {
        record.error = _redact(String(record.error));
      }
      if (_pendingUISilentFailure) {
        _pendingUISilentFailure.sawNetwork = true;
        _pendingUISilentFailure.lastNetworkAt = _nowSeconds();
      }
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

  function _normalizeNetworkError(input) {
    var payload = {};
    if (input instanceof Error) {
      payload.error = input.message || String(input);
    } else if (input && typeof input === "object") {
      for (var key in input) {
        if (input.hasOwnProperty(key)) {
          payload[key] = input[key];
        }
      }
    } else if (input !== undefined && input !== null) {
      payload.error = String(input);
    }

    payload.method = (payload.method || "GET").toUpperCase();
    payload.url = _redact(payload.url || (global.location ? global.location.href : ""));
    payload.status_code = typeof payload.status_code === "number"
      ? payload.status_code
      : (typeof payload.status === "number" ? payload.status : 0);
    payload.duration_ms = typeof payload.duration_ms === "number" ? payload.duration_ms : 0;
    payload.request_body = payload.request_body === undefined ? null : _redact(payload.request_body);
    payload.response_body = payload.response_body === undefined ? null : _redact(payload.response_body);
    payload.error = _redact(payload.error || payload.error_message || "Network error");
    return payload;
  }

  function _buildNetworkErrorDescription(record) {
    var method = record && record.method ? record.method : "GET";
    var url = record && record.url ? record.url : "unknown";
    var status = record && typeof record.status_code === "number" ? record.status_code : 0;
    var detail = record && record.error ? String(record.error) : "network request failed";
    return method + " " + url + " failed (" + status + "): " + detail;
  }

  function _autoReportNetworkError(record) {
    if (!cfg.autoDetectNetworkErrors) return;
    reportSilentFailure({
      description: _buildNetworkErrorDescription(record),
      observed: "Detected by Browser SDK V3 network error auto-report",
      expected: {
        type: "network_success",
        status_code: "2xx",
      },
      route: global.location ? global.location.pathname : "",
    });
  }

  function _getTextPreview(node) {
    if (!node) return "";
    var text = "";
    if (typeof node.textContent === "string") {
      text = node.textContent;
    } else {
      text = String(node);
    }
    return text.replace(/\s+/g, " ").trim().slice(0, 160);
  }

  function _captureDomSnapshot(target) {
    var element = target && target.nodeType === 1 ? target : (document && document.body ? document.body : null);
    if (!element) return null;
    return {
      selector: _getSelector(element),
      text_preview: _getTextPreview(element),
      route_path: global.location ? global.location.pathname : "",
      timestamp: _nowSeconds(),
    };
  }

  function _cancelPendingUISilentFailure() {
    if (_uiSilentFailureTimer) {
      clearTimeout(_uiSilentFailureTimer);
      _uiSilentFailureTimer = null;
    }
    _pendingUISilentFailure = null;
  }

  function _markUISilentFailureDomChange(target) {
    if (!_pendingUISilentFailure) return;
    _pendingUISilentFailure.domChanged = true;
    _pendingUISilentFailure.domSnapshot = _captureDomSnapshot(target || document.body);
  }

  function _buildUISilentFailureDescription(pending) {
    var selector = pending && pending.selector ? pending.selector : "unknown target";
    var route = pending && pending.routePath ? pending.routePath : "/";
    return "UI interaction " + pending.eventType + " on " + selector + " produced no visible change on " + route;
  }

  function _scheduleUISilentFailureCheck() {
    if (!_pendingUISilentFailure) return;
    if (_uiSilentFailureTimer) {
      clearTimeout(_uiSilentFailureTimer);
    }
    _uiSilentFailureTimer = setTimeout(function () {
      if (!_pendingUISilentFailure) return;
      var pending = _pendingUISilentFailure;
      _uiSilentFailureTimer = null;
      var routeChanged = (global.location ? global.location.pathname : "") !== pending.routePath;
      var recentDomChange = _lastDomMutationAt > pending.startedAt;
      if (pending.domChanged || pending.sawNetwork || routeChanged || recentDomChange) {
        _pendingUISilentFailure = null;
        return;
      }

      var snapshot = _captureDomSnapshot(pending.target);
      if (snapshot) {
        _pushRecent(_recentUI, {
          event_type: "silent_failure_dom_snapshot",
          target_selector: snapshot.selector,
          target_text: snapshot.text_preview,
          timestamp: snapshot.timestamp,
          route_path: snapshot.route_path,
        }, cfg.silentFailureContextSize);
      }

      reportSilentFailure({
        description: _buildUISilentFailureDescription(pending),
        observed: "Detected by Browser SDK V6 UI silent failure observer",
        expected: {
          type: "ui_feedback",
          selector: pending.selector,
          within_ms: cfg.uiSilentFailureTimeoutMs,
        },
        route: pending.routePath,
      });
      _pendingUISilentFailure = null;
    }, cfg.uiSilentFailureTimeoutMs);
  }

  function _armUISilentFailureDetection(eventType, target) {
    if (!cfg.autoDetectUISilentFailures) return;
    if (!target) return;
    var selector = _getSelector(target);
    if (!selector) return;
    _cancelPendingUISilentFailure();
    
    // 延迟 100ms 再开始观察，避免点击本身的 DOM 变化（focus、:active）被误判
    var observeStartTime = _nowSeconds() + 0.1;
    
    _pendingUISilentFailure = {
      eventType: eventType,
      selector: selector,
      target: target,
      targetText: _getTextPreview(target),
      routePath: global.location ? global.location.pathname : "",
      startedAt: observeStartTime,
      domChanged: false,
      sawNetwork: false,
      lastNetworkAt: null,
    };
    _scheduleUISilentFailureCheck();
  }

  function _installUISilentFailureObserver() {
    if (!cfg.captureUI || !cfg.autoDetectUISilentFailures) return;
    if (typeof MutationObserver === "undefined" || typeof document === "undefined") return;
    if (_uiMutationObserver) return;
    var root = document.querySelector(cfg.uiSilentFailureObserveSelector) || document.body;
    if (!root) return;
    _lastRoutePath = global.location ? global.location.pathname : "";
    _uiMutationObserver = new MutationObserver(function (mutations) {
      _lastDomMutationAt = _nowSeconds();
      if (_pendingUISilentFailure && mutations && mutations.length > 0) {
        var first = mutations[0];
        _markUISilentFailureDomChange(first.target || root);
      }
      var currentRoute = global.location ? global.location.pathname : "";
      if (_pendingUISilentFailure && currentRoute !== _lastRoutePath) {
        _pendingUISilentFailure.domChanged = true;
      }
      _lastRoutePath = currentRoute;
    });
    _uiMutationObserver.observe(root, {
      subtree: true,
      childList: true,
      attributes: true,
      characterData: true,
    });
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
          _autoReportNetworkError(record);
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
          _autoReportNetworkError(record);
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
          _autoReportNetworkError(record);
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
          _autoReportNetworkError(record);
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

        if (evt === "click" || evt === "submit") {
          _armUISilentFailureDetection(evt, target);
        }
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
   * @param {object} opts - { endpoint, apiKey, captureErrors, captureNetwork, captureUI, captureConsole, sampleRate, networkSampleRate, networkThrottleMs, autoDetectNetworkErrors, autoDetectUISilentFailures, uiSilentFailureTimeoutMs, uiSilentFailureObserveSelector, silentFailureContextSize, batchSize, batchInterval, maxRetries }
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
    _installUISilentFailureObserver();
    _installConsoleHook();
    _installPageHideHook();
    _restorePendingBatches(); // V5 恢复暂存的批次
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
    var silentPayload = {
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
    };
    if (_onSilentFailureReport && typeof _onSilentFailureReport === "function") {
      try {
        _onSilentFailureReport(silentPayload);
      } catch (e) {
      }
    }
    _send("/ingest/silent-failure", {
      message: silentPayload.message,
      expectation: silentPayload.expectation,
      observed: silentPayload.observed,
      observed_events: silentPayload.observed_events,
      trace_id: silentPayload.trace_id,
      source: silentPayload.source,
      extra: silentPayload.extra,
    });
  }

  /**
   * 手动上报网络错误，并自动附带最近的 UI / network 上下文。
   * @param {Error|object|string} error - Error 实例或 {method, url, status_code, duration_ms, request_body, response_body, error}
   */
  function reportNetworkError(error) {
    var record = _normalizeNetworkError(error);
    _reportNetworkRecord(record);
    _autoReportNetworkError(record);
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
    reportNetworkError: reportNetworkError,
    reportError: reportError,
    reportUIEvent: reportUIEvent,
    getSessionId: getSessionId,
    getTraceId: getTraceId,
    setTraceId: setTraceId,
    _cfg: cfg,
    _getPendingUISilentFailure: function() { return _pendingUISilentFailure; },
    _getLastDomMutationAt: function() { return _lastDomMutationAt; },
    _getUIMutationObserver: function() { return _uiMutationObserver; },
    onNetworkCapture: function (callback) {
      _onNetworkCapture = callback;
    },
    onSilentFailureReport: function (callback) {
      _onSilentFailureReport = callback;
    },
    _captureDomSnapshot: _captureDomSnapshot,
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
