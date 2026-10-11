"""auto_test 页面最终落点、标题元数据与导航失败阶段测试。

覆盖任务书范围与边界：
1. _sanitize_page_url：URL 用户凭据（userinfo）脱敏、敏感 query 参数掩码、
   敏感 fragment 掩码、非字符串容错、2048 字符硬性截断。
2. _extract_page_info：有界提取 final_url 与 title、标题脱敏、300 字符限制、
   页面关闭或 title() 异常时防御性回退 None、不抛二次异常。
3. auto_test_handler 成功路径：保留既有 url，附加 page_info（final_url, title）。
4. auto_test_handler 重定向路径：url 保持原始请求地址，page_info.final_url
   如实反映观察到的重定向落点。
5. auto_test_handler 导航失败路径：保持既有 error 与 isError 语义，
   附加 failure_stage="navigation" 与 error_type。
6. stdio / HTTP 统一工具失败契约（is_tool_failure_result）校验。
"""
import asyncio
import sys
import types
from types import SimpleNamespace

import pytest

from app.config import settings
from app.mcp.protocol.tool_errors import is_tool_failure_result
from app.mcp.tools import auto_test_api
from app.mcp.tools.auto_test_api import _extract_page_info, _sanitize_page_url


# ── 1. _sanitize_page_url 单元测试 ──────────────────────────────


def test_sanitize_page_url_normal():
    """正常 URL 应原样保留（去除首尾空白）。"""
    url = "https://example.com/dashboard/orders?tab=active&page=2"
    assert _sanitize_page_url(url) == url
    assert _sanitize_page_url(f"  {url}  ") == url


def test_sanitize_page_url_userinfo():
    """URL 中的用户名密码必须脱敏，防止凭据泄露。"""
    url_with_pass = "http://admin:secret123@127.0.0.1:8080/api/orders"
    sanitized = _sanitize_page_url(url_with_pass)
    assert sanitized is not None
    assert "secret123" not in sanitized
    assert "admin:***@127.0.0.1:8080" in sanitized

    url_user_only = "http://admin@127.0.0.1:8080/api/orders"
    sanitized_user = _sanitize_page_url(url_user_only)
    assert "***@127.0.0.1:8080" in sanitized_user

    # 用户名含未转义 @（如邮箱型账号）应正确按最后一个 @ 划分 userinfo 与 host
    url_email_user = "http://user@example.com:secret123@127.0.0.1:8080/api/orders"
    sanitized_email_user = _sanitize_page_url(url_email_user)
    assert sanitized_email_user is not None
    assert "secret123" not in sanitized_email_user
    assert "***@127.0.0.1:8080" in sanitized_email_user


def test_sanitize_page_url_sensitive_query():
    """URL 查询参数中的敏感字段（token, password, key 等）必须掩码。"""
    url = "http://localhost:3000/callback?code=ok&token=xyz789&password=mypass&public_id=123"
    sanitized = _sanitize_page_url(url)
    assert sanitized is not None
    assert "xyz789" not in sanitized
    assert "mypass" not in sanitized
    assert "token=***" in sanitized or "token=%2A%2A%2A" in sanitized
    assert "password=***" in sanitized or "password=%2A%2A%2A" in sanitized
    assert "code=ok" in sanitized
    assert "public_id=123" in sanitized


def test_sanitize_page_url_fragment_and_path():
    """URL fragment 中的敏感信息需脱敏。"""
    url = "http://localhost:3000/orders#token=secret_frag_token"
    sanitized = _sanitize_page_url(url)
    assert sanitized is not None
    assert "secret_frag_token" not in sanitized


def test_sanitize_page_url_edge_cases():
    """非字符串、None、空串安全回退 None；超长 URL 截断至 2048。"""
    assert _sanitize_page_url(None) is None
    assert _sanitize_page_url("") is None
    assert _sanitize_page_url("   ") is None
    assert _sanitize_page_url(123) is None

    long_url = "http://example.com/" + "a" * 3000
    sanitized_long = _sanitize_page_url(long_url)
    assert sanitized_long is not None
    assert len(sanitized_long) == 2048


# ── 2. _extract_page_info 单元测试 ──────────────────────────────


@pytest.mark.asyncio
async def test_extract_page_info_normal():
    """正常页面对象应提取最终 URL 与脱敏标题。"""
    class _Page:
        url = "http://127.0.0.1:3000/dashboard"

        async def title(self):
            return "Dashboard - MyApp"

        def is_closed(self):
            return False

    info = await _extract_page_info(_Page())
    assert info == {
        "final_url": "http://127.0.0.1:3000/dashboard",
        "title": "Dashboard - MyApp",
    }


@pytest.mark.asyncio
async def test_extract_page_info_title_truncation_and_redaction():
    """页面标题超出 300 字符应截断；标题中的敏感信息应脱敏。"""
    class _Page:
        url = "http://127.0.0.1:3000/profile"

        async def title(self):
            return "Profile: password=supersecret " + ("X" * 400)

        def is_closed(self):
            return False

    info = await _extract_page_info(_Page())
    assert info["final_url"] == "http://127.0.0.1:3000/profile"
    assert "supersecret" not in (info["title"] or "")
    assert len(info["title"] or "") <= 300


@pytest.mark.asyncio
async def test_extract_page_info_page_closed():
    """页面已关闭时优雅返回 None，不伪造值，不抛异常。"""
    class _Page:
        url = "http://127.0.0.1:3000/closed"

        def is_closed(self):
            return True

        async def title(self):
            raise RuntimeError("target closed")

    info = await _extract_page_info(_Page())
    assert info == {"final_url": None, "title": None}


@pytest.mark.asyncio
async def test_extract_page_info_title_exception():
    """title() 调用抛异常时，不影响 final_url，title 降级为 None。"""
    class _Page:
        url = "http://127.0.0.1:3000/active"

        def is_closed(self):
            return False

        async def title(self):
            raise TimeoutError("title timeout")

    info = await _extract_page_info(_Page())
    assert info["final_url"] == "http://127.0.0.1:3000/active"
    assert info["title"] is None


@pytest.mark.asyncio
async def test_extract_page_info_none():
    """传入 None 时安全返回全 None 结构。"""
    info = await _extract_page_info(None)
    assert info == {"final_url": None, "title": None}


# ── 3. auto_test_handler 替身 Playwright 集成测试 ───────────────


class _MockContext:
    def __init__(self):
        self.routes = []

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))


class _MockPage:
    def __init__(self, initial_url="http://127.0.0.1:8765/page", goto_exc=None):
        self.context = _MockContext()
        self.url = initial_url
        self.goto_exc = goto_exc
        self.init_scripts = []
        self.page_title = "My App Title"
        self._is_closed = False

    async def add_init_script(self, script=None, path=None):
        self.init_scripts.append(script)

    def on(self, event, handler):
        pass

    async def goto(self, url, **kwargs):
        if self.goto_exc:
            raise self.goto_exc
        return None

    async def query_selector_all(self, selector):
        return []

    async def wait_for_timeout(self, ms):
        return None

    async def evaluate(self, expression, arg=None):
        return None

    async def title(self):
        return self.page_title

    def is_closed(self):
        return self._is_closed


class _MockBrowser:
    def __init__(self, page):
        self._page = page
        self.closed = False

    async def new_page(self):
        return self._page

    async def close(self):
        self.closed = True


def _install_mock_playwright(monkeypatch, page: _MockPage) -> _MockBrowser:
    browser = _MockBrowser(page)
    pw_mod = types.ModuleType("playwright")
    api_mod = types.ModuleType("playwright.async_api")

    class _MockPlaywrightContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        @property
        def chromium(self):
            return SimpleNamespace(launch=self._launch)

        async def _launch(self, **kwargs):
            return browser

    api_mod.async_playwright = lambda: _MockPlaywrightContext()
    pw_mod.async_api = api_mod
    monkeypatch.setitem(sys.modules, "playwright", pw_mod)
    monkeypatch.setitem(sys.modules, "playwright.async_api", api_mod)
    return browser


@pytest.fixture()
def _mock_env(monkeypatch):
    import app.runtime.verifier.browser_launcher as launcher_mod
    import app.runtime.verifier.ui_runner as ui_runner_mod

    monkeypatch.setattr(launcher_mod, "resolve_launch_kwargs", dict)

    async def _noop(ctx):
        return None

    monkeypatch.setattr(ui_runner_mod, "install_ssrf_guard_async", _noop)


@pytest.mark.asyncio
async def test_auto_test_success_returns_page_info(monkeypatch, _mock_env):
    """正常访问路径：返回 url、page_info（final_url, title），成功结果无 error。"""
    page = _MockPage(initial_url="http://127.0.0.1:8765/home")
    page.page_title = "Home Page"
    browser = _install_mock_playwright(monkeypatch, page)

    res = await auto_test_api.auto_test_handler({
        "url": "http://127.0.0.1:8765/home",
        "max_actions": 5,
    })

    assert "error" not in res
    assert res["url"] == "http://127.0.0.1:8765/home"
    assert "page_info" in res
    assert res["page_info"] == {
        "final_url": "http://127.0.0.1:8765/home",
        "title": "Home Page",
    }
    assert "sdk_capture" in res
    assert browser.closed is True
    # MCP 失败契约验证：无 error 键即非失败
    assert is_tool_failure_result(res) is False


@pytest.mark.asyncio
async def test_auto_test_redirection_reflects_final_url(monkeypatch, _mock_env):
    """重定向落点：请求 /orders 被跳转到 /login 时，url 保留请求地址，page_info 反映登录落点。"""
    page = _MockPage()
    # 模拟网络重定向后 page.url 变为登录地址，且携带带 token 的 query
    page.url = "http://127.0.0.1:8765/login?redirect=%2Forders&token=user_session_token"
    page.page_title = "User Login - MyApp"
    _install_mock_playwright(monkeypatch, page)

    res = await auto_test_api.auto_test_handler({
        "url": "http://127.0.0.1:8765/orders",
        "max_actions": 0,
    })

    assert "error" not in res
    assert res["url"] == "http://127.0.0.1:8765/orders"
    assert res["page_info"]["final_url"] is not None
    assert "/login?redirect=%2Forders" in res["page_info"]["final_url"]
    assert "user_session_token" not in res["page_info"]["final_url"]
    assert "token=***" in res["page_info"]["final_url"] or "token=%2A%2A%2A" in res["page_info"]["final_url"]
    assert res["page_info"]["title"] == "User Login - MyApp"
    assert is_tool_failure_result(res) is False


@pytest.mark.asyncio
async def test_auto_test_navigation_failure_returns_failure_stage_and_type(monkeypatch, _mock_env):
    """导航失败：保留 error='Tool execution failed'，附加 failure_stage='navigation' 与 error_type。"""
    class _CustomNavigationTimeout(TimeoutError):
        pass

    page = _MockPage(
        initial_url="about:blank",
        goto_exc=_CustomNavigationTimeout("Navigation timeout 30000ms exceeded"),
    )
    page.page_title = "Loading..."
    browser = _install_mock_playwright(monkeypatch, page)

    res = await auto_test_api.auto_test_handler({
        "url": "http://127.0.0.1:8765/unreachable",
        "max_actions": 0,
    })

    # 保留原错误字段
    assert res["error"] == "Tool execution failed"
    # 结构化失败阶段与异常类型
    assert res["failure_stage"] == "navigation"
    assert res["error_type"] == "_CustomNavigationTimeout"
    assert res["url"] == "http://127.0.0.1:8765/unreachable"
    # 附带尝试读取到的页面信息
    assert "page_info" in res
    assert res["page_info"]["final_url"] == "about:blank"
    assert res["page_info"]["title"] == "Loading..."
    assert "sdk_capture" in res
    assert browser.closed is True

    # MCP 失败契约验证：包含非空 error 键，被正确识别为工具执行失败
    assert is_tool_failure_result(res) is True


@pytest.mark.asyncio
async def test_auto_test_navigation_failure_closed_page_graceful(monkeypatch, _mock_env):
    """导航异常导致页面已关闭时，page_info 优雅回退 None，不引发二次崩溃。"""
    class _ConnectionCrash(ConnectionError):
        pass

    page = _MockPage(
        initial_url="http://127.0.0.1:8765/crashed",
        goto_exc=_ConnectionCrash("Connection reset"),
    )
    page._is_closed = True
    _install_mock_playwright(monkeypatch, page)

    res = await auto_test_api.auto_test_handler({
        "url": "http://127.0.0.1:8765/crashed",
        "max_actions": 0,
    })

    assert res["error"] == "Tool execution failed"
    assert res["failure_stage"] == "navigation"
    assert res["error_type"] == "_ConnectionCrash"
    assert res["page_info"] == {"final_url": None, "title": None}
    assert is_tool_failure_result(res) is True
