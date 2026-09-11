"""stdio 与 HTTP 传输层工具调用口径一致性回归（R8）。

覆盖三条此前只在 HTTP 侧生效、stdio 侧缺失的规则：
1. inputSchema 入参校验 —— 官方 MCP SDK 只对 tools/list 中出现的工具做
   jsonschema 校验，`agent_visible=False` 的 SDK 上报类工具在 stdio 上完全
   绕过校验，HTTP 侧则一律先校验再执行；
2. 轻/重型双池槽位门控 —— stdio 此前无任何并发上限，重型工具可被无界并发
   拉起浏览器子进程；
3. MCP 工具指标 —— mcp_tool_* 指标此前只反映 HTTP 流量。

同时验证槽位在成功/超时/异常/业务失败四条路径上都被归还（无泄漏）。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams

from app.config import settings
from app.mcp.protocol import server as protocol
from app.mcp.protocol.jsonrpc import JSONRPCRequest
from app.mcp.protocol.server import _handle_tools_call, _tool_registry, register_tool


def _stdio_request(name: str, arguments: dict) -> CallToolRequest:
    return CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )


async def _stdio_call(name: str, arguments: dict) -> dict:
    import app.mcp_server as stdio

    result = await stdio.server.request_handlers[CallToolRequest](_stdio_request(name, arguments))
    return result.model_dump(mode="json")


async def _http_call(name: str, arguments: dict) -> dict:
    return await _handle_tools_call(
        JSONRPCRequest(id="parity-1", method="tools/call",
                       params={"name": name, "arguments": arguments})
    )


def _stdio_payload(dumped: dict) -> dict:
    return json.loads(dumped["content"][0]["text"])


# 这些工具刻意不进 tools/list（agent_visible=False），因此不会被官方 SDK
# 的 jsonschema 校验覆盖 —— 正是 stdio 校验缺口的触发面。
SDK_ONLY_TOOL = "ingest_console"


@pytest.fixture
def _registered():
    from app.mcp.tools import register_all_tools
    from app.runtime.core.storage import factory as storage_factory

    register_all_tools()
    storage_factory._trace_store = None
    yield
    storage_factory._trace_store = None


class TestArgumentValidationParity:
    def test_tool_under_test_is_not_agent_visible(self, _registered):
        """守卫：若该工具被改回可见，本文件的校验用例就失去意义。"""
        tool = _tool_registry[SDK_ONLY_TOOL]
        assert tool.get("agent_visible", True) is False
        assert SDK_ONLY_TOOL not in [t["name"] for t in protocol.get_agent_visible_tools()]

    @pytest.mark.asyncio
    async def test_missing_required_param_rejected_by_both_transports(self, _registered):
        """缺必填参数：两侧都拒绝，且都不执行 handler。"""
        http = await _http_call(SDK_ONLY_TOOL, {})
        assert http["error"]["code"] == protocol.INVALID_PARAMS
        assert "message" in http["error"]["message"]

        stdio_dump = await _stdio_call(SDK_ONLY_TOOL, {})
        assert stdio_dump["isError"] is True
        payload = _stdio_payload(stdio_dump)
        assert payload["error_code"] == "INVALID_PARAMS"
        assert "message" in payload["error"]
        # 修复前：stdio 直接执行 handler，落一条空消息记录并回 saved=True
        assert "saved" not in payload

    @pytest.mark.asyncio
    async def test_null_typed_param_rejected_by_both_transports(self, _registered):
        http = await _http_call(SDK_ONLY_TOOL, {"message": None})
        assert http["error"]["code"] == protocol.INVALID_PARAMS

        stdio_dump = await _stdio_call(SDK_ONLY_TOOL, {"message": None})
        assert stdio_dump["isError"] is True
        assert _stdio_payload(stdio_dump)["error_code"] == "INVALID_PARAMS"

    @pytest.mark.asyncio
    async def test_valid_arguments_still_execute_on_stdio(self, _registered):
        """校验补齐后正常上报路径不能被收紧：仍然执行并落库。"""
        dumped = await _stdio_call(SDK_ONLY_TOOL, {"message": "boom", "level": "error"})
        assert dumped["isError"] is False
        assert _stdio_payload(dumped)["saved"] is True


class TestHeavySlotGating:
    @pytest.mark.asyncio
    async def test_stdio_honours_heavy_capacity(self, _registered, monkeypatch):
        """重型槽位被 HTTP 侧占满时，stdio 必须同样 fast-fail 而不是再起一个子进程。"""
        import app.mcp_server as stdio

        spawned = []

        def _blocking_handler(_arguments):
            spawned.append(1)
            return {"ok": True}

        register_tool(
            "parity_heavy_tool",
            description="test-only heavy tool",
            handler=_blocking_handler,
            inputSchema={"type": "object"},
            heavy=True,
        )
        monkeypatch.setattr(stdio.settings, "tool_busy_queue_timeout", 0.0)
        try:
            free = protocol._heavy_pool.semaphore._value
            assert free > 0, "前置条件：需有空闲重型槽位"
            held = []
            for _ in range(free):
                await protocol._heavy_pool.semaphore.acquire()
                held.append(1)
            try:
                dumped = await _stdio_call("parity_heavy_tool", {})
                payload = _stdio_payload(dumped)
                assert dumped["isError"] is True
                assert payload["error_code"] == "TOOL_BUSY"
                # 修复前：stdio 无门控，handler 会被真正执行
                assert spawned == []

                # 同一状态下 HTTP 也必须给出同一语义（两传输共用一个门控）
                http = await _http_call("parity_heavy_tool", {})
                assert http["result"]["isError"] is True
                assert http["result"]["error_code"] == "TOOL_BUSY"
            finally:
                for _ in held:
                    protocol._heavy_pool.semaphore.release()
        finally:
            _tool_registry.pop("parity_heavy_tool", None)

    @pytest.mark.asyncio
    async def test_gating_applies_to_async_tools_too(self, _registered, monkeypatch):
        """async handler 同样受门控（此前 stdio 直接 await 无任何上限）。"""
        import app.mcp_server as stdio

        runs = []

        async def _async_tool(_arguments):
            runs.append(1)
            return {"ok": True}

        register_tool(
            "parity_async_tool",
            description="test-only async tool",
            handler=_async_tool,
            inputSchema={"type": "object"},
            heavy=True,
        )
        monkeypatch.setattr(stdio.settings, "tool_busy_queue_timeout", 0.0)
        try:
            free = protocol._heavy_pool.semaphore._value
            for _ in range(free):
                await protocol._heavy_pool.semaphore.acquire()
            try:
                dumped = await _stdio_call("parity_async_tool", {})
                assert _stdio_payload(dumped)["error_code"] == "TOOL_BUSY"
                assert runs == []
            finally:
                for _ in range(free):
                    protocol._heavy_pool.semaphore.release()
        finally:
            _tool_registry.pop("parity_async_tool", None)


class TestSlotAccounting:
    """四条结束路径都必须归还槽位，否则池会被逐步吃掉直至恒 TOOL_BUSY。"""

    @staticmethod
    def _register(name, handler, **kwargs):
        register_tool(name, description="test-only", handler=handler,
                      inputSchema={"type": "object"}, **kwargs)

    @staticmethod
    async def _wait_slot_restored(before: int, timeout: float = 5.0) -> bool:
        """轮询等待槽位恢复（归还回调是异步的，不能只 sleep(0) 一次就断言）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if protocol._light_pool.semaphore._value >= before:
                return True
            await asyncio.sleep(0.01)
        return protocol._light_pool.semaphore._value >= before

    @pytest.mark.parametrize(
        "case",
        ["ok", "business_failure", "handler_exception", "timeout"],
    )
    @pytest.mark.asyncio
    async def test_stdio_releases_slot_on_every_path(self, _registered, monkeypatch, case):
        import app.mcp_server as stdio

        def _ok(_a):
            return {"ok": True}

        def _business_failure(_a):
            return {"error": "nope"}

        def _boom(_a):
            raise RuntimeError("boom")

        def _slow(_a):
            import time

            time.sleep(1.0)
            return {"ok": True}

        handlers = {
            "ok": _ok,
            "business_failure": _business_failure,
            "handler_exception": _boom,
            "timeout": _slow,
        }
        name = f"parity_slot_{case}"
        self._register(name, handlers[case])
        if case == "timeout":
            monkeypatch.setattr(stdio.settings, "tool_timeout_seconds", 0.05)
        try:
            before = protocol._light_pool.semaphore._value
            dumped = await _stdio_call(name, {})
            payload = _stdio_payload(dumped)
            if case == "ok":
                assert dumped["isError"] is False
            else:
                assert dumped["isError"] is True
                if case == "timeout":
                    assert payload["_timed_out"] is True

            if case == "timeout":
                # ── 改断言三件套（DESIGN C1 §0 / §3.1）──
                # 原断言：超时响应返回后 `await asyncio.sleep(0)` 即断言 `_value == before`。
                #   该断言通过恰恰依赖 mcp_server.py finally 里的**无条件 release**，
                #   即它编码了 B07「超时提前归还槽位 → 实际并发超发」这一缺陷本身。
                # 新断言：超时响应返回时槽位**仍被真实线程占用**（不得提前归还）；
                #   待真实线程终结（handler 睡 1.0s）后槽位才恢复。
                # 为什么：归还权从「awaiter 返回」移到「真实任务终结」（结算与归还分离）。
                await asyncio.sleep(0)
                assert protocol._light_pool.semaphore._value < before, (
                    "超时后槽位被提前归还：真实线程仍在运行，槽位必须继续记账"
                )
                assert await self._wait_slot_restored(before), "真实线程终结后槽位必须恢复"
            else:
                # ok / business_failure / handler_exception：线程同步终结，
                # 归还回调异步执行，轮询等待即可（不改变「必须归还」的语义）。
                assert await self._wait_slot_restored(before), "结束后槽位必须归还"
        finally:
            _tool_registry.pop(name, None)


class TestStdioRecordsToolMetrics:
    @pytest.mark.asyncio
    async def test_status_labels_match_http(self, _registered, monkeypatch):
        """stdio 调用写入与 HTTP 同名同状态的指标（此前 stdio 完全不埋点）。"""
        import app.mcp_server as stdio

        calls: list[tuple[str, str]] = []
        busy: list[tuple[str, str]] = []
        monkeypatch.setattr(stdio, "record_mcp_tool_call",
                            lambda name, status, duration=0.0: calls.append((name, status)))
        monkeypatch.setattr(stdio, "record_mcp_tool_busy",
                            lambda name, pool="light", wait=0.0: busy.append((name, pool)))

        await _stdio_call(SDK_ONLY_TOOL, {"message": "m"})
        assert (SDK_ONLY_TOOL, "ok") in calls
        calls.clear()

        await _stdio_call(SDK_ONLY_TOOL, {})
        assert (SDK_ONLY_TOOL, "invalid_params") in calls
        calls.clear()

        await _stdio_call("no_such_tool_at_all", {})
        assert ("no_such_tool_at_all", "error") in calls

        def _blocking(_a):
            return {"ok": True}

        register_tool("parity_metric_busy", description="t", handler=_blocking,
                      inputSchema={"type": "object"}, heavy=True)
        monkeypatch.setattr(stdio.settings, "tool_busy_queue_timeout", 0.0)
        try:
            free = protocol._heavy_pool.semaphore._value
            for _ in range(free):
                await protocol._heavy_pool.semaphore.acquire()
            try:
                await _stdio_call("parity_metric_busy", {})
            finally:
                for _ in range(free):
                    protocol._heavy_pool.semaphore.release()
            assert ("parity_metric_busy", "busy") in calls
            assert ("parity_metric_busy", "heavy") in busy
        finally:
            _tool_registry.pop("parity_metric_busy", None)
