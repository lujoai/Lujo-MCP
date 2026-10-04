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
import json
import re
import sys
import types
from types import SimpleNamespace

import pytest

from app.config import settings


# ── Playwright 替身（支持 context.route 的 unshift 语义与请求分发） ──


class FakeRequest:
    def __init__(self, url: str, method: str = "GET", post_data: str | None = None,
                 origin: str = "http://127.0.0.1:8765"):
        self.url = url
        self.method = method
        self.headers = {"origin": origin}
        self._post_data = post_data

    @property
    def post_data(self) -> str | None:
        return self._post_data


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

    async def dispatch(self, url: str, method: str = "GET", post_data: str | None = None) -> FakeRoute:
        route = FakeRoute(FakeRequest(url, method, post_data))
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
            "http://127.0.0.1:8999/ingest/batch", method="POST", post_data=batch
        )
        return None

    async def query_selector_all(self, selector):
        return []

    async def wait_for_timeout(self, ms):
        return None

    async def evaluate(self, expression, arg=None):
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
        {"phase": "ready", "buffered": 0, "replayed": 1},  # 状态 evaluate
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
