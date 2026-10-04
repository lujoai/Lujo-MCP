"""
v0.9.8 auto_test 自动埋点单元测试。

验收标准（作者定案设计）：
1. init script 构造函数产出自包含 JS：注入 <script src="http://{host}:{port}/ai-debug.js">
   并在其加载后 AiDebug.init({ endpoint: "http://{host}:{port}" })；
2. settings.auto_inject_sdk=False 时完全跳过注入（构造函数返回 None）；
3. auto_test 的 page 创建点把 init script 装到每个打开的页面上
   （用假 playwright 对象验证调用参数，无真实浏览器依赖）。

全程不启动真实浏览器、不访问网络；settings 经 monkeypatch 改写单例属性。
"""
import sys
import types
from types import SimpleNamespace

import pytest

from app.config import settings


# ── 构造函数单元测试 ──


def test_build_script_contains_endpoint_and_sdk_path(monkeypatch):
    """init script 应包含实时 http_host/http_port 拼出的 SDK 地址与 init endpoint。"""
    from app.mcp.tools.auto_test_api import _build_sdk_init_script

    monkeypatch.setattr(settings, "http_host", "127.0.0.1")
    monkeypatch.setattr(settings, "http_port", 8999)

    script = _build_sdk_init_script()

    assert script is not None
    # endpoint 使用 http_host/http_port 实时值
    assert "http://127.0.0.1:8999" in script
    # SDK 脚本路径（实现以 ENDPOINT + '/ai-debug.js' 拼接，两者不漂移）
    assert "/ai-debug.js" in script
    # init 的 endpoint 指向同一地址（SDK POST /ingest/* 的基底）
    assert "AiDebug.init" in script
    assert "http://127.0.0.1:8999" in script
    # init script 运行时 document.head 可能为 null —— 必须回落 documentElement
    assert "documentElement" in script


def test_build_script_disabled_returns_none(monkeypatch):
    """开关关闭时必须完全跳过注入：返回 None，而非空串或半成品。"""
    from app.mcp.tools.auto_test_api import _build_sdk_init_script

    monkeypatch.setattr(settings, "auto_inject_sdk", False)

    assert _build_sdk_init_script() is None


# ── 假 playwright：验证 page 创建点接线 ──


class FakePage:
    """记录 add_init_script / 事件注册 / 导航调用的最小 page 替身。"""

    def __init__(self):
        self.init_scripts = []
        self.events = []
        self.context = SimpleNamespace()
        # v1.0.x：auto_test 需在 context 上注册回传拦截路由
        self.context.route_calls = []
        self.context.route = self._record_route
        self.url = "http://127.0.0.1:8765/page"

    async def _record_route(self, pattern, handler):
        self.context.route_calls.append((pattern, handler))

    async def add_init_script(self, script=None, path=None):
        self.init_scripts.append(script)

    def on(self, event, handler):
        self.events.append(event)

    async def goto(self, url, **kwargs):
        return None

    async def query_selector_all(self, selector):
        return []

    async def wait_for_timeout(self, ms):
        return None

    async def evaluate(self, expression, arg=None):
        return None


class FakeBrowser:
    def __init__(self, page):
        self._page = page
        self.closed = False

    async def new_page(self):
        return self._page

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser):
        self._browser = browser

    async def launch(self, **kwargs):
        return self._browser


class FakePlaywright:
    """async_playwright() 异步上下文管理器替身。"""

    def __init__(self, browser):
        self.chromium = FakeChromium(browser)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _install_fake_playwright(monkeypatch, page):
    """把假 playwright.async_api.async_playwright 注入 sys.modules。

    auto_test_api._run 在调用时才 `from playwright.async_api import
    async_playwright`，故 setitem 生效；真实 playwright 已安装，
    monkeypatch 会在用例结束后恢复原条目。
    """
    browser = FakeBrowser(page)
    pw_module = types.ModuleType("playwright")
    api_module = types.ModuleType("playwright.async_api")
    api_module.async_playwright = lambda: FakePlaywright(browser)
    pw_module.async_api = api_module
    monkeypatch.setitem(sys.modules, "playwright", pw_module)
    monkeypatch.setitem(sys.modules, "playwright.async_api", api_module)
    return browser


async def _noop_async_guard(ctx):
    return None


@pytest.fixture()
def _stub_auto_test_env(monkeypatch):
    """屏蔽 _run 的浏览器启动解析与 SSRF 守卫（纯逻辑测试不触真实实现）。"""
    import app.runtime.verifier.browser_launcher as launcher_mod
    import app.runtime.verifier.ui_runner as ui_runner_mod

    monkeypatch.setattr(launcher_mod, "resolve_launch_kwargs", dict)
    monkeypatch.setattr(ui_runner_mod, "install_ssrf_guard_async", _noop_async_guard)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_auto_test_env")
async def test_auto_test_page_gets_init_script(monkeypatch):
    """开关开启：auto_test 打开的页面必须装上 init script（接线验收）。"""
    from app.mcp.tools import auto_test_api

    monkeypatch.setattr(settings, "auto_inject_sdk", True)
    monkeypatch.setattr(settings, "http_host", "127.0.0.1")
    monkeypatch.setattr(settings, "http_port", 8999)

    page = FakePage()
    _install_fake_playwright(monkeypatch, page)

    result = await auto_test_api._run(
        "http://127.0.0.1:8765/page", max_actions=1,
        capture_console=True, capture_network=True,
    )

    assert "error" not in result
    assert len(page.init_scripts) == 1
    assert "http://127.0.0.1:8999" in page.init_scripts[0]
    assert "AiDebug.init" in page.init_scripts[0]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_auto_test_env")
async def test_auto_test_page_skips_injection_when_disabled(monkeypatch):
    """开关关闭：page 创建点完全不调用 add_init_script。"""
    from app.mcp.tools import auto_test_api

    monkeypatch.setattr(settings, "auto_inject_sdk", False)

    page = FakePage()
    _install_fake_playwright(monkeypatch, page)

    result = await auto_test_api._run(
        "http://127.0.0.1:8765/page", max_actions=1,
        capture_console=True, capture_network=True,
    )

    assert "error" not in result
    assert page.init_scripts == []
