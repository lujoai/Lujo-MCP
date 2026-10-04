"""
MCP 工具：auto_test —— 自动遍历页面所有可交互元素并捕获缺陷。

仅暴露同步入口，内部新开事件循环跑 Playwright 异步 API，
避免与调用方的事件循环冲突。

v1.0.x 修复（自动采集链路）：
1. init script 不再在 ``document.head`` / ``document.documentElement`` 均为空时
   静默死亡：早期异常钩子（console.error / error / unhandledrejection）立即
   建立、先缓冲，DOM 就绪后再挂载 SDK，ready 后回放缓冲，单次采集；
   ``window.__LUJO_SDK_STATE__`` 暴露可观测阶段。
2. 回传不再依赖浏览器跨源 HTTP 上报（默认 ``cors_origins=""`` 时预检必然失败）：
   本工具在自己的 browser context 上拦截 Lujo endpoint 的 SDK 上报，事件进入
   heavy 子进程内存，经结果内部键 ``_lujo_ingest`` 随 heavy IPC 回主进程，
   由 ``ingest_dispatch.drain_result_ingest_events`` 以与 /ingest/* 端点同一套
   校验/脱敏/入库落地。服务器 CORS/鉴权默认值不变，页面不接触任何密钥。
3. 遍历后进入有上限的观察窗口（事件驱动 settle + 强制冲刷 + 回传安静确认），
   延迟故障在窗口内仍可采集；``sdk_capture`` 状态字段如实区分
   关闭/初始化成败/已观察事件数/回传完成度。
"""
import json
import logging
import time
from urllib.parse import urlparse

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
                    "遍历结束后的观察窗口毫秒数（0-10000）：窗口内延迟发生的"
                    "console.error / 网络失败仍会被采集；事件安静后可提前结束。"
                    "不保证捕获任意晚发生的异常。"
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
    - **ready 后回放**：缓冲事件经 SDK 通道单次采集（console 类重放
      console.error，pageerror 类重放 ErrorEvent）；
    - **幂等**：``__LUJO_SDK_INJECTED__`` 守卫防重复注入；
    - **可观测**：``window.__LUJO_SDK_STATE__`` 暴露 phase/buffered/replayed，
      ``__LUJO_SDK_DRAIN__`` 供父进程在 SDK 初始化失败时取回缓冲。

    - endpoint 使用 settings.http_host / http_port 实时值（默认 127.0.0.1:8710）；
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
        "    buffered: 0, replayed: 0, error: ''\n"
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
        "  function push(kind, message) {\n"
        "    if (buffer.length >= MAX_BUFFER) { return; }\n"
        "    buffer.push({ kind: kind, message: String(message).slice(0, 2000) });\n"
        "    state.buffered = buffer.length;\n"
        "  }\n"
        # 早期钩子：SDK ready 前缓冲，ready 后放行（SDK 自身钩子负责采集）
        "  var origError = console.error;\n"
        "  if (typeof origError === 'function') {\n"
        "    console.error = function () {\n"
        "      if (!window.__LUJO_SDK_READY__) {\n"
        "        try { push('console', fmt(arguments)); } catch (e) {}\n"
        "      }\n"
        "      return origError.apply(console, arguments);\n"
        "    };\n"
        "  }\n"
        "  window.addEventListener('error', function (ev) {\n"
        "    if (window.__LUJO_SDK_READY__) { return; }\n"
        "    try {\n"
        "      var msg = (ev && ev.message) ? ev.message : 'error';\n"
        "      var where = (ev && ev.filename)"
        " ? (' @' + ev.filename + ':' + (ev.lineno || 0)) : '';\n"
        "      push('pageerror', msg + where);\n"
        "    } catch (e) {}\n"
        "  });\n"
        "  window.addEventListener('unhandledrejection', function (ev) {\n"
        "    if (window.__LUJO_SDK_READY__) { return; }\n"
        "    try {\n"
        "      push('pageerror', 'Unhandled rejection: '"
        " + ((ev && ev.reason) ? String(ev.reason) : 'unknown'));\n"
        "    } catch (e) {}\n"
        "  });\n"
        "  window.__LUJO_SDK_DRAIN__ = function () {\n"
        "    var items = buffer; buffer = []; state.buffered = 0;\n"
        "    return items;\n"
        "  };\n"
        "  function replay() {\n"
        "    var items = buffer; buffer = []; state.buffered = 0;\n"
        "    for (var i = 0; i < items.length; i++) {\n"
        "      try {\n"
        "        if (items[i].kind === 'pageerror' && typeof ErrorEvent === 'function') {\n"
        "          window.dispatchEvent(new ErrorEvent('error', {\n"
        "            message: items[i].message, error: new Error(items[i].message)\n"
        "          }));\n"
        "        } else {\n"
        "          console.error('[lujo-early] ' + items[i].message);\n"
        "        }\n"
        "        state.replayed += 1;\n"
        "      } catch (e) {}\n"
        "    }\n"
        "  }\n"
        "  function initSdk() {\n"
        "    try {\n"
        "      if (window.AiDebug && typeof window.AiDebug.init === 'function') {\n"
        "        window.AiDebug.init({ endpoint: ENDPOINT });\n"
        "        if (window.AiDebug._inited) {\n"
        "          window.__LUJO_SDK_READY__ = true;\n"
        "          state.phase = 'ready';\n"
        "          replay();\n"
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
    代理。截获的事件经结果内部键 ``_lujo_ingest`` 回主进程排水入库。
    """

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self.pattern = f"{endpoint}/**"
        self.events: list[dict] = []
        self.truncated = False
        self.ingest_requests = 0
        self.last_event_monotonic = 0.0
        self.fulfill_failures = 0

    def _append_event(self, path: str, payload: dict) -> None:
        if len(self.events) >= _MAX_CAPTURED_INGEST_EVENTS:
            self.truncated = True
            return
        self.events.append({"path": path, "payload": payload})

    def _capture_body(self, path: str, post_data: str | None) -> int:
        try:
            payload = json.loads(post_data or "{}")
        except ValueError:
            logger.warning("auto_test 截获到无法解析的上报体 path=%s", path)
            return 0
        count = 0
        if path == "/ingest/batch":
            events = payload.get("events") if isinstance(payload, dict) else None
            if isinstance(events, list):
                for ev in events:
                    if isinstance(ev, dict):
                        self._append_event(
                            str(ev.get("path") or ""), ev.get("payload") or {}
                        )
                        count += 1
        else:
            if isinstance(payload, dict):
                self._append_event(path, payload)
                count = 1
        if count:
            self.last_event_monotonic = time.monotonic()
        return count

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
                await route.fulfill(
                    status=204,
                    headers={
                        **cors,
                        "Access-Control-Allow-Methods": "POST, OPTIONS",
                        "Access-Control-Allow-Headers": "content-type, x-api-key",
                    },
                )
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
            if path.startswith("/ingest/") and req.method == "POST":
                count = self._capture_body(path, req.post_data)
                self.ingest_requests += 1
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    headers=cors,
                    body=json.dumps({"count": count, "captured": True}),
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
        """遍历后收尾：观察窗口 → 强制冲刷 → 回传安静确认 → 状态汇总。

        观察窗口为固定有界 dwell（分片等待）：页面在窗口内调度的定时器/
        请求（含慢速连接失败，如回环 connection-refused 的秒级延迟）都能
        自然推进；不做"安静即提前收"——静默早退会在已调度未触发的页面
        动作之前关掉浏览器。回传完成只依据真实观察（事件计数停增 + 静默
        期 + 在途请求清空）产生，不做无依据的"完成"宣称。
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
            await page.evaluate(
                "(function () { try {"
                " if (window.AiDebug && window.AiDebug._flushBatch)"
                " { window.AiDebug._flushBatch(false); }"
                " if (window.AiDebug && window.AiDebug.destroy)"
                " { window.AiDebug.destroy({ flush: true }); }"
                " return true;"
                "} catch (e) { return false; } })()"
            )
        except Exception:
            flush_ok = False
            logger.debug("auto_test 冲刷 SDK 批队列失败（页面可能已离开）", exc_info=True)

        # ③ 回传安静确认：冲刷后事件计数停增 + 静默期达标 + 在途清空即完成
        delivery = "timeout"
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
                    delivery = "complete"
                    break
                prev = n
        else:
            delivery = "no_sdk"

        # ④ 读页面注入状态；SDK 未 ready 时排水早期缓冲作为替补采集通道
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
        if phase not in (None, "ready"):
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
                    self._append_event("/ingest/error", {
                        "exc_type": "EarlyPageError",
                        "message": message,
                        "frames": [],
                        "source": "lujo-auto-test",
                    })
                else:
                    self._append_event("/ingest/console", {
                        "level": "error",
                        "message": message,
                        "source": "lujo-auto-test",
                    })

        status = {
            "enabled": True,
            "init": {"ready": "ready"}.get(phase, "failed" if phase else "unknown"),
            "events_captured": len(self.events),
            "events_truncated": self.truncated,
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
                    "delivery": "no_sdk",
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
                sdk_status = {"enabled": False, "init": "off"}

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
