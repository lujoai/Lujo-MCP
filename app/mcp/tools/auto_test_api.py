"""
MCP 工具：auto_test —— 自动遍历页面所有可交互元素并捕获缺陷。

仅暴露同步入口，内部新开事件循环跑 Playwright 异步 API，
避免与调用方的事件循环冲突。

v1.0.x 修复（自动采集链路）：
1. init script 不再在 ``document.head`` / ``document.documentElement`` 均为空时
   静默死亡：早期异常钩子（console.error / error / unhandledrejection）立即
   建立并**只进缓冲**，DOM 就绪后再挂载 SDK；缓冲不再"回放"（回放会二次触发
   页面既有 error 监听器、二次输出页面 console），改由 ``__LUJO_SDK_DRAIN__``
   单次排水取回原始项，由 heavy 侧 ``finish`` 映射入队。
   ``window.__LUJO_SDK_STATE__`` 暴露可观测阶段与 drained 计数。
2. 回传不再依赖浏览器跨源 HTTP 上报（默认 ``cors_origins=""`` 时预检必然失败）：
   本工具在自己的 browser context 上拦截 Lujo endpoint 的 SDK 上报，事件进入
   heavy 子进程内存，经结果内部键 ``_lujo_ingest`` 随 heavy IPC 回主进程，
   由 ``ingest_dispatch.drain_result_ingest_events`` 以与 /ingest/* 端点同一套
   校验/脱敏/入库落地。服务器 CORS/鉴权默认值不变，页面不接触任何密钥。
   拦截层按**原始字节体**（req.post_data_buffer）处理：如实支持 SDK 的 gzip
   上报（browser-sdk 对 >4KB 载荷自动置 Content-Encoding: gzip），有界解压
   （防 zip bomb）后走同一套闸门；br/deflate 等未知编码明确拒绝（415），
   绝不静默当明文解析。注意 req.post_data 是字节体的 UTF-8 解码结果，对
   gzip 体会直接抛 UnicodeDecodeError，不得再对生产路径使用。
3. 遍历后进入**固定有界**观察窗口（分片 dwell，事件安静也不提前结束）+
   强制冲刷（读取 JS 包装返回值）+ 回传安静确认，延迟故障在窗口内仍可采集。
   ``sdk_capture`` 状态字段如实区分 关闭/初始化成败/已观察事件数/回传完成度：
   ``delivery`` 只表示 **SDK→本工具捕获队列** 的回传完成度，**不代表主进程
   入库成功**；入库由 drain 写入的 events_ingested / ingest_failures /
   events_ingest_truncated 表达，两者不得混为一谈。请求体/累计字节超限一律
   明确拒绝并如实计数（UTF-8 字节），不做静默截断或部分入队。
"""
import gzip
import io
import json
import logging
import time
from urllib.parse import urlparse

from app.mcp.tools.ingest_dispatch import DISPATCH_INGEST_PATHS
from app.mcp.tools.ingest_dispatch import RESULT_INGEST_KEY as _RESULT_INGEST_KEY

AUTO_TEST_DEF = {
    "name": "auto_test",
    "description": (
        "本工具需要浏览器采集能力（Playwright）；未启用时调用会返回 "
        "CAPABILITY_MISSING 与启用指引。"
        "【前端现场采集入口】当用户报告前端问题（页面异常、白屏、『点了没反应』、"
        "接口表现不对、疑似静默失败）而服务端还没有任何上报现场时，应先用本工具"
        "打开目标页面自动遍历并采集真实运行现场，再分析修复。"
        "本机开发服务器（http://localhost:3000 等）默认放行，无需任何额外配置，"
        "也不需要目标页面接入 SDK 或调整服务器 CORS。"
        "自动遍历页面所有可交互元素（按钮/链接/输入框），"
        "依次执行点击并监听控制台错误和网络 4xx/5xx。"
        "遍历期间自动为页面注入采集脚本（Browser SDK），页面早期异常也会被捕获；"
        "采集的现场进入本 Lujo 实例存储，随后可用 diagnose_issue 查询"
        "（多现场时按候选 request_id 精确回查）。"
        "返回中的 sdk_capture 字段如实反映采集状态"
        "（enabled/init/events_captured/delivery）。"
        "需要 url、不需要 request_id；不需要手动指定选择器，"
        "适合快速验收 AI 生成的前端页面、批量发现「点了没反应」的静默问题；"
        "已有上报现场时定位单个已知问题请先用 diagnose_issue。"
        "需要 Playwright（pip install playwright && playwright install chromium）。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要测试的页面 URL"},
            "max_actions": {"type": "integer", "default": 20},
            "capture_console": {"type": "boolean", "default": True},
            "capture_network": {"type": "boolean", "default": True},
            "observe_ms": {
                "type": "integer",
                "default": 2000,
                "description": (
                    "遍历结束后的固定观察窗口毫秒数（0-10000，默认 2000）："
                    "窗口内延迟发生的 console.error / 网络失败仍会被采集；"
                    "窗口为固定有界 dwell，事件安静也不会提前结束，"
                    "不保证捕获窗口之外更晚发生的异常。"
                ),
            },
        },
        "required": ["url"],
    },
}

logger = logging.getLogger("lujo-mcp.auto_test")

# 子进程侧事件截获上限：与主进程排水上限（ingest_dispatch._MAX_DRAIN_EVENTS）
# 同量级；截断在 sdk_capture.events_truncated 如实上报。
_MAX_CAPTURED_INGEST_EVENTS = 200
# 观察窗口为固定有界 dwell（毫秒，调用方可经 observe_ms 调整，0-10000）；
# 回传安静确认的时间参数（毫秒）
_FLUSH_WAIT_MAX_MS = 2500
_FLUSH_QUIET_MS = 300

# finish() 的冲刷包装（单一事实源）：页面即将关闭前把 SDK 批队列排空到本工具的
# 拦截路由。tests/unit/test_auto_inject_script_js.py 会用真实 Node 执行本表达式
# 锁定其返回值语义（缺 SDK / 内部异常必须 false，不得让 finish() 误报 complete）。
_FLUSH_SDK_JS = (
    "(function () { try {"
    # F1（reviewer task-2）：SDK 从未初始化（window.AiDebug 缺失或非对象）时
    # 冲刷不可能发生 → 必须 return false，否则 finish() 会误报 delivery=complete
    " if (!window.AiDebug || typeof window.AiDebug !== 'object')"
    " { return false; }"
    " if (window.AiDebug && window.AiDebug._flushBatch)"
    " { window.AiDebug._flushBatch(false); }"
    " if (window.AiDebug && window.AiDebug.destroy)"
    " { window.AiDebug.destroy({ flush: true }); }"
    " return true;"
    "} catch (e) { return false; } })()"
)

# 采集请求体字节上限（UTF-8 编码后计量）＝**原始体**闸门：复用既有约定——
# app/middleware.py 以 settings.max_body_size（默认 1 MiB）限制 HTTP 请求体，
# 这里对 SDK 上报做同一口径的原始体闸门，避免畸形/超大 JSON 进入 heavy 内存。
_MAX_CAPTURE_BODY_BYTES = 1_048_576
# gzip **解压后**字节上限：复用 app/api/ingest.py::_MAX_DECOMPRESSED_SIZE
# （10 MiB）的既有约定，与真实 /ingest/batch 端点同一口径（否则 1-10 MiB 的
# 正常大批次在 beacon/sync 路径会被本通道误丢）。tools 层不得反向 import
# api 层，故本地常量 + 注释锚定；改动此处必须同步核对 app/api/ingest.py:27。
_MAX_CAPTURE_DECOMPRESSED_BYTES = 10 * 1024 * 1024
# 会话累计采集字节上限（UTF-8）：heavy IPC 帧硬上限为
# heavy_spawn._MAX_FRAME_BYTES = 64 MiB（app/mcp/protocol/heavy_spawn.py），
# 取其 1/8（8 MiB）留足余量，保证结果帧本身不会顶到协议的无效长度防线。
_MAX_CAPTURE_TOTAL_BYTES = 8 * 1024 * 1024
# 本工具真正需要截获的路径：ingest 分发路径（单一事实源）之外只多一个批处理端点。
# 其余请求（含其它 OPTIONS/POST）一律 route.fallback()，交回 SSRF 守卫与正常请求处理。
_CAPTURE_INGEST_PATHS = DISPATCH_INGEST_PATHS | {"/ingest/batch"}

# gzip 上报体支持（R5）：browser-sdk/ai-debug.js 在 payload > 4KB 时自动 gzip
# 并置 Content-Encoding: gzip（:79 compressionThreshold=4096、:656
# setRequestHeader("Content-Encoding", "gzip")；sendBeacon 场景不压缩）。
# 采集通道必须与真实 /ingest/batch 端点同一契约，否则正常大批次必丢。
_GZIP_READ_CHUNK = 8192
_IDENTITY_ENCODINGS = frozenset({"", "identity"})
# 采集拒绝的 HTTP 状态映射：仅这些 reason 走非 2xx（正常批次保持 200）。
# 400/415 会让 SDK 的压缩发送回退明文重发一次（ai-debug.js:663），413 走
# 批次拆分（:669），与真实端点的语义一致。
_CAPTURE_REJECT_STATUS = {
    "body_too_large": 413,
    "total_bytes_exceeded": 413,
    "unsupported_encoding": 415,
    "undecodable": 400,
}


class _CaptureBodyTooLarge(Exception):
    """采集体超限（原始字节或 gzip 解压后）。

    必须独立于 ValueError：json.JSONDecodeError / UnicodeDecodeError 都是
    ValueError 子类，共用 except ValueError 会把"非法 JSON / 非法 UTF-8"
    误判为"体积超限"而错误返回 413。语义对齐
    app/api/ingest.py::_DecompressedSizeExceeded（tools 层不得反向 import
    api 层）。
    """


def _bounded_gzip_decompress(data: bytes, max_size: int) -> bytes:
    """有界 gzip 解压（防 zip bomb）：流式读取，累计输出超 max_size 即抛。

    与 app/api/ingest.py::_bounded_gzip_decompress 同语义（8 KiB 分片、累计
    超限即抛 _CaptureBodyTooLarge）。解压失败（非 gzip / 截断）由 gzip/zlib
    抛 OSError/EOFError，调用方转为如实拒绝（undecodable），不得外泄到
    route_handler 的兜底异常分支（那会把正常请求算成拦截异常并 fallback）。
    """
    chunks: list[bytes] = []
    total = 0
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
        while True:
            chunk = f.read(_GZIP_READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_size:
                raise _CaptureBodyTooLarge(f"decompressed size exceeds {max_size}")
            chunks.append(chunk)
    return b"".join(chunks)


def is_available() -> bool:
    """返回当前 Python 运行时是否安装了 Playwright。"""
    try:
        from playwright.async_api import async_playwright as _  # noqa: F401
    except ImportError:
        return False
    return True


_SDK_SCRIPT_CACHE: str | None = None
_SDK_SCRIPT_CACHE_LOADED = False


def _load_sdk_script_source() -> str | None:
    """读取随包分发的 browser-sdk/ai-debug.js 内容（进程内缓存）。

    源码布局与冻结布局（packaging spec 把 browser-sdk 放到 bundle 根，
    与 app/ 模块树同根）都覆盖；读不到返回 None，调用方退回网络通道加载。
    """
    global _SDK_SCRIPT_CACHE, _SDK_SCRIPT_CACHE_LOADED
    if _SDK_SCRIPT_CACHE_LOADED:
        return _SDK_SCRIPT_CACHE
    _SDK_SCRIPT_CACHE_LOADED = True
    import pathlib
    import sys

    candidates = [
        # 源码布局：app/mcp/tools/… 的上三级 = 仓库根（与 app/main.py 的
        # /ai-debug.js 服务端解析同一份文件，保持单一事实源）
        pathlib.Path(__file__).resolve().parents[3] / "browser-sdk" / "ai-debug.js",
    ]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(pathlib.Path(meipass) / "browser-sdk" / "ai-debug.js")
    for cand in candidates:
        if cand.is_file():
            try:
                _SDK_SCRIPT_CACHE = cand.read_text(encoding="utf-8")
            except OSError:
                logger.exception("读取 SDK 脚本失败：%s", cand)
            break
    else:
        logger.warning("auto_test 未找到 browser-sdk/ai-debug.js（候选：%s）", candidates)
    return _SDK_SCRIPT_CACHE


def _build_sdk_init_script() -> str | None:
    """构造 auto_test 页面自动埋点的 Playwright init script。

    产出自包含 JS，行为契约（tests/unit/test_auto_inject_script_js.py 以真实
    Node 运行时固化）：

    - **早期观察立即建立**：console.error 包装与 error / unhandledrejection
      监听先落缓冲（不依赖 DOM），页面早期（SDK 加载完成前）异常不丢失；
    - **DOM 相关操作可延迟**：``documentElement`` 就绪后再挂载
      ``<script src="{endpoint}/ai-debug.js">``（不再在 head 与
      documentElement 均为空时 ``appendChild`` 抛异常被吞）；
    - **不回放**：早期缓冲只保存页面真实可得的原始信息（缺失字段一律不写、
      不伪造堆栈），不 new ErrorEvent / 不 dispatchEvent / 不调 console.error——
      采集不得改变目标页面既有监听器、错误处理与日志行为；
    - **单次排水**：``__LUJO_SDK_DRAIN__`` 返回并清空缓冲，state.drained 累加；
      heavy 侧 ``finish`` 负责把原始项映射入队（每个原始事件只排空一次）；
    - **幂等**：``__LUJO_SDK_INJECTED__`` 守卫防重复注入；
    - **可观测**：``window.__LUJO_SDK_STATE__`` 暴露 phase/buffered/drained。

    - endpoint 使用 settings.http_host / http_port 实时值（默认 127.0.0.1:8710）；
      统一模式下父进程会把 CLI（--http-host/--http-port）解析出的**有效绑定**
      随 spawn 注入 heavy 子进程环境（app/mcp/protocol/heavy_process.py::
      _child_env_with_effective_http），因此本处读到的是真实监听地址，而不是
      子进程自己继承到的 env/默认值；
    - 返回 None 表示注入被关闭（settings.auto_inject_sdk=False），调用方
      必须完全跳过注入。
    """
    from app.config import settings

    if not settings.auto_inject_sdk:
        return None
    endpoint = f"http://{settings.http_host}:{settings.http_port}"
    # json.dumps 把 endpoint 安全编码为 JS 字符串字面量（引号/特殊字符转义）
    endpoint_js = json.dumps(endpoint)
    return (
        "(function () {\n"
        "  'use strict';\n"
        "  var MAX_BUFFER = 50;\n"
        "  var WAIT_TICK_MS = 25;\n"
        "  var WAIT_MS = Number(window.__LUJO_SDK_WAIT_MS__) > 0"
        " ? Number(window.__LUJO_SDK_WAIT_MS__) : 10000;\n"
        "  if (window.__LUJO_SDK_INJECTED__) { return; }\n"
        "  window.__LUJO_SDK_INJECTED__ = true;\n"
        "  var state = window.__LUJO_SDK_STATE__ = {\n"
        "    phase: 'buffering', injectedAt: Date.now(),\n"
        "    buffered: 0, drained: 0, error: ''\n"
        "  };\n"
        f"  var ENDPOINT = {endpoint_js};\n"
        "  var SDK_URL = ENDPOINT + '/ai-debug.js';\n"
        "  var buffer = [];\n"
        "  function noteError(err) {\n"
        "    state.error = String((err && err.message) || err).slice(0, 300);\n"
        "  }\n"
        "  function fmt(args) {\n"
        "    var out = [], i, a;\n"
        "    for (i = 0; i < args.length; i++) {\n"
        "      a = args[i];\n"
        "      try { out.push(a && typeof a === 'object' ? JSON.stringify(a) : String(a)); }\n"
        "      catch (e) { out.push('[object]'); }\n"
        "    }\n"
        "    return out.join(' ');\n"
        "  }\n"
        "  function push(item) {\n"
        "    if (buffer.length >= MAX_BUFFER) { return; }\n"
        "    buffer.push(item);\n"
        "    state.buffered = buffer.length;\n"
        "  }\n"
        "  function clip(value, limit) {\n"
        "    if (value === undefined || value === null) { return null; }\n"
        "    var s = String(value);\n"
        "    return s ? s.slice(0, limit) : null;\n"
        "  }\n"
        # 早期钩子：SDK ready 前缓冲，ready 后放行（SDK 自身钩子负责采集）
        "  var origError = console.error;\n"
        "  if (typeof origError === 'function') {\n"
        "    console.error = function () {\n"
        "      if (!window.__LUJO_SDK_READY__) {\n"
        "        try { push({ kind: 'console', message: fmt(arguments).slice(0, 2000) }); } catch (e) {}\n"
        "      }\n"
        "      return origError.apply(console, arguments);\n"
        "    };\n"
        "  }\n"
        "  window.addEventListener('error', function (ev) {\n"
        "    if (window.__LUJO_SDK_READY__) { return; }\n"
        "    try {\n"
        "      var msg = (ev && ev.message) ? String(ev.message) : 'error';\n"
        "      var where = (ev && ev.filename)"
        " ? (' @' + ev.filename + ':' + (ev.lineno || 0)) : '';\n"
        "      var item = { kind: 'pageerror', message: (msg + where).slice(0, 2000) };\n"
        "      var err = ev && ev.error;\n"
        "      var excType = clip(err && err.name, 200);\n"
        "      if (excType) { item.exc_type = excType; }\n"
        "      var file = clip(ev && ev.filename, 1000);\n"
        "      if (file) { item.file = file; }\n"
        "      if (ev && typeof ev.lineno === 'number' && ev.lineno > 0)"
        " { item.line = ev.lineno; }\n"
        "      if (ev && typeof ev.colno === 'number' && ev.colno > 0)"
        " { item.column = ev.colno; }\n"
        "      var stack = clip(err && err.stack, 4000);\n"
        "      if (stack) { item.stack = stack; }\n"
        "      push(item);\n"
        "    } catch (e) {}\n"
        "  });\n"
        "  window.addEventListener('unhandledrejection', function (ev) {\n"
        "    if (window.__LUJO_SDK_READY__) { return; }\n"
        "    try {\n"
        "      var reason = ev && ev.reason;\n"
        "      var item = { kind: 'pageerror', message: ('Unhandled rejection: '"
        " + String(reason)).slice(0, 2000) };\n"
        "      var excType = clip(reason && reason.name, 200);\n"
        "      if (excType) { item.exc_type = excType; }\n"
        "      var stack = clip(reason && reason.stack, 4000);\n"
        "      if (stack) { item.stack = stack; }\n"
        "      push(item);\n"
        "    } catch (e) {}\n"
        "  });\n"
        "  window.__LUJO_SDK_DRAIN__ = function () {\n"
        "    var items = buffer; buffer = []; state.buffered = 0;\n"
        "    state.drained += items.length;\n"
        "    return items;\n"
        "  };\n"
        "  function initSdk() {\n"
        "    try {\n"
        "      if (window.AiDebug && typeof window.AiDebug.init === 'function') {\n"
        "        window.AiDebug.init({ endpoint: ENDPOINT });\n"
        "        if (window.AiDebug._inited) {\n"
        "          window.__LUJO_SDK_READY__ = true;\n"
        "          state.phase = 'ready';\n"
        "          return true;\n"
        "        }\n"
        "      }\n"
        "      state.phase = 'loading';\n"
        "      return false;\n"
        "    } catch (e) {\n"
        "      state.phase = 'failed';\n"
        "      noteError(e);\n"
        "      return false;\n"
        "    }\n"
        "  }\n"
        "  function mount() {\n"
        "    try {\n"
        "      var root = document.head || document.documentElement;\n"
        "      if (!root) { return false; }\n"
        "      var s = document.createElement('script');\n"
        "      s.src = SDK_URL;\n"
        "      s.onload = function () { initSdk(); };\n"
        "      s.onerror = function () {\n"
        "        state.phase = 'failed';\n"
        "        state.error = 'sdk script load failed';\n"
        "      };\n"
        "      root.appendChild(s);\n"
        "      state.phase = 'loading';\n"
        "      return true;\n"
        "    } catch (e) {\n"
        "      state.phase = 'failed';\n"
        "      noteError(e);\n"
        "      return false;\n"
        "    }\n"
        "  }\n"
        "  if (!mount()) {\n"
        "    var waited = 0;\n"
        "    var timer = setInterval(function () {\n"
        "      waited += WAIT_TICK_MS;\n"
        "      var ok = mount();\n"
        "      if (ok || waited >= WAIT_MS) {\n"
        "        clearInterval(timer);\n"
        "        if (!ok) {\n"
        "          state.phase = 'failed';\n"
        "          if (!state.error) { state.error = 'document root unavailable'; }\n"
        "        }\n"
        "      }\n"
        "    }, WAIT_TICK_MS);\n"
        "  }\n"
        "})();"
    )


class _SdkCapture:
    """auto_test 会话内的 SDK 上报拦截器（heavy 子进程侧，不触网）。

    拦截范围仅限 Lujo 自身 endpoint（由 ``_build_sdk_init_script`` 注入的
    ENDPOINT），其余请求 ``fallback()`` 交回 SSRF 逐跳守卫；不构成任意地址
    代理。截获的事件经结果内部键 ``_lujo_ingest`` 回主进程排水入库；
    请求体一律按字节体读取（req.post_data_buffer），支持 gzip 与有界解压。
    """

    def __init__(
        self,
        endpoint: str,
        max_body_bytes: int | None = None,
        max_total_bytes: int | None = None,
        max_decompressed_bytes: int | None = None,
    ):
        self.endpoint = endpoint
        self.pattern = f"{endpoint}/**"
        # 限额默认取模块常量；测试可传小阈值验证拒绝语义
        self.max_body_bytes = (
            _MAX_CAPTURE_BODY_BYTES if max_body_bytes is None else max_body_bytes
        )
        self.max_total_bytes = (
            _MAX_CAPTURE_TOTAL_BYTES if max_total_bytes is None else max_total_bytes
        )
        # 解压后上限（仅 gzip 分支使用）：默认复用 _MAX_DECOMPRESSED_SIZE 的 10 MiB
        self.max_decompressed_bytes = (
            _MAX_CAPTURE_DECOMPRESSED_BYTES
            if max_decompressed_bytes is None
            else max_decompressed_bytes
        )
        self.events: list[dict] = []
        self.truncated = False
        self.ingest_requests = 0
        self.last_event_monotonic = 0.0
        self.fulfill_failures = 0
        # 字节/丢弃如实计数（delivery 判定与状态汇总都读这些字段）
        self.capture_bytes = 0
        self.oversized_requests = 0
        self.over_total_requests = 0
        self.unknown_path_events = 0
        # R5：编码/解码类丢弃（delivery 判定与状态汇总同样读这些字段）
        self.body_encoding_rejected = 0
        self.undecodable_bodies = 0

    def _append_event(self, path: str, payload: dict) -> bool:
        """入队一条事件；超出条数上限则标记 truncated 并返回 False（丢弃如实暴露）。"""
        if len(self.events) >= _MAX_CAPTURED_INGEST_EVENTS:
            self.truncated = True
            return False
        self.events.append({"path": path, "payload": payload})
        return True

    def _capture_body(
        self, path: str, body: bytes | None, content_encoding: str | None = None
    ) -> dict:
        """截获一次上报请求：编码闸门 → 三层体积闸门 → 解析入队。

        入参 body 是原始字节体（Playwright 的 req.post_data_buffer）；生产
        路径不得改用 req.post_data——后者是字节体的 UTF-8 解码结果，遇到
        gzip 二进制体会直接抛 UnicodeDecodeError。

        三层体积闸门（一律 UTF-8 编码字节数）：
        1. 原始体闸门 max_body_bytes（默认 1 MiB，全部请求）：复用
           settings.max_body_size 约定（app/config.py:128 / app/middleware.py）；
        2. 解压后闸门 max_decompressed_bytes（默认 10 MiB，**仅 gzip**）：
           复用 app/api/ingest.py::_MAX_DECOMPRESSED_SIZE 约定；identity/
           明文体没有解压层，仍只由第 1 层约束（与真实端点一致）；
        3. 累计闸门 max_total_bytes（默认 8 MiB，全部请求）：按**实际接收并
           尝试解析的载荷字节数**（gzip 场景即解压后长度）累加——**包含无法
           解析为 JSON 的体**（非法 UTF-8 / 非法 JSON 同样占额度，防洪语义：
           连续坏体不得绕过累计上限）；在累计之前就被拒绝的请求（原始体超限、
           gzip 解压失败或解压超限、不支持的编码）不计入。中文等多字节载荷下
           字符数会低估真实体积，必须用字节数才不误放行。

        返回 {"accepted", "rejected", "received_bytes", "reason", "limit"?}：
        - reason 为 None 表示如实 2xx；
        - body_too_large：超过第 1 层或第 2 层闸门（limit 指出是哪一层）；
        - total_bytes_exceeded：超过第 3 层闸门；
        - unsupported_encoding：content-encoding 既非 identity 也非 gzip；
        - undecodable：gzip 解压失败 / 非 UTF-8 / JSON 解析失败。
        以上任一非 None 原因都整个请求拒绝：不入队、不部分入队。
        """
        raw = body or b""
        received = len(raw)
        outcome = {
            "accepted": 0, "rejected": 0,
            "received_bytes": received, "reason": None,
        }
        encoding = (content_encoding or "").strip().lower()
        # 第 1 层·原始体闸门：解析/解压之前先量，禁止静默截断造成畸形数据
        if received > self.max_body_bytes:
            self.oversized_requests += 1
            outcome["reason"] = "body_too_large"
            outcome["limit"] = self.max_body_bytes
            return outcome
        if encoding == "gzip":
            try:
                payload_bytes = _bounded_gzip_decompress(
                    raw, self.max_decompressed_bytes
                )
            except _CaptureBodyTooLarge:
                # 第 2 层·解压后闸门（解压器已按同一上限截断，防 zip bomb）
                self.oversized_requests += 1
                outcome["reason"] = "body_too_large"
                outcome["limit"] = self.max_decompressed_bytes
                return outcome
            except Exception:
                # 损坏 gzip / 截断：如实拒绝，不得抛到 route_handler 之外
                self.undecodable_bodies += 1
                outcome["reason"] = "undecodable"
                return outcome
            # 第 2 层复核（冗余保险，防未来改动漂移）：解压后仍按同一上限判定
            if len(payload_bytes) > self.max_decompressed_bytes:
                self.oversized_requests += 1
                outcome["reason"] = "body_too_large"
                outcome["limit"] = self.max_decompressed_bytes
                return outcome
        elif encoding in _IDENTITY_ENCODINGS:
            # identity/明文体无解压层：仍只由第 1 层 max_body_bytes 约束
            payload_bytes = raw
        else:
            # br/deflate 等：不得静默当明文解析
            self.body_encoding_rejected += 1
            outcome["reason"] = "unsupported_encoding"
            outcome["content_encoding"] = encoding
            return outcome
        # 第 3 层·累计闸门：整个请求拒绝（不得部分入队）；按实际接收并尝试解析的
        # 载荷字节数累加（含随后解析失败的体——防洪语义，见 docstring）
        if self.capture_bytes + len(payload_bytes) > self.max_total_bytes:
            self.over_total_requests += 1
            outcome["reason"] = "total_bytes_exceeded"
            outcome["limit"] = self.max_total_bytes
            return outcome
        self.capture_bytes += len(payload_bytes)
        try:
            payload = json.loads(payload_bytes or b"{}")
        except ValueError:
            # 非法 UTF-8（UnicodeDecodeError）/ 非法 JSON 同为 ValueError 子类
            logger.warning("auto_test 截获到无法解析的上报体 path=%s", path)
            self.undecodable_bodies += 1
            outcome["reason"] = "undecodable"
            return outcome
        accepted = 0
        rejected = 0
        if path == "/ingest/batch":
            events = payload.get("events") if isinstance(payload, dict) else None
            if isinstance(events, list):
                for ev in events:
                    if not isinstance(ev, dict):
                        rejected += 1
                        continue
                    ev_path = str(ev.get("path") or "")
                    # 未知事件路径不得入队，也不得伪装为有效采集成功
                    if ev_path not in DISPATCH_INGEST_PATHS:
                        rejected += 1
                        self.unknown_path_events += 1
                        continue
                    if self._append_event(ev_path, ev.get("payload") or {}):
                        accepted += 1
        elif isinstance(payload, dict):
            if self._append_event(path, payload):
                accepted = 1
        outcome["accepted"] = accepted
        outcome["rejected"] = rejected
        if accepted:
            self.last_event_monotonic = time.monotonic()
        return outcome

    async def route_handler(self, route) -> None:
        """context.route 处理器：SDK 上报截获 + 本地 CORS 头 fulfill。

        注意：这里的 CORS 头只作用于本工具私有浏览器会话内的**拦截响应**，
        不改服务器全局 CORS 配置；目的是让页面 SDK 视角上报成功（否则会
        进入指数退避重试）。真实校验/脱敏/入库发生在主进程排水时，与
        /ingest/* 端点同一套处理。
        """
        req = route.request
        path = urlparse(req.url).path
        origin = req.headers.get("origin") or "*"
        cors = {"Access-Control-Allow-Origin": origin}
        try:
            if req.method == "OPTIONS":
                # 只为本采集通道真正需要的路径处理预检；其余 OPTIONS 交回
                # 正常请求处理（不得吞掉其它端点的预检，也不得扩大全局 CORS）
                if path in _CAPTURE_INGEST_PATHS:
                    await route.fulfill(
                        status=204,
                        headers={
                            **cors,
                            "Access-Control-Allow-Methods": "POST, OPTIONS",
                            "Access-Control-Allow-Headers": "content-type, x-api-key",
                        },
                    )
                else:
                    await route.fallback()
                return
            if path == "/ai-debug.js" and req.method == "GET":
                source = _load_sdk_script_source()
                if source is not None:
                    await route.fulfill(
                        status=200,
                        content_type="application/javascript",
                        headers=cors,
                        body=source,
                    )
                else:
                    # 非预期布局（本地无 SDK 文件）：退回网络通道（统一模式下
                    # 仍可经真实 HTTP /ai-debug.js 加载）
                    await route.fallback()
                return
            if req.method == "POST" and path in _CAPTURE_INGEST_PATHS:
                # 必须读字节体：req.post_data 是 post_data_buffer 的 UTF-8
                # 解码结果，gzip 二进制体会直接抛 UnicodeDecodeError
                outcome = self._capture_body(
                    path,
                    req.post_data_buffer,
                    req.headers.get("content-encoding"),
                )
                self.ingest_requests += 1
                reject_status = _CAPTURE_REJECT_STATUS.get(outcome["reason"])
                if reject_status is not None:
                    # 明确拒绝：不得把丢弃/截断报告成全链路成功
                    reject = {
                        "error": "capture payload rejected",
                        "reason": outcome["reason"],
                        "received_bytes": outcome["received_bytes"],
                    }
                    # limit 由 _capture_body 按实际触发的闸门给出（原始体/解压后/累计）
                    if outcome.get("limit") is not None:
                        reject["limit"] = outcome["limit"]
                    if outcome.get("content_encoding"):
                        reject["content_encoding"] = outcome["content_encoding"]
                    await route.fulfill(
                        status=reject_status,
                        content_type="application/json",
                        headers=cors,
                        body=json.dumps(reject),
                    )
                    return
                # 如实返回本批的捕获结果：count=入队数，rejected=被拒事件数
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    headers=cors,
                    body=json.dumps({
                        "count": outcome["accepted"],
                        "rejected": outcome["rejected"],
                        "captured": outcome["accepted"] > 0,
                    }),
                )
                return
            await route.fallback()
        except Exception:
            # fulfill 本身失败：如实放行到网络通道（统一模式下仍有机会经
            # 真实 HTTP 送达主进程），并由状态字段暴露拦截异常计数
            self.fulfill_failures += 1
            logger.exception("auto_test 回传拦截处理失败 url=%s", str(req.url)[:160])
            try:
                await route.fallback()
            except Exception:
                logger.debug("fallback 亦失败（连接可能已断）", exc_info=True)

    async def finish(self, page, observe_ms: int, inflight: set) -> dict:
        """遍历后收尾：观察窗口 → 冲刷（读返回值）→ 状态+早期排水 → 安静确认/汇总。

        观察窗口为固定有界 dwell（分片等待）：页面在窗口内调度的定时器/
        请求（含慢速连接失败，如回环 connection-refused 的秒级延迟）都能
        自然推进；不做"安静即提前收"——静默早退会在已调度未触发的页面
        动作之前关掉浏览器。回传完成只依据真实观察（事件计数停增 + 静默
        期 + 在途请求清空）产生，不做无依据的"完成"宣称。

        delivery 语义：只表示 SDK→本捕获队列的回传完成度，与主进程入库
        结果（drain 的 events_ingested / ingest_failures）无对应关系；
        有丢弃（截断/单体重/累计超限/未知路径）时最高只能是 partial。
        """
        # ① 观察窗口：固定有界 dwell，分片等待（上限 observe_ms）
        remaining = observe_ms
        while remaining > 0:
            chunk = min(remaining, 500)
            await page.wait_for_timeout(chunk)
            remaining -= chunk

        # ② 强制冲刷：先常规 flush（异步 XHR），再 destroy({flush:true}) 收尾
        # 冲刷——SDK 客户端批节流（throttleWindowMs=5000 / maxBatchesPerWindow=2）
        # 会把超出配额的批次暂存到 _pendingBatches 延迟发送，常规 flush 不排空
        # 暂存批；destroy 的 beacon 路径同步排空全部暂存与队列（无 apiKey 时
        # 走同步 XHR，经本工具的拦截路由送达子进程）。页面即将关闭，钩子拆除
        # 无副作用（SDK destroy 即为此场景设计）。
        flush_ok = True
        try:
            flushed = await page.evaluate(_FLUSH_SDK_JS)
            # 包装内部异常会 return false：必须读真实返回值，不能把
            # "evaluate 未抛异常"当成冲刷成功
            flush_ok = flushed is True
        except Exception:
            flush_ok = False
            logger.debug("auto_test 冲刷 SDK 批队列失败（页面可能已离开）", exc_info=True)

        # ③ 读页面注入状态 + 一律排空早期缓冲（不再只在 SDK 未 ready 时）：
        # 早期项只保存在页面缓冲里（不再回放），必须取回并单次入队。
        state = None
        try:
            state = await page.evaluate(
                "(function () { try { return window.__LUJO_SDK_STATE__"
                " ? JSON.parse(JSON.stringify(window.__LUJO_SDK_STATE__)) : null; }"
                " catch (e) { return null; } })()"
            )
        except Exception:
            logger.debug("auto_test 读取注入状态失败", exc_info=True)
        phase = (state or {}).get("phase")
        drained: list = []
        try:
            drained = await page.evaluate(
                "(function () { try { return typeof window.__LUJO_SDK_DRAIN__"
                " === 'function' ? window.__LUJO_SDK_DRAIN__() : []; }"
                " catch (e) { return []; } })()"
            ) or []
        except Exception:
            logger.debug("auto_test 排水早期缓冲失败", exc_info=True)
        for item in drained:
            if not isinstance(item, dict):
                continue
            message = str(item.get("message") or "")
            if item.get("kind") == "pageerror":
                payload = {
                    "exc_type": str(item.get("exc_type") or "EarlyPageError"),
                    "message": message,
                    "source": "lujo-auto-test",
                }
                # frames 仅在 file+line 真实存在时构造，不伪造 function/行号
                file = item.get("file")
                line = item.get("line")
                column = item.get("column")
                frames = []
                if file and isinstance(line, int):
                    frame = {"file": str(file), "line": line}
                    if isinstance(column, int):
                        frame["column"] = column
                    frames.append(frame)
                payload["frames"] = frames
                extra = {}
                if item.get("stack"):
                    extra["stack"] = str(item["stack"])
                if file:
                    extra["file"] = str(file)
                if isinstance(line, int):
                    extra["line"] = line
                if isinstance(column, int):
                    extra["column"] = column
                if extra:
                    payload["extra"] = extra
                self._append_event("/ingest/error", payload)
            else:
                self._append_event("/ingest/console", {
                    "level": "error",
                    "message": message,
                    "source": "lujo-auto-test",
                })
            self.last_event_monotonic = time.monotonic()

        # ④ 安静确认/状态汇总：有丢弃就绝不宣称 complete
        dropped = bool(
            self.truncated
            or self.unknown_path_events
            or self.oversized_requests
            or self.over_total_requests
            or self.body_encoding_rejected
            or self.undecodable_bodies
        )
        quiet = False
        if flush_ok:
            flush_deadline = time.monotonic() + _FLUSH_WAIT_MAX_MS / 1000
            prev = -1
            while time.monotonic() < flush_deadline:
                await page.wait_for_timeout(120)
                n = len(self.events)
                quiet_for = time.monotonic() - (
                    self.last_event_monotonic if self.events else 0.0
                )
                if (
                    n == prev
                    and quiet_for >= _FLUSH_QUIET_MS / 1000
                    and not inflight
                ):
                    quiet = True
                    break
                prev = n
        if not flush_ok:
            delivery = "flush_failed"
        elif quiet and not dropped:
            delivery = "complete"
        elif quiet:
            delivery = "partial"
        else:
            delivery = "timeout"

        status = {
            "enabled": True,
            "init": {"ready": "ready"}.get(phase, "failed" if phase else "unknown"),
            "events_captured": len(self.events),
            "events_truncated": self.truncated,
            # 字节/丢弃如实计数（delivery 使用同一批字段判定）
            "capture_bytes": self.capture_bytes,
            "oversized_requests": self.oversized_requests,
            "over_total_requests": self.over_total_requests,
            "unknown_path_events": self.unknown_path_events,
            "body_encoding_rejected": self.body_encoding_rejected,
            "undecodable_bodies": self.undecodable_bodies,
            "delivery": delivery,
        }
        if state:
            status["page_phase"] = phase
        if self.fulfill_failures:
            status["route_failures"] = self.fulfill_failures
        return status


async def _run(
    url: str,
    max_actions: int,
    capture_console: bool,
    capture_network: bool,
    observe_ms: int = 2000,
) -> dict:
    """内部 async 函数：用 Playwright 异步 API 执行遍历"""
    from playwright.async_api import async_playwright

    # v0.9.8 浏览器回退链：playwright chromium → 系统 Chrome → 系统 Edge；
    # 冻结发行版不随包分发 chromium，靠系统浏览器通道兜底。
    # 探测是纯路径存在性检查（不 spawn driver），在事件循环内同步调用安全。
    from app.runtime.verifier.browser_launcher import resolve_launch_kwargs
    launch_kwargs = resolve_launch_kwargs()
    if launch_kwargs is None:
        from app.runtime.verifier.ui_runner import capability_missing_payload
        return capability_missing_payload()

    # FIX(v0.7.1-b9-3): 遍历期间 console/network 错误列表无界增长——此前只在返回前
    # 截断 [:20]，遍历中页面刷大量错误/4xx 会无界累积内存；现采集即限长（保前 N 条）。
    # 这些列表是工具响应的展示通道；入库走 SDK 截获通道（不重复登记）。
    _MAX_CAPTURED_ERRORS = 100
    console_errors = []
    network_errors = []
    executed = []
    skipped = []

    from app.config import settings
    endpoint = f"http://{settings.http_host}:{settings.http_port}"
    capture = _SdkCapture(endpoint)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, **launch_kwargs)
        try:
            page = await browser.new_page()

            # v0.9.8 auto_test 自动埋点：把 Browser SDK init script 装到本页
            # （add_init_script 在每次导航的页面脚本之前执行，对 goto 及后续
            # 导航都生效）。v1.0.x 起早期异常先缓冲、DOM 就绪后挂载、
            # ready 后回放（见 _build_sdk_init_script 契约）。
            sdk_init_script = _build_sdk_init_script()
            if sdk_init_script is not None:
                await page.add_init_script(script=sdk_init_script)

            # SSRF 逐跳守卫：初始 URL 经 is_safe_url 校验，但 goto 重定向 / 点击触发的
            # 导航默认不校验，攻击者可借 302/JS 跳转内网绕过。复用 ui_runner 守卫逐跳拦截。
            # v1.0.x：async API 必须用 async 版守卫（sync 版在此路径注册从未生效）。
            from app.runtime.verifier.ui_runner import install_ssrf_guard_async
            await install_ssrf_guard_async(page.context)

            # 在途请求追踪：观察窗口的"安静"判定必须包含在途请求清空——
            # Windows 等环境下回环 connection-refused 要 ~2s 才浮现，静默早退
            # 会在慢网失败浮现前关掉浏览器，把已发出的失败丢在半路。
            # Request 对象按同一性去重（同一请求的 finished/failed 事件回传
            # 同一实例），集合随页面关闭释放，不构成泄漏。
            inflight: set = set()
            page.on("request", lambda req: inflight.add(req))
            page.on("requestfinished", lambda req: inflight.discard(req))
            page.on("requestfailed", lambda req: inflight.discard(req))

            # 回传拦截：注册在 SSRF 守卫之后（Playwright 后注册先匹配），
            # 仅截获 Lujo endpoint 的 SDK 上报/脚本加载，其余 fallback 交回守卫。
            if sdk_init_script is not None:
                await page.context.route(capture.pattern, capture.route_handler)

            if capture_console:
                def _on_console(msg) -> None:
                    if msg.type in ("error", "warning"):
                        if len(console_errors) < _MAX_CAPTURED_ERRORS:
                            console_errors.append({"type": msg.type, "text": msg.text})

                page.on("console", _on_console)

            if capture_network:
                def _on_response(resp) -> None:
                    if resp.status >= 400:
                        if len(network_errors) < _MAX_CAPTURED_ERRORS:
                            network_errors.append({"url": resp.url, "status": resp.status})

                page.on("response", _on_response)

            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception as e:
                logger.error(str(e), exc_info=True)
                # goto 失败也如实带回采集状态（可能已截获早期事件）
                status = {
                    "enabled": sdk_init_script is not None,
                    "init": "unknown",
                    "events_captured": len(capture.events),
                    # goto 失败时无法完成冲刷/安静确认：开启注入记为 flush_failed；
                    # no_sdk 只表示注入开关关闭（禁用分支）
                    "delivery": (
                        "flush_failed" if sdk_init_script is not None else "no_sdk"
                    ),
                }
                result = {
                    "error": "Tool execution failed",
                    "url": url,
                    "sdk_capture": status,
                }
                if sdk_init_script is not None:
                    result[_RESULT_INGEST_KEY] = capture.events
                return result

            els = await page.query_selector_all(
                "button, a[href], input:not([type=hidden]), select, textarea, "
                "[role=button], [onclick]"
            )
            found = len(els)

            for idx, el in enumerate(els):
                if idx >= max_actions:
                    skipped.append({"index": idx, "reason": "超过最大交互数"})
                    continue
                try:
                    tag = await el.evaluate("el => el.tagName.toLowerCase()")
                    text = (await el.inner_text() or "")[:50]
                    hint = await el.evaluate("el => ({ tag: el.tagName, id: el.id, cls: el.className })")

                    if not await el.is_visible():
                        skipped.append({"index": idx, "tag": tag, "text": text, "reason": "不可见"})
                        continue

                    before = page.url
                    await el.click(timeout=5000)
                    await page.wait_for_timeout(500)
                    after = page.url

                    executed.append({
                        "index": idx, "tag": tag, "text": text,
                        "id": hint.get("id", ""), "class": hint.get("cls", "")[:60],
                        "changed_url": before != after,
                    })
                except Exception as e:
                    logger.error(str(e), exc_info=True)
                    executed.append({"index": idx, "error": "Tool execution failed", "silent_failure": False})

            # v1.0.x 收尾：观察窗口 + 强制冲刷 + 回传安静确认 + 状态汇总
            if sdk_init_script is not None:
                sdk_status = await capture.finish(page, observe_ms, inflight)
            else:
                # 注入开关关闭：no_sdk 专表此意，不得与冲刷失败（flush_failed）混淆
                sdk_status = {"enabled": False, "init": "off", "delivery": "no_sdk"}

            result = {
                "url": url,
                "found_elements": found,
                "executed_count": len(executed),
                "skipped_count": len(skipped),
                "executed": executed,
                "console_errors": console_errors[:20],
                "network_errors": network_errors[:20],
                "silent_failure_detected": len(network_errors) > 0
                    or any(e.get("silent_failure") for e in executed),
                "sdk_capture": sdk_status,
            }
            if sdk_init_script is not None:
                # 内部保留键：主进程排水入库（ingest_dispatch），不进 MCP 响应
                result[_RESULT_INGEST_KEY] = capture.events
            return result
        finally:
            # FIX: C2 —— 正常/异常/取消都及时关闭浏览器，避免 chromium 进程残留
            # （async_playwright 上下文退出为兜底，此处保证尽早释放）
            try:
                await browser.close()
            except Exception:
                logger.debug("auto_test 关闭浏览器失败（可能已关闭）", exc_info=True)


async def auto_test_handler(arguments: dict) -> dict:
    """异步入口 —— 直接在当前事件循环中运行，避免嵌套循环冲突"""
    try:
        from playwright.async_api import async_playwright as _  # noqa: F401
    except ImportError:
        # P0-B：能力缺失=执行失败——与 verify_ui 共用统一 CAPABILITY_MISSING
        # 载荷；本工具在 heavy 子进程执行，dict 经 IPC 回传，不 raise。
        from app.runtime.verifier.ui_runner import capability_missing_payload

        return capability_missing_payload()

    url = arguments["url"]
    from app.runtime.verifier.ui_runner import is_safe_url
    ok, reason = is_safe_url(url)
    if not ok:
        return {"error": f"URL 被安全策略拒绝：{reason}", "url": url}
    max_actions = min(arguments.get("max_actions", 20), 50)
    cc = arguments.get("capture_console", True)
    cn = arguments.get("capture_network", True)
    try:
        observe_ms = int(arguments.get("observe_ms", 2000))
    except (TypeError, ValueError):
        observe_ms = 2000
    observe_ms = max(0, min(observe_ms, 10000))

    return await _run(url, max_actions, cc, cn, observe_ms)
