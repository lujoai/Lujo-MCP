"""auto_test 采集→回传→主实例入库→diagnose 可查链路测试（替身 Playwright）。

背景（v1.0.0 修复工作包，先失败测试）：
auto_test 是 heavy 工具，业务全部在子进程执行。旧实现只依赖浏览器内 SDK 经
HTTP 上报回流主进程——默认 ``cors_origins=""`` 时跨源预检失败，请求根本发不出，
子进程里观察到的现场永远进不了 MCP 主进程的存储（diagnose_issue 查不到）。

修复契约（本文件固化）：
1. ``_run`` 在 context 上注册 Lujo endpoint 拦截路由（先于 SSRF 守卫生效），
   SDK 对 ``{endpoint}/ingest/*`` 的上报被截获进子进程事件队列并以 CORS 头
   fulfill（浏览器侧成功、不真正发网络请求）；
2. 结果 dict 携带内部键 ``_lujo_ingest``（{path, payload} 列表，有上限）；
3. 主进程侧 ``drain_result_ingest_events`` 弹出该键，经与 /ingest/* 端点同一套
   ``dispatch_single`` 入库，并把 ``events_ingested`` 写回 ``result.sdk_capture``；
4. 入库后 diagnose 候选枚举可查到本次 marker；
5. ``auto_inject_sdk=False`` 时：不注册路由、无 ``_lujo_ingest``、
   ``sdk_capture.init == "off"``。

Playwright 以替身模拟（route 注册采用 unshift 语义：后注册先匹配），
真实浏览器链路由 tests/e2e/test_auto_test_collection_chain.py 与验收矩阵覆盖。
"""
import fnmatch
import gzip
import json
import re
import sys
import types
from types import SimpleNamespace

import pytest

from app.config import settings


# ── Playwright 替身（支持 context.route 的 unshift 语义与请求分发） ──


class FakeRequest:
    """Playwright Request 替身：post_data 是 post_data_buffer 的 UTF-8 解码结果。

    与真实 Playwright 语义一致：二进制体（如 gzip）访问 post_data 直接抛
    UnicodeDecodeError——R5 的生产路径必须改读 post_data_buffer（字节体）。
    """

    def __init__(self, url: str, method: str = "GET", post_data: str | None = None,
                 origin: str = "http://127.0.0.1:8765",
                 post_data_buffer: bytes | None = None,
                 headers: dict | None = None):
        self.url = url
        self.method = method
        merged = {"origin": origin}
        if headers:
            # Playwright 头名一律小写
            merged.update({str(k).lower(): v for k, v in headers.items()})
        self.headers = merged
        if post_data_buffer is not None:
            self._buffer = bytes(post_data_buffer)
        elif post_data is not None:
            self._buffer = post_data.encode("utf-8")
        else:
            self._buffer = None

    @property
    def post_data_buffer(self) -> bytes | None:
        return self._buffer

    @property
    def post_data(self) -> str | None:
        if self._buffer is None:
            return None
        return self._buffer.decode("utf-8")


class FakeRoute:
    def __init__(self, request: FakeRequest):
        self.request = request
        self.fulfilled = None
        self.fell_back = False
        self.aborted = False

    async def fulfill(self, status=200, body=None, content_type=None, headers=None, **_):
        self.fulfilled = {
            "status": status, "body": body,
            "content_type": content_type, "headers": headers or {},
        }

    async def fallback(self, **_):
        self.fell_back = True

    async def abort(self, **_):
        self.aborted = True

    async def continue_(self, **_):
        self.continued = True


class FakeContext:
    """route 注册采用 Playwright 语义：后注册的 handler 先匹配。"""

    def __init__(self):
        self.routes: list[tuple[str, object]] = []
        self.dispatched: list[FakeRoute] = []

    async def route(self, pattern: str, handler):
        self.routes.insert(0, (pattern, handler))

    async def dispatch(self, url: str, method: str = "GET", post_data: str | None = None,
                       post_data_buffer: bytes | None = None,
                       headers: dict | None = None) -> FakeRoute:
        route = FakeRoute(
            FakeRequest(url, method, post_data,
                        post_data_buffer=post_data_buffer, headers=headers)
        )
        self.dispatched.append(route)
        for pattern, handler in list(self.routes):
            regex = re.compile(
                fnmatch.translate(pattern).replace(r"\Z", "") + r".*\Z", re.IGNORECASE
            )
            if regex.match(url) or fnmatch.fnmatch(url, pattern):
                await handler(route)
                if route.fulfilled is not None or route.aborted:
                    break
                # fallback/continue → 继续匹配下一个（更早注册的）handler
        return route


class FakePage:
    def __init__(self, context: FakeContext):
        self.context = context
        self.init_scripts = []
        self.events = {}
        self.url = "http://127.0.0.1:8765/page"
        self.evaluate_responses: list = []
        # F1：记录实际下发的 evaluate 表达式（锁定 finish 使用共享冲刷包装）
        self.evaluate_expressions: list = []
        # R5：可让 goto 发出 gzip 批量体（真实 SDK >4KB 自动压缩路径）
        self.goto_post_data_buffer: bytes | None = None
        self.goto_headers: dict | None = None

    async def add_init_script(self, script=None, path=None):
        self.init_scripts.append(script)

    def on(self, event, handler):
        self.events.setdefault(event, []).append(handler)

    async def goto(self, url, **kwargs):
        self.url = url
        # 模拟 SDK 注入成功后的真实上报形态：/ingest/batch 批量 POST
        batch = json.dumps({
            "events": [
                {
                    "path": "/ingest/console",
                    "payload": {
                        "level": "error",
                        "message": "CHAINMARKER-UNIT early console error",
                        "source": "browser_sdk",
                        "session_id": "sess-unit-chain",
                        "extra": {"session_id": "sess-unit-chain", "url": url},
                    },
                },
            ]
        })
        await self.context.dispatch(
            "http://127.0.0.1:8999/ingest/batch", method="POST", post_data=batch,
            post_data_buffer=self.goto_post_data_buffer,
            headers=self.goto_headers,
        )
        return None

    async def query_selector_all(self, selector):
        return []

    async def wait_for_timeout(self, ms):
        return None

    async def evaluate(self, expression, arg=None):
        self.evaluate_expressions.append(expression)
        if self.evaluate_responses:
            resp = self.evaluate_responses.pop(0)
            if isinstance(resp, Exception):
                raise resp
            return resp
        return None


class FakeBrowser:
    def __init__(self, page):
        self._page = page
        self.closed = False

    async def new_page(self):
        return self._page

    async def close(self):
        self.closed = True


def _install_fake_playwright(monkeypatch, page: FakePage) -> FakeBrowser:
    browser = FakeBrowser(page)
    pw_module = types.ModuleType("playwright")
    api_module = types.ModuleType("playwright.async_api")

    class _FakePlaywright:
        def __init__(self, browser_):
            self._b = browser_

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        @property
        def chromium(self):
            return SimpleNamespace(launch=lambda **kw: self._async_launch(**kw))

        @staticmethod
        async def _async_launch(**kw):
            return browser

    api_module.async_playwright = lambda: _FakePlaywright(browser)
    pw_module.async_api = api_module
    monkeypatch.setitem(sys.modules, "playwright", pw_module)
    monkeypatch.setitem(sys.modules, "playwright.async_api", api_module)
    return browser


async def _noop_async_guard(ctx):
    return None


@pytest.mark.asyncio
async def test_install_ssrf_guard_async_registers_and_blocks_private(monkeypatch):
    """v1.0.x 修复：async 版 SSRF 守卫必须真正 await 注册（sync 版在 async
    context 上注册从未生效，auto_test 长期无逐跳守卫）。"""
    import app.runtime.verifier.ui_runner as ui_runner_mod

    # 公网放行路径经白名单命中验证（单测不依赖外部 DNS 解析）
    monkeypatch.setattr(settings, "ui_url_allowlist", "public-example.test")

    calls = []

    class _Ctx:
        async def route(self, pattern, handler):
            calls.append(("route", pattern, handler))

    await ui_runner_mod.install_ssrf_guard_async(_Ctx())
    assert calls and calls[0][0] == "route" and calls[0][1] == "**/*"

    # 拦截语义：内部 scheme 放行（continue_）；公网 http(s) 放行；
    # 元数据地址拒绝（fail-closed，与 sync 版同一套 inspect_url_security）
    class _Route:
        def __init__(self, url):
            from types import SimpleNamespace

            self.request = SimpleNamespace(url=url)
            self.action = None

        async def continue_(self, **_):
            self.action = "continue"

        async def abort(self, **_):
            self.action = "abort"

    handler = calls[0][2]

    internal = _Route("data:text/plain,hi")
    await handler(internal)
    assert internal.action == "continue"

    public = _Route("https://public-example.test/x.js")
    await handler(public)
    assert public.action == "continue"

    metadata = _Route("http://169.254.169.254/latest/meta-data")
    await handler(metadata)
    assert metadata.action == "abort"


@pytest.fixture()
def _stub_auto_test_env(monkeypatch):
    """屏蔽浏览器启动解析与 SSRF 守卫（本文件只验证采集/回传内部流）。"""
    import app.runtime.verifier.browser_launcher as launcher_mod
    import app.runtime.verifier.ui_runner as ui_runner_mod

    monkeypatch.setattr(launcher_mod, "resolve_launch_kwargs", dict)
    monkeypatch.setattr(ui_runner_mod, "install_ssrf_guard_async", _noop_async_guard)


@pytest.mark.asyncio
async def test_run_registers_ingest_capture_route_and_returns_events(monkeypatch, _stub_auto_test_env):
    """B2/B3 断点：route 截获 SDK 上报进 ``_lujo_ingest``，并以 CORS 头 fulfill。"""
    from app.mcp.tools import auto_test_api

    monkeypatch.setattr(settings, "auto_inject_sdk", True)
    monkeypatch.setattr(settings, "http_host", "127.0.0.1")
    monkeypatch.setattr(settings, "http_port", 8999)

    page = FakePage(FakeContext())
    page.evaluate_responses = [
        None,                                # flush evaluate
        {"phase": "ready", "buffered": 0, "drained": 0},  # 状态 evaluate
    ]
    browser = _install_fake_playwright(monkeypatch, page)

    result = await auto_test_api.auto_test_handler({
        "url": "http://127.0.0.1:8765/page", "max_actions": 1,
    })

    assert "error" not in result, result
    # 内部事件键存在且为 {path, payload} 形态
    events = result.get("_lujo_ingest")
    assert isinstance(events, list) and events, f"应携带 _lujo_ingest 事件：{result}"
    assert events[0]["path"] == "/ingest/console"
    assert "CHAINMARKER-UNIT" in str(events[0]["payload"].get("message"))
    # 采集状态字段（可选新增）：enabled/init/events_captured/delivery
    status = result.get("sdk_capture")
    assert isinstance(status, dict), f"应包含 sdk_capture 状态：{result}"
    assert status.get("enabled") is True
    assert status.get("init") == "ready"
    assert status.get("events_captured", 0) >= 1
    # 浏览器侧 fulfill：2xx + 允许跨源读取（页面 SDK 视角上报成功）
    ingest_route = next(
        r for r in page.context.dispatched if "/ingest/" in r.request.url
    )
    assert ingest_route.fulfilled is not None
    assert 200 <= ingest_route.fulfilled["status"] < 300
    aco = (ingest_route.fulfilled["headers"] or {}).get("Access-Control-Allow-Origin")
    assert aco, "fulfill 必须带 CORS 头，否则页面 SDK 读不到响应"
    # 资源回收
    assert browser.closed is True


@pytest.mark.asyncio
async def test_run_without_sdk_reports_off_state(monkeypatch, _stub_auto_test_env):
    """auto_inject_sdk=False：不注册路由、无 _lujo_ingest、init=off（开关语义保持）。"""
    from app.mcp.tools import auto_test_api

    monkeypatch.setattr(settings, "auto_inject_sdk", False)

    page = FakePage(FakeContext())
    _install_fake_playwright(monkeypatch, page)

    result = await auto_test_api.auto_test_handler({
        "url": "http://127.0.0.1:8765/page", "max_actions": 1,
    })

    assert "error" not in result
    assert page.init_scripts == [], "开关关闭时不得注入 init script"
    assert "_lujo_ingest" not in result
    status = result.get("sdk_capture")
    assert isinstance(status, dict) and status.get("enabled") is False
    assert status.get("init") == "off"
    # delivery 取值集合里的 no_sdk 只表示注入开关关闭
    assert status.get("delivery") == "no_sdk"
    # 未注册任何采集路由
    assert page.context.routes == []


def test_drain_ingests_events_into_queryable_storage():
    """B3 断点：主进程排水后事件进入可查询存储，diagnose 候选可见 marker。"""
    from app.mcp.tools.diagnose_api import _enumerate_fault_candidates
    from app.mcp.tools.ingest_dispatch import drain_result_ingest_events
    from app.runtime.core import trace_repo

    result = {
        "url": "http://127.0.0.1:8765/page",
        "sdk_capture": {"enabled": True, "init": "ready", "events_captured": 1},
        "_lujo_ingest": [
            {
                "path": "/ingest/console",
                "payload": {
                    "level": "error",
                    "message": "DRAINMARKER unit drain console error",
                    "source": "browser_sdk",
                    "session_id": "sess-drain-unit",
                },
            },
        ],
    }

    drain_result_ingest_events(result)

    # 内部键必须被弹出，不得泄漏进 MCP 响应
    assert "_lujo_ingest" not in result
    status = result["sdk_capture"]
    assert status.get("events_ingested") == 1, status
    # diagnose 候选枚举可查到（console error 桶级故障信号）
    candidates, _complete = _enumerate_fault_candidates(None, since_minutes=0)
    assert candidates, "排水入库后 diagnose 候选不应为空"
    # marker 可从桶内 console 记录回查
    found_marker = False
    for cand in candidates:
        bucket = cand.get("request_id")
        for entry in trace_repo.get_console_logs(bucket):
            if "DRAINMARKER" in str(entry.get("message") or ""):
                found_marker = True
    assert found_marker, f"marker 应可从存储回查：{candidates}"


def test_drain_counts_failures_and_ignores_results_without_key():
    """排水对未知 path 计失败、对无内部键的结果为 no-op。"""
    from app.mcp.tools.ingest_dispatch import drain_result_ingest_events

    bad = {
        "sdk_capture": {"enabled": True},
        "_lujo_ingest": [{"path": "/ingest/not-exist", "payload": {}}],
    }
    drain_result_ingest_events(bad)
    assert bad["sdk_capture"].get("ingest_failures") == 1
    assert bad["sdk_capture"].get("events_ingested") == 0

    plain = {"url": "x", "found_elements": 0}
    drain_result_ingest_events(plain)
    assert plain == {"url": "x", "found_elements": 0}


# ── R1/R2/R4：冲刷返回值 / 采集白名单收窄 / UTF-8 字节限额 ──
# （先失败测试；替身 Playwright 不启动真实浏览器）


_ENDPOINT = "http://127.0.0.1:8999"


def _make_capture(**kwargs):
    from app.mcp.tools.auto_test_api import _SdkCapture

    return _SdkCapture(_ENDPOINT, **kwargs)


def _route(method: str = "GET", path: str = "/", post_data: str | None = None,
           post_data_buffer: bytes | None = None,
           headers: dict | None = None) -> FakeRoute:
    return FakeRoute(FakeRequest(
        _ENDPOINT + path, method=method, post_data=post_data,
        post_data_buffer=post_data_buffer, headers=headers,
    ))


async def _finish_status(capture, *, flush=True, state=None, drained=None) -> dict:
    """直接跑 finish()：按 冲刷 → 状态 → 排水 顺序回放三个 evaluate 响应。"""
    page = FakePage(FakeContext())
    page.evaluate_responses = [
        flush,
        state if state is not None else {"phase": "ready"},
        drained if drained is not None else [],
    ]
    return await capture.finish(page, 0, set())


@pytest.mark.asyncio
async def test_flush_false_reports_flush_failed_and_still_drains(monkeypatch, _stub_auto_test_env):
    """R1：冲刷包装返回 False 必须读出来——delivery=flush_failed，且早期缓冲仍被排空入队。"""
    from app.mcp.tools import auto_test_api

    monkeypatch.setattr(settings, "auto_inject_sdk", True)
    monkeypatch.setattr(settings, "http_host", "127.0.0.1")
    monkeypatch.setattr(settings, "http_port", 8999)

    page = FakePage(FakeContext())
    page.evaluate_responses = [
        False,  # 冲刷包装内部异常 → 返回 false
        {"phase": "ready", "buffered": 1, "drained": 0},
        [{"kind": "console", "message": "EARLY-DRAIN-MARKER-1"}],
    ]
    _install_fake_playwright(monkeypatch, page)

    result = await auto_test_api.auto_test_handler({
        "url": "http://127.0.0.1:8765/page", "max_actions": 1,
    })

    status = result.get("sdk_capture") or {}
    assert status.get("delivery") == "flush_failed", status
    assert status.get("delivery") != "complete"
    messages = [str(e["payload"].get("message")) for e in result.get("_lujo_ingest") or []]
    assert any("EARLY-DRAIN-MARKER-1" in m for m in messages), messages


@pytest.mark.asyncio
async def test_flush_exception_reports_flush_failed_and_still_drains(monkeypatch, _stub_auto_test_env):
    """R1：冲刷 evaluate 抛异常同样 flush_failed，且不得因冲刷失败跳过早期排水。"""
    from app.mcp.tools import auto_test_api

    monkeypatch.setattr(settings, "auto_inject_sdk", True)
    monkeypatch.setattr(settings, "http_host", "127.0.0.1")
    monkeypatch.setattr(settings, "http_port", 8999)

    page = FakePage(FakeContext())
    page.evaluate_responses = [
        RuntimeError("page closed"),
        {"phase": "ready", "buffered": 1, "drained": 0},
        [{"kind": "pageerror", "message": "EARLY-DRAIN-MARKER-2"}],
    ]
    _install_fake_playwright(monkeypatch, page)

    result = await auto_test_api.auto_test_handler({
        "url": "http://127.0.0.1:8765/page", "max_actions": 1,
    })

    status = result.get("sdk_capture") or {}
    assert status.get("delivery") == "flush_failed", status
    assert status.get("delivery") != "complete"
    paths = [e["path"] for e in result.get("_lujo_ingest") or []]
    assert "/ingest/error" in paths, paths


@pytest.mark.asyncio
async def test_drain_maps_early_items_honestly():
    """R3：早期项映射为 /ingest/error(console)——真实字段进 frames/extra，缺失不伪造。"""
    capture = _make_capture()
    drained = [
        {"kind": "console", "message": "early console"},
        {"kind": "pageerror", "message": "plain pageerror"},
        {
            "kind": "pageerror", "message": "real pageerror", "exc_type": "TypeError",
            "file": "http://x/app.js", "line": 12, "column": 3, "stack": "TypeError: real",
        },
    ]
    status = await _finish_status(capture, flush=True, state={"phase": "ready"}, drained=drained)
    assert len(capture.events) == 3
    by_path = {}
    for ev in capture.events:
        by_path.setdefault(ev["path"], []).append(ev["payload"])
    assert len(by_path["/ingest/console"]) == 1
    errs = by_path["/ingest/error"]
    assert len(errs) == 2
    plain = next(p for p in errs if p["message"] == "plain pageerror")
    assert plain["exc_type"] == "EarlyPageError"  # 缺失时如实回落
    assert plain["frames"] == []                  # 无 file/line 不得伪造 frame
    assert "extra" not in plain
    real = next(p for p in errs if p["message"] == "real pageerror")
    assert real["exc_type"] == "TypeError"
    assert real["frames"] == [{"file": "http://x/app.js", "line": 12, "column": 3}]
    assert real["extra"]["stack"] == "TypeError: real"
    assert status["events_captured"] == 3


@pytest.mark.asyncio
async def test_options_preflight_only_for_capture_paths():
    """R2：OPTIONS 只在采集白名单路径上 204，其余一律 fallback（不吞预检）。"""
    capture = _make_capture()

    other = _route("OPTIONS", "/api/health")
    await capture.route_handler(other)
    assert other.fell_back is True
    assert other.fulfilled is None

    batch = _route("OPTIONS", "/ingest/batch")
    await capture.route_handler(batch)
    assert batch.fulfilled is not None and batch.fulfilled["status"] == 204
    headers = batch.fulfilled["headers"]
    assert headers.get("Access-Control-Allow-Methods") == "POST, OPTIONS"
    assert headers.get("Access-Control-Allow-Headers") == "content-type, x-api-key"
    assert headers.get("Access-Control-Allow-Origin")


@pytest.mark.asyncio
async def test_post_outside_whitelist_falls_back_without_capture():
    """R2：不在白名单的 POST（含 /ingest/unknown）必须 fallback，不得伪装采集成功。"""
    capture = _make_capture()
    route = _route("POST", "/ingest/unknown", json.dumps({"message": "nope"}))
    await capture.route_handler(route)
    assert route.fell_back is True
    assert route.fulfilled is None
    assert capture.events == []
    assert capture.ingest_requests == 0


@pytest.mark.asyncio
async def test_batch_rejects_unknown_inner_event_paths():
    """R2：/ingest/batch 内含未知事件路径必须计入 rejected、不入队，响应如实。"""
    capture = _make_capture()
    batch = json.dumps({"events": [
        {"path": "/ingest/console", "payload": {"level": "error", "message": "OK-1"}},
        {"path": "/ingest/unknown", "payload": {"message": "EVIL"}},
    ]})
    route = _route("POST", "/ingest/batch", batch)
    await capture.route_handler(route)

    assert route.fulfilled["status"] == 200
    body = json.loads(route.fulfilled["body"])
    assert body["count"] == 1, body
    assert body["rejected"] == 1, body
    assert body["captured"] is True
    assert [e["path"] for e in capture.events] == ["/ingest/console"]
    assert capture.unknown_path_events == 1


@pytest.mark.asyncio
async def test_single_request_byte_limit_rejects_413_without_queueing():
    """R4：单体超限 → 不入队、不解析，413 明确拒绝，oversized_requests 如实。"""
    capture = _make_capture(max_body_bytes=32)
    oversized = json.dumps({"level": "error", "message": "X" * 64})
    assert len(oversized.encode("utf-8")) > 32

    route = _route("POST", "/ingest/console", oversized)
    await capture.route_handler(route)

    assert route.fulfilled["status"] == 413, route.fulfilled
    reason = json.loads(route.fulfilled["body"])
    assert reason.get("reason") == "body_too_large", reason
    # R5b：identity/明文体由**原始体**上限闸门（max_body_bytes）约束，
    # limit 必须报原始体上限而不是解压后上限
    assert reason.get("limit") == 32, reason
    assert capture.events == []
    assert capture.oversized_requests == 1

    status = await _finish_status(capture)
    assert status["delivery"] != "complete"
    assert status["oversized_requests"] == 1
    assert status["events_captured"] == 0


@pytest.mark.asyncio
async def test_total_byte_limit_rejects_whole_request_without_partial_queueing():
    """R4：累计超限 → 整个请求拒绝（不得部分入队），over_total_requests 如实。"""
    body1 = json.dumps({"level": "error", "message": "A" * 8})
    body2 = json.dumps({"level": "error", "message": "B" * 8})
    limit = len(body1.encode("utf-8")) + 1
    capture = _make_capture(max_total_bytes=limit)

    first = _route("POST", "/ingest/console", body1)
    await capture.route_handler(first)
    assert first.fulfilled["status"] == 200
    assert len(capture.events) == 1

    second = _route("POST", "/ingest/console", body2)
    await capture.route_handler(second)
    assert second.fulfilled["status"] == 413, second.fulfilled
    assert json.loads(second.fulfilled["body"]).get("reason") == "total_bytes_exceeded"
    assert len(capture.events) == 1, "超限请求不得部分入队"
    assert capture.over_total_requests == 1

    status = await _finish_status(capture)
    assert status["delivery"] != "complete"
    assert status["over_total_requests"] == 1


@pytest.mark.asyncio
async def test_byte_limit_uses_utf8_bytes_not_character_count():
    """R4：计量一律 UTF-8 字节——字符数低于阈值但字节数超阈值必须被拒。"""
    payload = json.dumps({"level": "error", "message": "中文中文中文中文"}, ensure_ascii=False)
    char_len = len(payload)
    byte_len = len(payload.encode("utf-8"))
    assert byte_len > char_len, "构造前提：中文载荷 UTF-8 字节数应大于字符数"

    capture = _make_capture(max_body_bytes=char_len)
    route = _route("POST", "/ingest/console", payload)
    await capture.route_handler(route)

    assert route.fulfilled["status"] == 413, route.fulfilled
    assert capture.events == []
    assert capture.oversized_requests == 1


@pytest.mark.asyncio
async def test_normal_payload_still_captured_and_ingested(monkeypatch, _stub_auto_test_env):
    """R4 回归：正常 SDK 批次继续工作；drain 后 events_ingested 如实。"""
    from app.mcp.tools import auto_test_api
    from app.mcp.tools.ingest_dispatch import drain_result_ingest_events

    monkeypatch.setattr(settings, "auto_inject_sdk", True)
    monkeypatch.setattr(settings, "http_host", "127.0.0.1")
    monkeypatch.setattr(settings, "http_port", 8999)

    page = FakePage(FakeContext())
    page.evaluate_responses = [True, {"phase": "ready", "buffered": 0, "drained": 0}, []]
    _install_fake_playwright(monkeypatch, page)

    result = await auto_test_api.auto_test_handler({
        "url": "http://127.0.0.1:8765/page", "max_actions": 1,
    })

    status = result.get("sdk_capture") or {}
    assert status.get("delivery") == "complete", status
    assert status.get("capture_bytes", 0) > 0, status
    assert status.get("oversized_requests") == 0, status
    assert status.get("over_total_requests") == 0, status
    assert status.get("unknown_path_events") == 0, status
    events = result.get("_lujo_ingest") or []
    assert len(events) == 1 and events[0]["path"] == "/ingest/console", events

    drain_result_ingest_events(result)
    assert result["sdk_capture"].get("events_ingested") == 1, result["sdk_capture"]
    assert "_lujo_ingest" not in result


def test_drain_byte_limit_marks_truncated_and_stops_ingesting(monkeypatch):
    """R4：排水侧字节上限 → 停止入库并在 sdk_capture 写真实 events_ingest_truncated。"""
    import app.mcp.tools.ingest_dispatch as dispatch_mod

    first = {"path": "/ingest/console", "payload": {"level": "error", "message": "DRAINLIMIT-A"}}
    second = {"path": "/ingest/console", "payload": {"level": "error", "message": "DRAINLIMIT-B"}}
    size_first = len(json.dumps(first, ensure_ascii=False).encode("utf-8"))
    monkeypatch.setattr(dispatch_mod, "_MAX_DRAIN_BYTES", size_first)

    result = {"sdk_capture": {"enabled": True}, "_lujo_ingest": [first, second]}
    dispatch_mod.drain_result_ingest_events(result)

    status = result["sdk_capture"]
    assert status.get("events_ingest_truncated") is True, status
    assert status.get("events_ingested") == 1, status


def test_capture_whitelist_derives_from_dispatch_paths():
    """R2：采集白名单从 ingest_dispatch.DISPATCH_INGEST_PATHS 派生（单一事实源）。"""
    from app.mcp.tools.auto_test_api import _CAPTURE_INGEST_PATHS
    from app.mcp.tools.ingest_dispatch import DISPATCH_INGEST_PATHS

    assert _CAPTURE_INGEST_PATHS == DISPATCH_INGEST_PATHS | {"/ingest/batch"}


def test_dispatch_ingest_paths_is_consistency_locked():
    """R2：白名单与 dispatch_single 支持集严格一致，未知路径必须抛。"""
    from app.mcp.tools.ingest_dispatch import DISPATCH_INGEST_PATHS, dispatch_single

    assert DISPATCH_INGEST_PATHS == frozenset({
        "/ingest/error", "/ingest/network", "/ingest/ui-event",
        "/ingest/console", "/ingest/silent-failure",
    })
    for path in sorted(DISPATCH_INGEST_PATHS):
        dispatch_single(path, {})  # 不得抛 "Unknown ingest path"
    with pytest.raises(ValueError):
        dispatch_single("/ingest/unknown", {})


# ── R5：gzip 上报体（SDK >4KB 自动压缩）与二进制体边界 ──
# 真实 SDK（browser-sdk/ai-debug.js:79 compressionThreshold=4096、:656
# setRequestHeader("Content-Encoding","gzip")）对大批次自动 gzip；Playwright 的
# req.post_data 是 post_data_buffer.decode()，对二进制体直接抛 UnicodeDecodeError。
# 采集通道必须与真实 /ingest/batch 端点同一契约，否则正常大批次必丢。


def _gzip_batch(message: str) -> tuple[bytes, bytes]:
    plain = json.dumps({"events": [
        {"path": "/ingest/console", "payload": {"level": "error", "message": message}},
    ]}).encode("utf-8")
    return plain, gzip.compress(plain)


@pytest.mark.asyncio
async def test_gzip_batch_body_is_decompressed_and_captured():
    """R5 红相：gzip 批次体不得抛异常/走 fallback，必须解压后入队。"""
    capture = _make_capture()
    plain, compressed = _gzip_batch("GZIP-MARKER-" + "x" * 5000)
    assert len(plain) > 4096, "构造前提：与 SDK compressionThreshold 同量级"

    route = _route("POST", "/ingest/batch", post_data_buffer=compressed,
                   headers={"content-encoding": "gzip"})
    await capture.route_handler(route)

    assert capture.fulfill_failures == 0, "二进制体不得抛进 拦截异常 兜底分支"
    assert route.fell_back is False
    assert route.fulfilled is not None, "gzip 批次必须被拦截并 fulfill"
    assert 200 <= route.fulfilled["status"] < 300, route.fulfilled
    body = json.loads(route.fulfilled["body"])
    assert body["count"] == 1 and body["captured"] is True, body
    assert [e["path"] for e in capture.events] == ["/ingest/console"]
    assert capture.oversized_requests == 0
    assert capture.undecodable_bodies == 0
    assert capture.body_encoding_rejected == 0

    status = await _finish_status(capture)
    assert "route_failures" not in status, status
    assert status["delivery"] == "complete", status


@pytest.mark.asyncio
async def test_gzip_batch_survives_full_chain_into_drain(monkeypatch, _stub_auto_test_env):
    """R5 端到端（替身）：gzip 批次不丢、route_failures 不增长、drain 后入库正确。"""
    from app.mcp.tools import auto_test_api
    from app.mcp.tools.ingest_dispatch import drain_result_ingest_events

    monkeypatch.setattr(settings, "auto_inject_sdk", True)
    monkeypatch.setattr(settings, "http_host", "127.0.0.1")
    monkeypatch.setattr(settings, "http_port", 8999)

    plain, compressed = _gzip_batch("GZIPCHAIN-" + "y" * 5000)
    assert len(plain) > 4096

    page = FakePage(FakeContext())
    page.goto_post_data_buffer = compressed
    page.goto_headers = {"content-encoding": "gzip"}
    page.evaluate_responses = [True, {"phase": "ready", "buffered": 0, "drained": 0}, []]
    _install_fake_playwright(monkeypatch, page)

    result = await auto_test_api.auto_test_handler({
        "url": "http://127.0.0.1:8765/page", "max_actions": 1,
    })

    dispatch_route = page.context.dispatched[0]
    assert dispatch_route.fulfilled is not None
    assert 200 <= dispatch_route.fulfilled["status"] < 300, dispatch_route.fulfilled

    status = result.get("sdk_capture") or {}
    assert "route_failures" not in status, f"gzip 批次不得计入 route_failures：{status}"
    events = result.get("_lujo_ingest") or []
    assert len(events) == 1 and events[0]["path"] == "/ingest/console", events
    assert "GZIPCHAIN" in str(events[0]["payload"].get("message"))
    assert status.get("delivery") == "complete", status
    assert status.get("undecodable_bodies") == 0, status
    assert status.get("body_encoding_rejected") == 0, status

    drain_result_ingest_events(result)
    assert result["sdk_capture"].get("events_ingested") == 1, result["sdk_capture"]


@pytest.mark.asyncio
async def test_gzip_decompressed_size_limit_is_rejected():
    """R5b：解压后字节数超 max_decompressed_bytes → 413 body_too_large（限 zip bomb）。"""
    capture = _make_capture(max_body_bytes=2048, max_decompressed_bytes=4096)
    plain = json.dumps({"events": [
        {"path": "/ingest/console", "payload": {"level": "error", "message": "B" * 8192}},
    ]}).encode("utf-8")
    compressed = gzip.compress(plain)
    assert len(compressed) <= 2048, "构造前提：原始压缩体在原始体闸门之内"
    assert len(plain) > 4096

    route = _route("POST", "/ingest/batch", post_data_buffer=compressed,
                   headers={"content-encoding": "gzip"})
    await capture.route_handler(route)

    assert capture.fulfill_failures == 0
    assert route.fulfilled is not None and route.fulfilled["status"] == 413, route.fulfilled
    reject = json.loads(route.fulfilled["body"])
    assert reject["reason"] == "body_too_large", reject
    assert reject.get("limit") == 4096, reject
    assert capture.events == []
    assert capture.oversized_requests == 1
    assert capture.undecodable_bodies == 0

    status = await _finish_status(capture)
    assert status["delivery"] != "complete", status
    assert status["oversized_requests"] == 1


@pytest.mark.asyncio
async def test_decompressed_body_above_raw_limit_is_accepted():
    """R5b 反证：解压上限（复用 10MiB 约定）不得退化为原始体上限。

    压缩体 100B 级、解压后远大于 max_body_bytes(200) 但小于
    max_decompressed_bytes(4096) 的批次必须被接受。
    """
    capture = _make_capture(max_body_bytes=200, max_decompressed_bytes=4096)
    plain = json.dumps({"events": [
        {"path": "/ingest/console", "payload": {"level": "error", "message": "E" * 3000}},
    ]}).encode("utf-8")
    compressed = gzip.compress(plain)
    assert len(compressed) <= 200, "构造前提：压缩体在原始体闸门之内"
    assert 200 < len(plain) <= 4096, "构造前提：解压后超原始体上限但未超解压上限"

    route = _route("POST", "/ingest/batch", post_data_buffer=compressed,
                   headers={"content-encoding": "gzip"})
    await capture.route_handler(route)

    assert capture.fulfill_failures == 0
    assert route.fulfilled is not None and route.fulfilled["status"] == 200, route.fulfilled
    body = json.loads(route.fulfilled["body"])
    assert body["count"] == 1 and body["captured"] is True, body
    assert [e["path"] for e in capture.events] == ["/ingest/console"]
    assert capture.oversized_requests == 0
    assert capture.capture_bytes == len(plain)


@pytest.mark.asyncio
async def test_decompressed_over_limit_is_rejected_with_decompressed_limit():
    """R5b：解压后超 max_decompressed_bytes → 413 body_too_large，limit 报解压上限。"""
    capture = _make_capture(max_body_bytes=4096, max_decompressed_bytes=1024)
    plain = json.dumps({"events": [
        {"path": "/ingest/console", "payload": {"level": "error", "message": "F" * 3000}},
    ]}).encode("utf-8")
    compressed = gzip.compress(plain)
    assert len(compressed) <= 4096, "构造前提：压缩体在原始体闸门之内"
    assert len(plain) > 1024

    route = _route("POST", "/ingest/batch", post_data_buffer=compressed,
                   headers={"content-encoding": "gzip"})
    await capture.route_handler(route)

    assert capture.fulfill_failures == 0
    assert route.fulfilled is not None and route.fulfilled["status"] == 413, route.fulfilled
    reject = json.loads(route.fulfilled["body"])
    assert reject["reason"] == "body_too_large", reject
    assert reject.get("limit") == 1024, reject
    assert capture.events == []
    assert capture.oversized_requests == 1

    status = await _finish_status(capture)
    assert status["delivery"] != "complete", status
    assert status["oversized_requests"] == 1


@pytest.mark.asyncio
async def test_unsupported_content_encoding_is_rejected_not_parsed():
    """R5：br/deflate 等非 identity/gzip 编码必须明确拒绝（415），不得当明文解析。"""
    capture = _make_capture()
    plain, _ = _gzip_batch("UNSUPPORTED-ENCODING")
    route = _route("POST", "/ingest/batch", post_data_buffer=plain,
                   headers={"content-encoding": "br"})
    await capture.route_handler(route)

    assert capture.fulfill_failures == 0
    assert route.fulfilled is not None and route.fulfilled["status"] == 415, route.fulfilled
    reject = json.loads(route.fulfilled["body"])
    assert reject["reason"] == "unsupported_encoding", reject
    assert reject.get("content_encoding") == "br"
    assert capture.events == []
    assert capture.body_encoding_rejected == 1

    status = await _finish_status(capture)
    assert status["delivery"] != "complete", status
    assert status["body_encoding_rejected"] == 1


@pytest.mark.asyncio
async def test_binary_body_without_encoding_is_rejected_not_raised():
    """R5 红相（0x8b gzip magic 无编码头）：不得抛 UnicodeDecodeError，如实 400。"""
    capture = _make_capture()
    gzip_magic = b"\x1f\x8b\x08\x00" + b"\x00" * 8
    route = _route("POST", "/ingest/console", post_data_buffer=gzip_magic)
    await capture.route_handler(route)

    assert capture.fulfill_failures == 0, "解码失败不得计入拦截异常"
    assert route.fell_back is False
    assert route.fulfilled is not None and route.fulfilled["status"] == 400, route.fulfilled
    assert json.loads(route.fulfilled["body"])["reason"] == "undecodable"
    assert capture.events == []
    assert capture.undecodable_bodies == 1


@pytest.mark.asyncio
async def test_corrupt_gzip_body_is_rejected_not_raised():
    """R5：声明 gzip 但体损坏 → 400 undecodable，不得抛到 route_handler 之外。"""
    capture = _make_capture()
    route = _route("POST", "/ingest/batch", post_data_buffer=b"not-a-gzip-body",
                   headers={"content-encoding": "gzip"})
    await capture.route_handler(route)

    assert capture.fulfill_failures == 0
    assert route.fulfilled is not None and route.fulfilled["status"] == 400, route.fulfilled
    assert json.loads(route.fulfilled["body"])["reason"] == "undecodable"
    assert capture.events == []
    assert capture.undecodable_bodies == 1
    assert capture.oversized_requests == 0

    status = await _finish_status(capture)
    assert status["delivery"] != "complete", status
    assert status["undecodable_bodies"] == 1


@pytest.mark.asyncio
async def test_total_byte_gate_counts_decompressed_bytes():
    """R5：累计闸门按解压后载荷字节数累加（不是压缩后字节数）。"""
    big_plain, big_compressed = _gzip_batch("C" * 3000)
    small_plain, small_compressed = _gzip_batch("D" * 100)
    capture = _make_capture(max_total_bytes=len(big_plain) + 10)

    first = _route("POST", "/ingest/batch", post_data_buffer=big_compressed,
                   headers={"content-encoding": "gzip"})
    await capture.route_handler(first)
    assert first.fulfilled["status"] == 200, first.fulfilled
    assert capture.capture_bytes == len(big_plain)
    assert capture.over_total_requests == 0

    second = _route("POST", "/ingest/batch", post_data_buffer=small_compressed,
                    headers={"content-encoding": "gzip"})
    await capture.route_handler(second)
    assert second.fulfilled["status"] == 413, second.fulfilled
    assert json.loads(second.fulfilled["body"])["reason"] == "total_bytes_exceeded"
    assert capture.over_total_requests == 1
    # 若按压缩后长度累加，第二条不会超限——锁定口径为解压后字节数
    assert (
        len(big_compressed) + len(small_compressed) < capture.max_total_bytes
    ), "构造前提：压缩后总字节远低于累计上限"

    status = await _finish_status(capture)
    assert status["capture_bytes"] == len(big_plain)
    assert status["delivery"] != "complete", status


@pytest.mark.asyncio
async def test_identity_content_encoding_still_accepted():
    """R5 守卫：Content-Encoding: identity 与缺省头一样按明文正常采集。"""
    capture = _make_capture()
    body = json.dumps({"level": "error", "message": "IDENTITY-OK"})
    route = _route("POST", "/ingest/console", post_data=body,
                   headers={"content-encoding": "identity"})
    await capture.route_handler(route)

    assert route.fulfilled["status"] == 200, route.fulfilled
    assert [e["path"] for e in capture.events] == ["/ingest/console"]
    assert capture.body_encoding_rejected == 0
    assert capture.undecodable_bodies == 0


# ── F1 / R4-nit（独立 reviewer task-2 发现，review.md @ 474DCBEE） ──


@pytest.mark.asyncio
async def test_flush_false_with_failed_state_reports_flush_failed():
    """F1 契约（Python 侧）：冲刷未成功即便页内已 failed，也只能 flush_failed。

    reviewer 复现的旧行为是 {init: failed, delivery: complete}（因为冲刷包装在
    window.AiDebug 缺失时误 return true）；这里锁定"冲刷 false + 页内 failed"
    的组合结果必须是 init=failed / delivery=flush_failed，绝不 complete。
    """
    capture = _make_capture()
    status = await _finish_status(
        capture, flush=False, state={"phase": "failed"}, drained=[],
    )
    assert status["delivery"] == "flush_failed", status
    assert status["init"] == "failed", status
    assert status.get("page_phase") == "failed", status


def test_capture_body_docstring_documents_cumulative_quota_semantics():
    """R4-nit：累计闸门的文档口径必须与实现一致——无法解析的体同样占额度。"""
    from app.mcp.tools.auto_test_api import _SdkCapture

    # 去掉所有空白：docstring 换行/缩进不得影响口径断言
    doc = "".join((_SdkCapture._capture_body.__doc__ or "").split())
    assert "实际接收并尝试解析的载荷字节数" in doc, doc
    assert "无法解析为JSON的体" in doc, doc
    # 防洪语义的另一半：在累计之前就被拒绝的请求不计入
    assert "解压失败" in doc, doc


@pytest.mark.asyncio
async def test_undecodable_bodies_consume_cumulative_quota():
    """R4-nit 实现语义锁定：不可解析体也计入 capture_bytes 并触发累计闸门。

    防洪语义：不能改成"只在解析成功后累加"，否则连续坏体可绕过累计上限；
    文档按此实现对口径（而不是反过来放宽实现）。
    """
    body = b"\xff\xfe\xfd not-utf8"
    capture = _make_capture(max_total_bytes=len(body) * 2 + 1)

    first = _route("POST", "/ingest/console", post_data_buffer=body)
    await capture.route_handler(first)
    assert first.fulfilled["status"] == 400, first.fulfilled
    assert capture.capture_bytes == len(body), capture.capture_bytes
    assert capture.undecodable_bodies == 1

    second = _route("POST", "/ingest/console", post_data_buffer=body)
    await capture.route_handler(second)
    assert second.fulfilled["status"] == 400, second.fulfilled
    assert capture.capture_bytes == len(body) * 2, capture.capture_bytes

    third = _route("POST", "/ingest/console", post_data_buffer=body)
    await capture.route_handler(third)
    assert third.fulfilled["status"] == 413, third.fulfilled
    assert json.loads(third.fulfilled["body"])["reason"] == "total_bytes_exceeded"
    assert capture.over_total_requests == 1
    assert capture.undecodable_bodies == 2


@pytest.mark.asyncio
async def test_finish_executes_shared_flush_expression():
    """F1 单一事实源：finish 的冲刷必须执行 _FLUSH_SDK_JS，不得另写内联表达式。

    否则 node 层锁定的"缺 SDK 必须 false"语义会被内联实现静默绕过。
    """
    from app.mcp.tools.auto_test_api import _FLUSH_SDK_JS

    capture = _make_capture()
    page = FakePage(FakeContext())
    page.evaluate_responses = [True, {"phase": "ready"}, []]
    await capture.finish(page, 0, set())

    assert _FLUSH_SDK_JS in page.evaluate_expressions, page.evaluate_expressions
