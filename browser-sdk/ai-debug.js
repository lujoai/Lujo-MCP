/**
 * lujo-mcp Browser SDK v0.6.9
 *
 * 版本以 browser-sdk/package.json 的 version 为准（本注释仅为可读性，
 * 升级时随版本 bump 一并更新，避免再次漂移）。
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
    // v0.5.1 Source Map 支持：发布标识，随错误 extra 透传（空 = 不发送，向后兼容）
    release: "",
    localStorageKey: "ai-debug-pending-batches",  // localStorage 键名
    maxPendingBatches: 10,             // 最多暂存的批次数
    // V6 Resilient backoff & storage hygiene
    maxRetryDelay: 5000,               // 重试最大退避上限（ms）
    localStorageTTL: 86400000,         // localStorage 暂存批次过期时间（24h，ms）
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
  // FIX: CR-3 服务端 /ingest/batch 单次最多接受 100 条事件（app/api/ingest.py
  // _MAX_BATCH_EVENTS，超限 413）。SDK 单请求必须按此上限分片，否则
  // 恢复暂存批次时合并 >100 条必然 413，且 413 曾被当作可重试错误整批重试、
  // 整批回写 localStorage，形成"毒批"自增强循环（事件永远无法送达）。
  var _MAX_BATCH_EVENTS = 100;
  var _onSilentFailureReport = null;

  // ── V5 传输优化状态 ──
  var _batchTimestamps = [];  // 记录每次发送的时间戳，用于节流控制
  var _pendingBatches = [];   // 待发送的批次（节流延迟时暂存）
  // FIX(v0.7.0 Minor): 暂存队列上限——极端场景（长时间断网后恢复、节流窗口
  // 持续打满）下无限堆积；超出丢最旧并告警，保护内存有界。
  var _MAX_PENDING_BATCHES = 50;
  var _pendingTimer = null;   // 节流错开发送定时器（单实例，避免同一时刻齐发）

  // ── FIX: G3 destroy/teardown 所需的模块级引用 ──
  // 各钩子安装前的原始函数引用：destroy 时还原为真原始值，避免 HMR 重载时
  // 「新实例把上一代包装器当原始值再包一层」的套娃问题。
  var _origWindowOnerror = null;   // 安装前 global.onerror
  var _origFetch = null;           // 安装前 global.fetch
  var _origXhrOpen = null;         // 安装前 XMLHttpRequest.prototype.open
  var _origXhrSend = null;         // 安装前 XMLHttpRequest.prototype.send
  var _origConsoleError = null;    // 安装前 console.error
  var _origConsoleWarn = null;     // 安装前 console.warn
  // 具名监听器引用：destroy 时 removeEventListener 需要同一函数引用
  var _onUnhandledRejection = null;
  var _onVisibilityChange = null;
  var _onPageHide = null;
  var _uiHandlers = null;          // { click:fn, input:fn, change:fn, submit:fn }（capture=true）
  var _uiHookEvents = ["click", "input", "change", "submit"];
  // UI 事件去重表（FIX: G3 —— 提升到模块级以便清理/销毁；键含动态 className，
  // 此前只增不删导致长会话无限增长）
  var _debounce = {};
  var _DEBOUNCE_TTL_MS = 1000;     // 去重窗口（与上报去重判断一致）
  var _DEBOUNCE_MAX_KEYS = 1000;   // 去重表尺寸上限，超限按时间戳淘汰最旧
  // beacon 令牌续期心跳句柄（FIX: G3 —— 此前 setInterval 未保存句柄，无法停止）
  var _tokenRefreshTimer = null;
  // 销毁标志：重试定时器等异步回调触发时短路，避免 destroy 后继续上报
  var _destroyed = false;
  // 各钩子安装标志（FIX: G3 —— destroy 仅还原/摘除确实安装过的钩子，保证幂等）
  var _errorHookInstalled = false;
  var _networkHookInstalled = false;
  var _xhrHookInstalled = false;
  var _uiHookInstalled = false;
  var _pageHideHookInstalled = false;

  // FIX: G3 —— 去重表清理：删除过期键 + 超尺寸上限时按时间戳淘汰最旧。
  // 键含动态 className（SPA/CSS-in-JS 场景持续产生新键），只增不删会无限增长。
  function _cleanupDebounce(now) {
    var keys = Object.keys(_debounce);
    var i;
    for (i = 0; i < keys.length; i++) {
      if (now - _debounce[keys[i]] >= _DEBOUNCE_TTL_MS) {
        delete _debounce[keys[i]];
      }
    }
    keys = Object.keys(_debounce);
    if (keys.length > _DEBOUNCE_MAX_KEYS) {
      keys.sort(function (a, b) { return _debounce[a] - _debounce[b]; });
      var excess = keys.length - _DEBOUNCE_MAX_KEYS;
      for (i = 0; i < excess; i++) {
        delete _debounce[keys[i]];
      }
    }
  }

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

  function _redact(obj, _seen) {
    if (typeof obj === "string") return _redactString(obj);
    if (!obj || typeof obj !== "object") return obj;
    // FIX(v0.7.0 Minor): 环引用保护——已访问对象标记，循环引用截断为 null。
    // 此前 extra 里含环引用对象（如 a.self = a）时递归爆栈，reportError 直接
    // 抛 RangeError 整条上报丢失；截断后输出仍可安全 JSON.stringify。
    var seen = _seen || new WeakSet();
    if (seen.has(obj)) return null;
    seen.add(obj);
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
              out[k] = JSON.stringify(_redact(parsed, seen));
            } else {
              out[k] = _redactString(obj[k]);
            }
          } catch (e) {
            out[k] = _redactString(obj[k]);
          }
        } else if (typeof obj[k] === "object") {
          out[k] = _redact(obj[k], seen);
        } else {
          out[k] = obj[k];
        }
      }
    }
    return out;
  }

  // ── V2 批量上报 ──
  function _send(path, payload, force) {
    // FIX: P1-G2 —— 错误类上报豁免采样：sampleRate 此前对所有事件统一门控，
    // 手动 reportError / reportSilentFailure / reportNetworkError 与全局异常捕获
    // （window.onerror / unhandledrejection）在 sampleRate=0.5 时有一半概率
    // 被无提示丢弃——业界惯例错误类事件不参与采样（采样面向高频遥测）。
    // force=true 绕过采样；其余遥测（network / ui-event / console）保持原有
    // 采样行为不变。
    if (!cfg.endpoint || (!force && !_shouldSample())) return;
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

  // ── Beacon 短时令牌（CODE_REVIEW S1）──
  // sendBeacon/EventSource 无法设置自定义 header，历史实现把永久 API Key 放进
  // ?api_key= 查询参数（会被代理/CDN/浏览器历史/Referer 明文记录）。
  // 现改为：先用 header 换取短时令牌并缓存，URL 只带该令牌上报。
  var _beaconToken = null;
  var _beaconTokenExpiresAt = 0;

  function _beaconTokenValid() {
    return !!_beaconToken && _beaconTokenExpiresAt > Date.now() + 10000;
  }

  // 主动续期：保证页面关闭（sendBeacon）时令牌已缓存且未过期
  function _refreshBeaconToken() {
    if (!cfg.apiKey || !cfg.endpoint || _beaconTokenValid()) return;
    var xhr = new XMLHttpRequest();
    var url = cfg.endpoint.replace(/\/+$/, "") + "/auth/beacon-token";
    xhr.open("POST", url, true);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.setRequestHeader("X-API-Key", cfg.apiKey);
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          var data = JSON.parse(xhr.responseText);
          _beaconToken = data.token;
          _beaconTokenExpiresAt = Date.now() + (data.expires_in || 60) * 1000;
        } catch (e) {
          _beaconToken = null;
        }
      } else {
        // 换取失败不致命：sendBeacon 场景会退回同步 XHR（带 header）
        _beaconToken = null;
      }
    };
    xhr.onerror = function () { _beaconToken = null; };
    xhr.send("{}");
  }

  function _flushBatch(useBeacon) {
    if (!cfg.endpoint) return;

    // FIX(v0.7.0 Minor): 空队列 flush 清理悬挂的定时 flush 句柄——防句柄悬挂
    // 抑制下次调度（_addEvent 的 `else if (!_batchTimer)` 分支）。
    // 注意：beacon 冲刷路径不受影响（G1 语义保持：beacon 不看队列是否为空）。
    if (!useBeacon && _batchQueue.length === 0) {
      if (_batchTimer) {
        clearTimeout(_batchTimer);
        _batchTimer = null;
      }
      return;
    }

    if (_batchTimer) {
      clearTimeout(_batchTimer);
      _batchTimer = null;
    }

    var url = cfg.endpoint.replace(/\/+$/, "") + "/ingest/batch";

    // FIX: R7-G1 —— beacon（pagehide/unload）冲刷不看事件队列是否为空：
    // 节流暂存的 _pendingBatches 同样需要在卸载前同步冲刷，否则窗口满后
    // 暂存的批次在页面关闭时静默丢失。
    // 页面关闭/隐藏路径：必须同步冲刷，绝不能延迟到定时器（unload 后定时器不会触发）
    if (useBeacon) {
      if (_pendingTimer) {
        clearTimeout(_pendingTimer);
        _pendingTimer = null;
      }
      // 先排空此前被节流延迟暂存的批次，避免丢数据
      while (_pendingBatches.length > 0) {
        _sendBatchWithCompression(url, _pendingBatches.shift(), true);
      }
      // FIX: CR-3 同样按服务端上限分片，避免恢复/积压场景单次 beacon 超 100 条被 413
      while (_batchQueue.length > 0) {
        var beaconBatch = _batchQueue.splice(0, _MAX_BATCH_EVENTS);
        _sendBatchWithCompression(url, JSON.stringify({ events: beaconBatch }), true);
      }
      return;
    }

    // FIX: CR-3 按服务端 _MAX_BATCH_EVENTS(100) 分片发送：
    // 每片独立走节流检查，超出节流限额的片进入 _pendingBatches 由
    // _drainPendingBatches 定时器错开发送 —— 不丢数据也不触发 413。
    while (_batchQueue.length > 0) {
      var batch = _batchQueue.splice(0, _MAX_BATCH_EVENTS);
      var body = JSON.stringify({ events: batch });

      // V5 节流控制：检查是否在节流窗口内
      var now = Date.now();
      _batchTimestamps = _batchTimestamps.filter(function(ts) {
        return now - ts < cfg.throttleWindowMs;
      });

      if (_batchTimestamps.length >= cfg.maxBatchesPerWindow) {
        // 超过节流限制：入暂存队列，由单一定时器按间隔逐条错开发送（避免齐发尖峰）
        if (_pendingBatches.length >= _MAX_PENDING_BATCHES) {
          // FIX(v0.7.0 Minor): 暂存队列满 → 丢最旧并告警（内存有界）
          _pendingBatches.shift();
          console.warn("[ai-debug] Pending batch queue full (" + _MAX_PENDING_BATCHES + "), dropping oldest");
        }
        _pendingBatches.push(body);
        if (!_pendingTimer) {
          var delay = cfg.throttleWindowMs - (now - _batchTimestamps[0]);
          _pendingTimer = setTimeout(_drainPendingBatches, delay);
        }
        // 剩余分片一并交给暂存队列逐条错发：splice 已取出当前片，
        // 后续循环继续取片入队，避免同窗口内齐发
        continue;
      }

      // FIX: R7-G1 —— 发送决策点同步登记时间戳。此前压缩路径的时间戳只在
      // 异步压缩回调里登记，JS 单线程下同步 while 循环跑完全部分片后回调
      // 才执行 → 所有分片看到过期时间戳，节流失效（maxBatchesPerWindow:2
      // 时实测 4 个请求齐发）。无论是否压缩，登记都必须发生在本循环内。
      _batchTimestamps.push(now);
      _sendBatchWithCompression(url, body, false);
    }
  }

  // 节流暂存批次按固定间隔逐条发送（每次只发 1 条，发完再续期），避免同一时刻齐发
  function _drainPendingBatches() {
    _pendingTimer = null;
    if (_pendingBatches.length === 0) return;
    var url = cfg.endpoint.replace(/\/+$/, "") + "/ingest/batch";
    // FIX: R7-G1 —— 错发送也同步登记（此前由 _sendBatchDirect 内部登记）
    _batchTimestamps.push(Date.now());
    _sendBatchWithCompression(url, _pendingBatches.shift(), false);
    if (_pendingBatches.length > 0) {
      _pendingTimer = setTimeout(_drainPendingBatches, _pendingSendInterval());
    }
  }

  // 每条暂存批次的发送间隔 = 节流窗口 / 窗口内允许批次数（保证不超限且错开）
  function _pendingSendInterval() {
    var windowMs = cfg.throttleWindowMs > 0 ? cfg.throttleWindowMs : 1000;
    var max = cfg.maxBatchesPerWindow > 0 ? cfg.maxBatchesPerWindow : 1;
    return Math.ceil(windowMs / max);
  }

  // V5 压缩传输：根据 payload 大小决定是否压缩
  function _sendBatchWithCompression(url, body, useBeacon) {
    // FIX: P1-2 sendBeacon 无法设置 Content-Encoding: gzip，beacon 场景永不压缩
    var shouldCompress = !useBeacon && cfg.enableCompression &&
                         body.length > cfg.compressionThreshold &&
                         typeof CompressionStream !== "undefined";

    if (shouldCompress) {
      _compressAndSend(url, body, useBeacon);
    } else {
      _sendBatchDirect(url, body, useBeacon);
    }
  }

  // V5 gzip 压缩实现（使用 Compression Streams API）
  // FIX(v0.7.0 Minor): 删除 beacon 分支死代码——本函数仅由 _sendBatchWithCompression
  // 在 shouldCompress = !useBeacon && ... 成立时调用，useBeacon 恒为 false，
  // 函数内的 useBeacon 分支与专用的 _sendBatchSyncCompressed 永不可达
  // （beacon 场景在 _sendBatchDirect 走未压缩同步路径，见 P1-2 修复注释）。
  function _compressAndSend(url, body, useBeacon) {
    try {
      var blob = new Blob([body]);
      var cs = new CompressionStream("gzip");
      var compressedStream = blob.stream().pipeThrough(cs);

      new Response(compressedStream).blob().then(function(compressedBlob) {
        var reader = new FileReader();
        reader.onload = function() {
          var compressedBody = reader.result;
          // FIX: R7-G1 —— 此处不再登记节流时间戳：登记已前移到
          // _flushBatch/_drainPendingBatches 的发送决策点（同步），
          // 异步回调里登记会让节流检查恒看到过期时间戳

          // 常规 flush：异步 XHR + 指数退避重试（透传原始明文供 gzip 回退）
          _sendBatchXhrCompressed(url, compressedBody, body, 0);
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

  // FIX(v0.7.0 Minor): beacon 64KB 上限按 UTF-8 字节数判定——此前用字符数，
  // 中文等多字节字符约 3 万字（≈9 万字节）被误判"可走 sendBeacon"，
  // 实际超 64KB 限制被服务端截断/丢弃。
  function _utf8Length(str) {
    if (typeof TextEncoder !== "undefined") {
      return new TextEncoder().encode(str).length;
    }
    // 老浏览器兜底：unescape(encodeURIComponent(str)) 产生 UTF-8 字节串
    try {
      return unescape(encodeURIComponent(str)).length;
    } catch (e) {
      return str.length;
    }
  }

  // 未压缩直接发送
  function _sendBatchDirect(url, body, useBeacon) {
    // FIX: R7-G1 —— 节流时间戳登记已前移到 _flushBatch/_drainPendingBatches
    // 的发送决策点（同步），此处不再重复登记（否则压缩/未压缩两条路径
    // 双重计数，窗口配额被提前耗尽）

    // 页面关闭场景：必须同步发送（sendBeacon 或同步 XHR），异步 XHR 会在 unload 后被取消
    if (useBeacon) {
      if (_hasSendBeacon()) {
        if (_utf8Length(body) <= _BEACON_SIZE_LIMIT) {
          if (!_beaconTokenValid()) {
            // 令牌不可用 → 退回同步 XHR（带 header），避免 URL 暴露永久 Key
            _sendBatchSync(url, body);
            return;
          }
          var beaconUrl = url + "?token=" + encodeURIComponent(_beaconToken);
          var blob = new Blob([body], { type: "application/json" });
          if (navigator.sendBeacon(beaconUrl, blob)) {
            return; // sendBeacon 成功
          }
        }
        // 超限或 sendBeacon 失败 → 同步 XHR 降级
        _sendBatchSync(url, body);
        return;
      }
      // 无 sendBeacon 能力 → 同步 XHR 兜底（不再落到异步 XHR 导致丢数据）
      _sendBatchSync(url, body);
      return;
    }

    // 常规 flush：异步 XHR + 指数退避重试
    _sendBatchXhr(url, body, 0);
  }

  // Non-retryable status codes (400/401/403: client / auth errors)
  function _isNonRetryableStatus(status) {
    return status === 400 || status === 401 || status === 403;
  }

  // FIX: CR-3 413 = 批次条数超过服务端上限（/ingest/batch 最多 100 条）。
  // 整批重试毫无意义（重试同样大小的批次必然再次 413），且重试耗尽后把
  // 整批回写 localStorage 会在下次启动时形成更大的"毒批"。
  // 正确策略：对半拆分后作为两个独立请求重发，反复 413 时指数收敛到单条；
  // 单条仍被拒（服务端上限被调低到 1 或事件本身异常）则丢弃，避免无限循环。
  function _handleBatchTooLarge(url, body) {
    var parsed;
    try { parsed = JSON.parse(body); } catch (err) { return; }
    var events = (parsed && Array.isArray(parsed.events)) ? parsed.events : [];
    if (events.length <= 1) return; // 单条仍超限：丢弃（不无限重试）
    var mid = Math.ceil(events.length / 2);
    _sendBatchXhr(url, JSON.stringify({ events: events.slice(0, mid) }), 0);
    _sendBatchXhr(url, JSON.stringify({ events: events.slice(mid) }), 0);
  }

  // Parse Retry-After header (seconds)
  function _parseRetryAfter(xhr) {
    try {
      if (xhr && typeof xhr.getResponseHeader === "function") {
        var header = xhr.getResponseHeader("Retry-After");
        if (header) {
          var seconds = parseInt(header, 10);
          if (!isNaN(seconds) && seconds > 0) {
            return seconds * 1000;
          }
        }
      }
    } catch (err) {}
    return null;
  }

  // Compute retry delay with Full Jitter exponential backoff
  function _computeRetryDelay(attempt, retryAfterMs) {
    var maxDelay = cfg.maxRetryDelay || 5000;
    if (typeof retryAfterMs === "number" && retryAfterMs > 0) {
      return Math.min(retryAfterMs, maxDelay);
    }
    var baseDelay = 500;
    var expDelay = Math.min(maxDelay, baseDelay * Math.pow(2, attempt));
    var jitter = Math.floor(Math.random() * (expDelay - 50 + 1)) + 50;
    return jitter;
  }

  function _sendBatchXhr(url, body, attempt) {
    try {
      var xhr = new XMLHttpRequest();
      xhr.open("POST", url, true);
      xhr.setRequestHeader("Content-Type", "application/json");
      if (cfg.apiKey) xhr.setRequestHeader("X-API-Key", cfg.apiKey);
      xhr.onreadystatechange = function () {
        if (xhr.readyState !== 4) return;
        if (xhr.status >= 200 && xhr.status < 300) return; // success

        // FIX: CR-3 批次过大（413）：拆分重发，不整批重试
        if (xhr.status === 413) {
          _handleBatchTooLarge(url, body);
          return;
        }

        // Fast abort on non-retryable client error
        if (_isNonRetryableStatus(xhr.status)) {
          return;
        }

        // Retry with backoff
        if (attempt < cfg.maxRetries) {
          var retryAfterMs = _parseRetryAfter(xhr);
          var delay = _computeRetryDelay(attempt, retryAfterMs);
          setTimeout(function () {
            if (_destroyed) return; // FIX: G3 销毁后不再重试
            _sendBatchXhr(url, body, attempt + 1);
          }, delay);
        } else if (cfg.enableLocalStorageFallback) {
          // Retries exhausted -> fallback to localStorage
          _saveToLocalStorage(body);
        }
      };
      xhr.send(body);
    } catch (err) {
      if (attempt < cfg.maxRetries) {
        var delay = _computeRetryDelay(attempt, null);
        setTimeout(function () {
          if (_destroyed) return; // FIX: G3 销毁后不再重试
          _sendBatchXhr(url, body, attempt + 1);
        }, delay);
      } else if (cfg.enableLocalStorageFallback) {
        _saveToLocalStorage(body);
      }
    }
  }

  // V5 Compressed XHR send（body 为原始未压缩 JSON，供 gzip 被拒/失败时明文回退）
  function _sendBatchXhrCompressed(url, compressedBody, body, attempt) {
    try {
      var xhr = new XMLHttpRequest();
      xhr.open("POST", url, true);
      xhr.setRequestHeader("Content-Type", "application/json");
      xhr.setRequestHeader("Content-Encoding", "gzip");
      if (cfg.apiKey) xhr.setRequestHeader("X-API-Key", cfg.apiKey);
      xhr.onreadystatechange = function () {
        if (xhr.readyState !== 4) return;
        if (xhr.status >= 200 && xhr.status < 300) return; // success

        // 接收端拒绝 gzip（400/415）→ 用原始未压缩数据重发一次，避免发送损坏数据
        if (attempt === 0 && (xhr.status === 400 || xhr.status === 415)) {
          _sendBatchXhr(url, body, 0);
          return;
        }

        // FIX: CR-3 批次过大（413）：拆分重发，不整批重试（body 为原始明文）
        if (xhr.status === 413) {
          _handleBatchTooLarge(url, body);
          return;
        }

        // Fast abort on non-retryable client error
        if (_isNonRetryableStatus(xhr.status)) {
          return;
        }

        // Retry with backoff
        if (attempt < cfg.maxRetries) {
          var retryAfterMs = _parseRetryAfter(xhr);
          var delay = _computeRetryDelay(attempt, retryAfterMs);
          setTimeout(function () {
            if (_destroyed) return; // FIX: G3 销毁后不再重试
            _sendBatchXhrCompressed(url, compressedBody, body, attempt + 1);
          }, delay);
        } else if (cfg.enableLocalStorageFallback) {
          // 回退用原始明文（而非 gzip 字节），否则恢复时 JSON.parse 必然失败
          _saveToLocalStorage(body);
        }
      };
      xhr.send(compressedBody);
    } catch (err) {
      if (attempt < cfg.maxRetries) {
        var delay = _computeRetryDelay(attempt, null);
        setTimeout(function () {
          if (_destroyed) return; // FIX: G3 销毁后不再重试
          _sendBatchXhrCompressed(url, compressedBody, body, attempt + 1);
        }, delay);
      } else if (cfg.enableLocalStorageFallback) {
        _saveToLocalStorage(body);
      }
    }
  }

  // V5 Compressed sync XHR send (unload / beforeunload)
  // V5/V6 Fallback: save to localStorage with TTL and metadata wrapper
  function _saveToLocalStorage(body) {
    try {
      if (typeof localStorage === "undefined") return;
      
      var pending = [];
      var stored = localStorage.getItem(cfg.localStorageKey);
      if (stored) {
        try {
          pending = JSON.parse(stored);
        } catch (err) {
          pending = [];
        }
      }
      
      var now = Date.now();
      var ttl = cfg.localStorageTTL || 86400000;
      
      // Filter out expired batches
      pending = pending.filter(function (item) {
        if (item && typeof item === "object" && item.timestamp) {
          return (now - item.timestamp) <= ttl;
        }
        return true;
      });

      // Limit pending count
      while (pending.length >= cfg.maxPendingBatches) {
        pending.shift();
      }
      
      pending.push({
        timestamp: now,
        data: body
      });
      localStorage.setItem(cfg.localStorageKey, JSON.stringify(pending));
    } catch (err) {
      // localStorage quota exceeded or disabled
    }
  }

  // V5/V6 Restore pending batches on startup with TTL checks
  function _restorePendingBatches() {
    try {
      if (typeof localStorage === "undefined") return;
      
      var stored = localStorage.getItem(cfg.localStorageKey);
      if (!stored) return;
      
      var pending = [];
      try {
        pending = JSON.parse(stored);
      } catch (err) {
        localStorage.removeItem(cfg.localStorageKey);
        return;
      }
      
      localStorage.removeItem(cfg.localStorageKey);
      
      var now = Date.now();
      var ttl = cfg.localStorageTTL || 86400000;

      pending.forEach(function(item) {
        var body = null;
        if (item && typeof item === "object" && item.data) {
          if (item.timestamp && (now - item.timestamp) > ttl) {
            return; // expired
          }
          body = item.data;
        } else if (typeof item === "string") {
          body = item;
        }

        if (!body) return;

        var parsed;
        try {
          parsed = JSON.parse(body);
        } catch (err) {
          return;
        }
        var events = (parsed && Array.isArray(parsed.events)) ? parsed.events : [];
        events.forEach(function(ev) {
          if (ev && typeof ev === "object" && ev.path) {
            _batchQueue.push({ path: ev.path, payload: ev.payload });
          }
        });
      });
      
      if (_batchQueue.length > 0) {
        // FIX: CR-3 所有暂存批次的事件合入队列后，由 _flushBatch 按
        // _MAX_BATCH_EVENTS(100) 分片发送 —— 旧实现单次 flush 整个队列，
        // 恢复 10 批 × 20 条 = 200 条时必然触发服务端 413 毒批循环。
        _flushBatch(false);
      }
    } catch (err) {
      // ignore errors during restoration
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
    // FIX: G3 —— 具名并存模块级，destroy 时可 removeEventListener
    _pageHideHookInstalled = true;
    _onVisibilityChange = function () {
      if (document.hidden) {
        _flushBatch(true);
      }
    };

    _onPageHide = function () {
      _flushBatch(true);
    };

    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", _onVisibilityChange);
    }
    if (typeof global !== "undefined") {
      global.addEventListener("pagehide", _onPageHide);
    }
  }

  function _isSelfRequest(url) {
    if (!cfg.endpoint) return false;
    var raw = String(url || "");
    if (!raw) return false;
    // FIX(v0.7.0 Minor): 前缀匹配可被相似域名绕过——http://localhost:8000.evil.com
    // 命中 http://localhost:8000 前缀 → 误判为自请求 → 上报数据被静默丢弃。
    // 改为 URL 解析后比较 scheme/host；endpoint 带路径时要求路径前缀一致。
    // 注意：这是防丢数据的正确性修复，不是安全边界（本判定只决定"是否跳过采集"）。
    try {
      var parsed = new URL(raw, typeof location !== "undefined" ? location.href : undefined);
      var endpoint = new URL(cfg.endpoint);
      if (parsed.protocol !== endpoint.protocol || parsed.host !== endpoint.host) return false;
      if (endpoint.pathname && endpoint.pathname !== "/") {
        return parsed.pathname.indexOf(endpoint.pathname) === 0;
      }
      return true;
    } catch (e) {
      return false;
    }
  }

  // ── 全局异常捕获 ──
  function _installErrorHook() {
    _errorHookInstalled = true;
    // window.onerror → 同步异常
    // FIX: G3 —— 原始 handler 存模块级，destroy 时还原
    _origWindowOnerror = global.onerror;
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
          release: cfg.release || undefined,
        },
      }, true);
      // FIX(v0.7.1-b1-10): 透传原 handler 返回值：window.onerror 返回 true 可抑制
      // 浏览器默认错误上报，此前丢弃返回值导致宿主页面的抑制语义失效。
      if (_origWindowOnerror) return _origWindowOnerror.apply(this, arguments);
    };

    // unhandledrejection → Promise 未捕获
    // FIX: G3 —— 具名并存模块级，destroy 时可 removeEventListener
    _onUnhandledRejection = function (e) {
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
          release: cfg.release || undefined,
        },
      }, true);
    };
    global.addEventListener("unhandledrejection", _onUnhandledRejection);
  }

  function _parseStack(stack) {
    if (!stack) return [];
    return stack
      .split("\n")
      .slice(1)
      .filter(function (line) { return line.trim(); })
      .map(function (line) {
        var m = line.trim().match(/at\s+(.*?)\s+\(?(.+?):(\d+):(\d+)?\)?/);
        // v0.5.1: 保留 column（source map 精确定位必需；旧版丢弃了该值）
        if (m) return { file: m[2], line: parseInt(m[3]) || 0, column: parseInt(m[4]) || 0, function: m[1] || "" };
        // Chrome format: at file:line:col
        var m2 = line.trim().match(/at\s+(.+?):(\d+):(\d+)/);
        if (m2) return { file: m2[1], line: parseInt(m2[2]) || 0, column: parseInt(m2[3]) || 0, function: "" };
        return { file: "", line: 0, column: 0, function: line.trim() };
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

  function _reportNetworkRecord(record, force) {
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
      }, force);
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

    // FIX: G3 —— 原始 fetch 存模块级，destroy 时还原
    _origFetch = global.fetch;
    if (_origFetch) {
      _networkHookInstalled = true;
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
  // FIX(v0.7.1-b1-9): XHR 对象复用（open+send 多轮）时每轮 send 叠加 4 个监听器
  // 且从不摘除：第 3 次复用后单个请求被记录 3 次（已复现）。挂新监听前先摘除
  // 上一轮的，且每个终态 handler 触发时自摘除（once 语义），杜绝跨轮累积与
  // 旧轮次闭包（旧 url/method/start）误报新请求。
  function _detachNetListeners(xhr) {
    if (xhr._aiDebugNet) {
      var h = xhr._aiDebugNet;
      try {
        xhr.removeEventListener("load", h.load);
        xhr.removeEventListener("error", h.error);
        xhr.removeEventListener("abort", h.abort);
        xhr.removeEventListener("timeout", h.timeout);
      } catch (e) {
      }
      xhr._aiDebugNet = null;
    }
  }

  function _installXhrHook() {
    if (!cfg.captureNetwork) return;

    // FIX: G3 —— 原始 XHR 方法存模块级，destroy 时还原原型
    _origXhrOpen = XMLHttpRequest.prototype.open;
    _origXhrSend = XMLHttpRequest.prototype.send;
    _xhrHookInstalled = true;

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
          _detachNetListeners(xhr);
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
          _detachNetListeners(xhr);
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
          _detachNetListeners(xhr);
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
          _detachNetListeners(xhr);
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
        _detachNetListeners(xhr);
        xhr._aiDebugNet = { load: _onLoad, error: _onError, abort: _onAbort, timeout: _onTimeout };
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

    _uiHookInstalled = true;
    // FIX: G3 —— handler 具名并存 _uiHandlers（destroy 需同一引用 + capture=true 摘除）；
    // _debounce 用模块级并加过期/尺寸清理（键含动态 className，此前无限增长）。
    _uiHandlers = {};
    _uiHookEvents.forEach(function (evt) {
      var handler = function (e) {
        // 去重：同一秒内同一元素同类事件只报一次
        var target = e.target;
        if (!target) return;
        var key = evt + ":" + (target.id || target.className || target.tagName);
        var now = Date.now();
        if (_debounce[key] && now - _debounce[key] < _DEBOUNCE_TTL_MS) return;
        _debounce[key] = now;
        if (Object.keys(_debounce).length > _DEBOUNCE_MAX_KEYS) {
          _cleanupDebounce(now);
        }

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
      };
      _uiHandlers[evt] = handler;
      document.addEventListener(evt, handler, true);
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

    // FIX: G3 —— 原始 console 方法存模块级，destroy 时还原
    _origConsoleError = global.console.error;
    if (_origConsoleError) {
      global.console.error = function () {
        _sendConsole("error", Array.prototype.slice.call(arguments));
        _origConsoleError.apply(global.console, arguments);
      };
    }

    _origConsoleWarn = global.console.warn;
    if (_origConsoleWarn) {
      global.console.warn = function () {
        _sendConsole("warn", Array.prototype.slice.call(arguments));
        _origConsoleWarn.apply(global.console, arguments);
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
    // FIX: G3 —— HMR/重复载入兜底：全局已有上一代实例时先销毁旧实例，
    // 避免监听器/定时器/原型包装叠加导致事件重复上报。
    if (global.__AI_DEBUG_INSTANCE__ && typeof global.__AI_DEBUG_INSTANCE__.destroy === "function") {
      try { global.__AI_DEBUG_INSTANCE__.destroy(); } catch (e) {}
    }
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
    _destroyed = false;
    _installErrorHook();
    _installNetworkHook();
    _installXhrHook();
    _installUIHook();
    _installUISilentFailureObserver();
    _installConsoleHook();
    _installPageHideHook();
    _restorePendingBatches(); // V5 恢复暂存的批次
    // 主动换取 beacon 令牌并周期性续期（S1：sendBeacon 场景避免永久 Key 进 URL）
    _refreshBeaconToken();
    // FIX: G3 —— 保存心跳句柄，destroy 时 clearInterval（此前未保存、无法停止）
    _tokenRefreshTimer = setInterval(_refreshBeaconToken, 25000);
    // FIX: G3 —— 记录当前实例，供 HMR 重载时销毁旧实例
    global.__AI_DEBUG_INSTANCE__ = api;
    console.log("[ai-debug] SDK initialized, session=" + _sessionId);
  }

  /**
   * FIX: G3 —— 销毁 SDK：摘除全部监听器、还原被包装的全局/原型、停止全部定时器、
   * 清空队列与去重表，并重置初始化标志（之后可安全地重新 init）。幂等，可多次调用。
   *
   * 典型用途：
   * - Vite/webpack HMR 重载（init 内会经 __AI_DEBUG_INSTANCE__ 自动销毁旧实例），
   *   避免监听器叠加与事件重复上报；
   * - SPA 卸载 / 测试收尾，防止心跳与定时器泄漏。
   *
   * @param {object} [opts]
   * @param {boolean} [opts.flush=true] - 销毁前是否先把待发批次冲刷上报（best-effort）
   */
  function destroy(opts) {
    var shouldFlush = !(opts && opts.flush === false);
    _destroyed = true;

    // 1) 先冲刷待发数据（钩子还原前，best-effort）
    if (shouldFlush) {
      try { _flushBatch(true); } catch (e) {}
    }

    // 2) 停止全部定时器
    if (_tokenRefreshTimer) { clearInterval(_tokenRefreshTimer); _tokenRefreshTimer = null; }
    if (_batchTimer) { clearTimeout(_batchTimer); _batchTimer = null; }
    if (_pendingTimer) { clearTimeout(_pendingTimer); _pendingTimer = null; }
    if (_uiSilentFailureTimer) { clearTimeout(_uiSilentFailureTimer); _uiSilentFailureTimer = null; }

    // 3) 还原 window.onerror + 摘除 unhandledrejection
    if (_errorHookInstalled) {
      global.onerror = _origWindowOnerror;
      _origWindowOnerror = null;
      if (_onUnhandledRejection) {
        try { global.removeEventListener("unhandledrejection", _onUnhandledRejection); } catch (e) {}
      }
      _onUnhandledRejection = null;
      _errorHookInstalled = false;
    }

    // 4) 摘除 pagehide / visibilitychange
    if (_pageHideHookInstalled) {
      if (_onPageHide) {
        try { global.removeEventListener("pagehide", _onPageHide); } catch (e) {}
      }
      _onPageHide = null;
      if (_onVisibilityChange && typeof document !== "undefined") {
        try { document.removeEventListener("visibilitychange", _onVisibilityChange); } catch (e) {}
      }
      _onVisibilityChange = null;
      _pageHideHookInstalled = false;
    }

    // 5) 摘除 UI 捕获监听（注册时带 capture=true，摘除必须同参）
    if (_uiHookInstalled && _uiHandlers && typeof document !== "undefined") {
      for (var i = 0; i < _uiHookEvents.length; i++) {
        var evt = _uiHookEvents[i];
        if (_uiHandlers[evt]) {
          try { document.removeEventListener(evt, _uiHandlers[evt], true); } catch (e) {}
        }
      }
    }
    _uiHandlers = null;
    _uiHookInstalled = false;

    // 6) 还原 fetch / XHR 原型 / console
    if (_networkHookInstalled) {
      global.fetch = _origFetch;
      _origFetch = null;
      _networkHookInstalled = false;
    }
    if (_xhrHookInstalled && typeof XMLHttpRequest !== "undefined") {
      XMLHttpRequest.prototype.open = _origXhrOpen;
      XMLHttpRequest.prototype.send = _origXhrSend;
      _origXhrOpen = null;
      _origXhrSend = null;
      _xhrHookInstalled = false;
    }
    if (_consoleHookInstalled && global.console) {
      if (_origConsoleError) { global.console.error = _origConsoleError; }
      if (_origConsoleWarn) { global.console.warn = _origConsoleWarn; }
      _origConsoleError = null;
      _origConsoleWarn = null;
      _consoleHookInstalled = false;
    }

    // 7) 断开 MutationObserver
    if (_uiMutationObserver) {
      try { _uiMutationObserver.disconnect(); } catch (e) {}
      _uiMutationObserver = null;
    }

    // 8) 清空队列 / 缓冲 / 去重表 / 回调槽
    _batchQueue = [];
    _pendingBatches = [];
    _batchTimestamps = [];
    _recentNetwork = [];
    _recentUI = [];
    _debounce = {};
    _networkThrottle = {};
    _pendingUISilentFailure = null;
    _onNetworkCapture = null;
    _onSilentFailureReport = null;

    // 9) 重置初始化标志 + 清除全局实例标记（允许安全重建）
    _inited = false;
    if (global.__AI_DEBUG_INSTANCE__ === api) {
      try { delete global.__AI_DEBUG_INSTANCE__; } catch (e) { global.__AI_DEBUG_INSTANCE__ = undefined; }
    }
  }

  /**
   * 手动上报静默失败
   *
   * 自动从环形缓冲取出最近 N 条 network/UI 事件（N = cfg.silentFailureContextSize，默认 20）
   * 拼装为 observed_events 数组与 trace_id 一起上报，服务端会按 kind 分类入库，
   * 保证 AI 调试时通过 MCP `context` 工具能拿到完整事件链。
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
    }, true);
  }

  /**
   * 手动上报网络错误，并自动附带最近的 UI / network 上下文。
   * @param {Error|object|string} error - Error 实例或 {method, url, status_code, duration_ms, request_body, response_body, error}
   */
  function reportNetworkError(error) {
    var record = _normalizeNetworkError(error);
    // FIX: P1-G2 —— 手动 API 豁免采样（force=true）
    _reportNetworkRecord(record, true);
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
        release: cfg.release || undefined,
      }, extra || {}),
    }, true);
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
    destroy: destroy,
    flush: flush,
    reportSilentFailure: reportSilentFailure,
    reportNetworkError: reportNetworkError,
    reportError: reportError,
    reportUIEvent: reportUIEvent,
    getSessionId: getSessionId,
    getTraceId: getTraceId,
    setTraceId: setTraceId,
    _getPublicConfig: function() {
      var copy = {};
      for (var k in cfg) {
        if (cfg.hasOwnProperty(k) && k !== 'apiKey') {
          copy[k] = cfg[k];
        }
      }
      return copy;
    },
    // 测试辅助：运行时改单个配置项（绕过 init 的 _inited 守卫，仅供 e2e 测试用）
    _setConfig: function(key, value) {
      if (cfg.hasOwnProperty(key)) cfg[key] = value;
    },
    // 测试辅助：只读查询初始化状态
    get _inited() { return _inited; },
    _getPendingUISilentFailure: function() { return _pendingUISilentFailure; },
    _getLastDomMutationAt: function() { return _lastDomMutationAt; },
    _getUIMutationObserver: function() { return _uiMutationObserver; },
    // FIX: G3 测试辅助：去重表当前键数（验证过期/尺寸清理生效）
    _getDebounceSize: function() { return Object.keys(_debounce).length; },
    _isDestroyed: function() { return _destroyed; },
    onNetworkCapture: function (callback) {
      _onNetworkCapture = callback;
    },
    onSilentFailureReport: function (callback) {
      _onSilentFailureReport = callback;
    },
    _captureDomSnapshot: _captureDomSnapshot,
    _computeRetryDelay: _computeRetryDelay,
    _isNonRetryableStatus: _isNonRetryableStatus,
    _parseRetryAfter: _parseRetryAfter,
    _saveToLocalStorage: _saveToLocalStorage,
    _restorePendingBatches: _restorePendingBatches,
    _flushBatch: _flushBatch,
    // 测试辅助（v0.7.0 Minor 单测，只读）：自请求判定与定时 flush 句柄状态
    _isSelfRequest: _isSelfRequest,
    get _batchTimerScheduled() { return !!_batchTimer; },
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
