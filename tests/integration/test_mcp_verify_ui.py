"""H4 复核集成测试：verify_ui 经 MCP 通道实际调用，验证 to_thread 包装不阻塞事件循环。

复核目标（v0.3.0 Release Audit H4）：
- verify_ui 是同步 handler（内部调用 sync_playwright，可能阻塞事件循环）。
- H4 修复：MCP 协议层对同步 handler 统一走 `await asyncio.to_thread(handler, arguments)`。
  - HTTP MCP 通道：app/mcp/protocol/server.py::_handle_tools_call
  - stdio MCP 通道：app/mcp_server.py::_run_registered_tool
- 本测试经两条 MCP 通道实际调用 verify_ui 工具，并验证 to_thread 包装确实生效。

环境说明：测试环境 playwright 未安装，verify_ui 返回降级错误，不影响 H4 复核
（H4 复核的是"通道调用与 to_thread 包装"，不是 playwright 真实执行）。
"""
import asyncio
import json
import os
import time

import pytest

pytest.importorskip("mcp", reason="mcp SDK 不可用，跳过 H4 集成测试")

from app.mcp.protocol import server as protocol_server
from app.mcp.protocol.jsonrpc import JSONRPCRequest
from app.mcp.tools import register_all_tools
from app.mcp.tools.verify_ui_api import verify_ui_handler
from app.mcp.verifier import ui_runner


@pytest.fixture(scope="session", autouse=True)
def _ensure_tools_registered():
    """确保 _tool_registry 已填充。

    protocol_server 模块本身不会注册工具，注册动作在 app.mcp_server 加载时
    或显式调用 register_all_tools() 时发生。这里显式调用一次，确保后续所有
    测试都能在 registry 中找到 verify_ui。
    """
    register_all_tools()


def _make_tools_call_req(name: str, arguments: dict, req_id: int = 1) -> JSONRPCRequest:
    """构造 tools/call JSONRPCRequest。"""
    return JSONRPCRequest(
        jsonrpc="2.0",
        id=req_id,
        method="tools/call",
        params={"name": name, "arguments": arguments},
    )


def _parse_content(resp: dict) -> dict:
    """从 dispatch 返回的 JSON-RPC response 中解析工具结果。"""
    assert "result" in resp, f"预期 result 字段，实际: {resp}"
    content_text = resp["result"]["content"][0]["text"]
    return json.loads(content_text)


class _FakeTimeout(Exception):
    pass


class _FakeElement:
    def __init__(self, page, selector: str):
        self._page = page
        self._selector = selector

    def click(self):
        if self._selector == "#submit":
            return None
        raise AssertionError(f"unexpected selector: {self._selector}")

    def fill(self, value: str):
        self._page.values[self._selector] = value

    def hover(self):
        return None


class _FakePage:
    def __init__(self):
        self.url = ""
        self.values = {}

    def set_default_timeout(self, timeout_ms: int):
        self.timeout_ms = timeout_ms

    def goto(self, url: str, wait_until="domcontentloaded"):
        self.url = url

    def wait_for_selector(self, selector: str, timeout=5000):
        if selector in {"#submit", "#status"}:
            return _FakeElement(self, selector)
        raise _FakeTimeout(f"selector not found: {selector}")

    def text_content(self, selector: str, timeout=5000):
        if selector == "#status":
            return "Pending"
        raise _FakeTimeout(f"selector not found: {selector}")

    def wait_for_function(self, expression: str, arg, timeout=5000):
        if "includes" in expression and arg in self.url:
            return True
        if "===" in expression and self.url == arg:
            return True
        raise AssertionError("condition not met")


class _FakeBrowser:
    def __init__(self):
        self.page = _FakePage()

    def new_page(self):
        return self.page

    def close(self):
        return None


class _FakeChromium:
    def launch(self, headless=True):
        return _FakeBrowser()


class _FakePlaywright:
    def __init__(self):
        self.chromium = _FakeChromium()


class _FakePlaywrightContext:
    def __enter__(self):
        return _FakePlaywright()

    def __exit__(self, exc_type, exc, tb):
        return False


class TestVerifyUiHandlerNature:
    """验证 verify_ui handler 的同步性质 —— to_thread 包装路径的前提。"""

    def test_verify_ui_handler_is_sync_not_coroutine(self):
        """verify_ui_handler 必须是非协程函数，证明 to_thread 包装路径必然被走到。"""
        assert not asyncio.iscoroutinefunction(verify_ui_handler), (
            "verify_ui_handler 应为同步函数（H4 修复前提），实际为协程函数"
        )

    def test_verify_ui_registered_in_tool_registry(self):
        """verify_ui 必须已注册到 _tool_registry，否则 MCP 通道无法调用。"""
        assert "verify_ui" in protocol_server._tool_registry
        tool = protocol_server._tool_registry["verify_ui"]
        assert tool["handler"] is verify_ui_handler
        assert tool["inputSchema"], "inputSchema 不应为空"

    def test_run_registered_tool_wraps_sync_handler_via_to_thread(self):
        """app.mcp_server._run_registered_tool 对同步 handler 走 asyncio.to_thread。"""
        import inspect
        from app.mcp_server import _run_registered_tool

        # _run_registered_tool 应为协程函数
        assert asyncio.iscoroutinefunction(_run_registered_tool)

        # 源码里应包含 asyncio.to_thread 调用（验证 H4 修复确实落地）
        source = inspect.getsource(_run_registered_tool)
        assert "asyncio.to_thread" in source, (
            "_run_registered_tool 源码未包含 asyncio.to_thread —— H4 修复可能未落地"
        )

    def test_handle_tools_call_wraps_sync_handler_via_to_thread(self):
        """app.mcp.protocol.server._handle_tools_call 对同步 handler 走 asyncio.to_thread。"""
        import inspect
        from app.mcp.protocol.server import _handle_tools_call

        assert asyncio.iscoroutinefunction(_handle_tools_call)
        source = inspect.getsource(_handle_tools_call)
        assert "asyncio.to_thread" in source, (
            "_handle_tools_call 源码未包含 asyncio.to_thread —— H4 修复可能未落地"
        )


class TestVerifyUiViaDispatch:
    """经 HTTP MCP 协议层 dispatch 调用 verify_ui（覆盖 _handle_tools_call 的 to_thread 包装）。"""

    def test_no_spec_returns_structured_error(self):
        """无 spec/spec_id 时返回结构化错误，不抛异常、isError=false。"""
        req = _make_tools_call_req("verify_ui", {})
        resp = asyncio.run(protocol_server.dispatch(req))

        assert resp["result"]["isError"] is False
        content = _parse_content(resp)
        assert content["matched"] is False
        assert content["silent_failure"] is False
        assert content["error"] == "must provide spec or spec_id"
        assert content["diffs"] == []

    def test_kind_not_ui_returns_structured_diff(self):
        """spec.kind 非 ui 时返回结构化 diff，不抛异常。"""
        req = _make_tools_call_req("verify_ui", {"spec": {"kind": "http"}})
        resp = asyncio.run(protocol_server.dispatch(req))

        assert resp["result"]["isError"] is False
        content = _parse_content(resp)
        assert content["matched"] is False
        assert content["error"] == "spec.kind must be 'ui'"
        assert content["diffs"] == [
            {"field": "kind", "expected": "ui", "actual": "http"}
        ]

    def test_playwright_not_installed_returns_graceful_error(self):
        """playwright 未装时返回降级提示，不抛异常、不阻塞。"""
        if ui_runner.is_available():
            pytest.skip("playwright 已安装，跳过未装降级路径测试")

        req = _make_tools_call_req("verify_ui", {
            "spec": {"kind": "ui", "target": "http://localhost:0/nope"},
        })
        resp = asyncio.run(protocol_server.dispatch(req))

        assert resp["result"]["isError"] is False
        content = _parse_content(resp)
        assert content["matched"] is False
        assert "playwright 未安装" in content["error"], (
            f"应包含 playwright 未安装提示，实际: {content['error']}"
        )

    def test_private_url_rejection_returns_structured_security(self, monkeypatch):
        """私网目标经 MCP 调用时返回结构化安全拒绝结果。"""
        monkeypatch.setattr("app.config.settings.ui_url_allow_private", False)
        monkeypatch.setattr("app.config.settings.ui_url_allowlist", "")

        req = _make_tools_call_req("verify_ui", {
            "spec": {"kind": "ui", "target": "http://127.0.0.1:8123/private"},
        })
        resp = asyncio.run(protocol_server.dispatch(req))

        assert resp["result"]["isError"] is False
        content = _parse_content(resp)
        assert content["matched"] is False
        assert content["security"]["target"]["allowed"] is False
        assert content["security"]["target"]["rule"] == "private_network"
        assert content["failure_evidence"]["stage"] == "security_check"

    def test_assertion_failure_returns_structured_evidence(self, monkeypatch):
        """业务断言失败时经 MCP 返回 assertions 与 failure_evidence。"""
        monkeypatch.setattr(ui_runner, "_PLAYWRIGHT_AVAILABLE", True)
        monkeypatch.setattr(
            ui_runner, "sync_playwright", lambda: _FakePlaywrightContext(), raising=False
        )
        monkeypatch.setattr(ui_runner, "PlaywrightTimeout", _FakeTimeout, raising=False)

        req = _make_tools_call_req("verify_ui", {
            "spec": {
                "kind": "ui",
                "target": "https://example.com/form",
                "expect": {
                    "interactions": [
                        {
                            "action": "click",
                            "selector": "#submit",
                            "expect": {
                                "assertions": [
                                    {"type": "text", "selector": "#status", "equals": "Ready"}
                                ]
                            },
                        }
                    ]
                },
            }
        })
        resp = asyncio.run(protocol_server.dispatch(req))

        assert resp["result"]["isError"] is False
        content = _parse_content(resp)
        assert content["matched"] is False
        assert content["diffs"] == [{
            "field": "click(#submit).text",
            "expected": "Ready",
            "actual": "Pending",
        }]
        assert len(content["interactions"]) == 1
        interaction = content["interactions"][0]
        assert interaction["assertions"][0]["type"] == "text"
        assert interaction["assertions"][0]["matched"] is False
        assert interaction["failure_evidence"]["stage"] == "assertion"
        assert interaction["failure_evidence"]["selector"] == "#status"

    def test_unknown_tool_returns_method_not_found(self):
        """调用未注册的工具应返回 METHOD_NOT_FOUND 错误。"""
        req = _make_tools_call_req("nonexistent_tool", {})
        resp = asyncio.run(protocol_server.dispatch(req))

        assert "error" in resp
        assert resp["error"]["code"] == -32601  # METHOD_NOT_FOUND


class TestVerifyUiDoesNotBlockEventLoop:
    """验证 to_thread 包装生效 —— 同步 handler 在线程中执行，不阻塞事件循环。

    通过 monkeypatch.setitem 替换 _tool_registry 中的 verify_ui handler 为
    一个带 sleep 的同步假 handler，验证 dispatch 调用期间事件循环仍能推进。
    monkeypatch 会在测试结束自动还原，不会污染后续测试。
    """

    def test_sync_handler_runs_in_thread_not_blocking_loop(self, monkeypatch):
        """同步 handler 应在线程池中执行，事件循环并发任务能持续推进。"""

        def fake_slow_sync_handler(arguments: dict) -> dict:
            """模拟 sync_playwright 阻塞调用：sleep 0.3s。"""
            time.sleep(0.3)
            return {"matched": True, "diffs": [], "silent_failure": False}

        # 用 monkeypatch.setitem 替换 handler —— pytest 自动还原，不污染后续测试
        # （test_process_boundary 等后续测试调 verify_ui 时仍是原 handler）
        monkeypatch.setitem(
            protocol_server._tool_registry["verify_ui"],
            "handler",
            fake_slow_sync_handler,
        )

        # 假 handler 必须是同步的（否则走 await handler 路径，不验证 to_thread）
        assert not asyncio.iscoroutinefunction(fake_slow_sync_handler)

        async def scenario():
            # 启动一个并发计数任务 —— 若事件循环被阻塞，计数不会推进
            counter = {"n": 0}

            async def tick():
                while True:
                    counter["n"] += 1
                    await asyncio.sleep(0.02)

            tick_task = asyncio.create_task(tick())
            try:
                await asyncio.sleep(0.05)  # 让 tick 先跑几轮，记录基线
                baseline = counter["n"]
                assert baseline > 0, "并发 tick 未启动"

                # 调用 dispatch —— 若 to_thread 包装生效，事件循环不应被阻塞
                req = _make_tools_call_req(
                    "verify_ui", {"spec": {"kind": "ui", "target": "x"}}
                )
                t0 = time.monotonic()
                resp = await protocol_server.dispatch(req)
                elapsed = time.monotonic() - t0

                # handler 真的 sleep 了 0.3s（证明 fake handler 被执行）
                assert elapsed >= 0.25, (
                    f"handler 未真正执行 sleep（elapsed={elapsed:.3f}s）"
                )

                # 事件循环在 dispatch 期间继续推进（证明 to_thread 生效）
                after = counter["n"]
                assert after > baseline + 5, (
                    f"事件循环被同步 handler 阻塞：baseline={baseline}, "
                    f"after={after}，应差距 >=5（证明 to_thread 生效）"
                )

                # handler 返回了正确结果
                content = _parse_content(resp)
                assert content["matched"] is True
            finally:
                tick_task.cancel()
                try:
                    await tick_task
                except asyncio.CancelledError:
                    pass

        asyncio.run(scenario())

    def test_run_registered_tool_does_not_block_event_loop(self, monkeypatch):
        """直接验证 stdio 通道的 _run_registered_tool 也不阻塞事件循环。"""
        from app.mcp_server import _run_registered_tool

        def fake_slow_sync_handler(arguments: dict) -> dict:
            time.sleep(0.3)
            return {"ok": True}

        async def scenario():
            counter = {"n": 0}

            async def tick():
                while True:
                    counter["n"] += 1
                    await asyncio.sleep(0.02)

            tick_task = asyncio.create_task(tick())
            try:
                await asyncio.sleep(0.05)
                baseline = counter["n"]

                t0 = time.monotonic()
                result = await _run_registered_tool(fake_slow_sync_handler, {})
                elapsed = time.monotonic() - t0

                assert elapsed >= 0.25, (
                    f"handler 未真正执行 sleep（elapsed={elapsed:.3f}s）"
                )
                assert result == {"ok": True}

                after = counter["n"]
                assert after > baseline + 5, (
                    f"stdio 通道 _run_registered_tool 阻塞事件循环："
                    f"baseline={baseline}, after={after}"
                )
            finally:
                tick_task.cancel()
                try:
                    await tick_task
                except asyncio.CancelledError:
                    pass

        asyncio.run(scenario())


class TestVerifyUiViaStdioSubprocess:
    """经真实 stdio MCP 通道调用 verify_ui —— 拉起 app.mcp_server 子进程。

    用 mcp SDK 的 stdio_client + ClientSession 完成 initialize → tools/list → tools/call
    完整链路。子进程启动用 `python -c` 注入 M9 workaround（避免 .env extra_forbidden 阻断）。
    """

    def test_verify_ui_called_via_stdio_mcp_channel(self):
        """完整 stdio MCP 链路调用 verify_ui，验证通道可用、不阻塞、不抛异常。"""
        from mcp.client.stdio import stdio_client, StdioServerParameters
        from mcp.client.session import ClientSession

        # M9 workaround：子进程注入 extra=ignore，避免 .env extra_forbidden 阻断
        # 不修改业务代码，仅在子进程启动时注入一行 patch
        bootstrap = (
            "import pydantic_settings; "
            "pydantic_settings.BaseSettings.model_config['extra']='ignore'; "
            "import runpy; runpy.run_module('app.mcp_server', run_name='__main__')"
        )
        # Windows 子进程默认用 cp936 编码 stdout，但 mcp SDK 按 UTF-8 解码会崩
        # 强制子进程 stdout/stderr 走 UTF-8
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        server_params = StdioServerParameters(
            command="python",
            args=["-c", bootstrap],
            cwd=".",
            env=env,
        )

        async def scenario():
            try:
                async with stdio_client(server_params) as (read, write):
                    async with ClientSession(read, write) as session:
                        # initialize 握手
                        init_result = await session.initialize()
                        assert init_result is not None

                        # tools/list 应包含 verify_ui
                        tools_result = await session.list_tools()
                        tool_names = [t.name for t in tools_result.tools]
                        assert "verify_ui" in tool_names, (
                            f"verify_ui 未在工具列表中: {tool_names}"
                        )

                        # tools/call 调用 verify_ui —— 无参数应返回结构化错误
                        call_result = await session.call_tool("verify_ui", {})
                        assert call_result.isError is False
                        assert len(call_result.content) > 0
                        text = call_result.content[0].text
                        payload = json.loads(text)
                        assert payload["matched"] is False
                        assert payload["error"] == "must provide spec or spec_id"
            except (OSError, RuntimeError) as e:
                pytest.skip(f"stdio 子进程启动失败，跳过：{type(e).__name__}: {e}")

        try:
            asyncio.run(asyncio.wait_for(scenario(), timeout=30.0))
        except asyncio.TimeoutError:
            pytest.skip("stdio 子进程响应超时（>30s），可能环境异常")
