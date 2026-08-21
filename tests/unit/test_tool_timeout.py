"""工具超时处理测试：验证同步/异步工具超时响应结构及 sync_future.cancel() 取消调度行为"""
import asyncio
import time
import pytest
from unittest.mock import MagicMock, patch

from app.config import settings
from app.mcp.protocol.server import _handle_tools_call, register_tool, _tool_registry
from app.mcp.protocol.jsonrpc import JSONRPCRequest


@pytest.fixture(autouse=True)
def _restore_registry():
    saved = dict(_tool_registry)
    yield
    _tool_registry.clear()
    _tool_registry.update(saved)


@pytest.mark.asyncio
async def test_sync_tool_timeout_cancels_future(monkeypatch):
    """验证同步工具超时时，封装的 Future 会被调用 cancel()，且返回结构正确。"""
    monkeypatch.setattr(settings, "tool_timeout_seconds", 0.05)

    def slow_sync_handler(args):
        time.sleep(0.2)
        return "done"

    register_tool("slow_sync_test", "slow sync tool", slow_sync_handler, inputSchema={"type": "object"})

    mock_future = None
    orig_run_in_executor = asyncio.get_running_loop().run_in_executor

    def wrapped_run_in_executor(executor, func, *args):
        nonlocal mock_future
        fut = orig_run_in_executor(executor, func, *args)
        real_cancel = fut.cancel
        spy_cancel = MagicMock(side_effect=real_cancel)
        fut.cancel = spy_cancel
        mock_future = fut
        return fut

    with patch.object(asyncio.get_running_loop(), "run_in_executor", side_effect=wrapped_run_in_executor):
        req = JSONRPCRequest(
            id="req-timeout-1",
            method="tools/call",
            params={"name": "slow_sync_test", "arguments": {}},
        )
        resp = await _handle_tools_call(req)

    result = resp.get("result", {})
    assert result.get("isError") is True
    assert result.get("error_code") == "TOOL_TIMEOUT"
    assert result.get("_timed_out") is True
    assert "已中止" in result.get("content", [{}])[0].get("text", "")
    assert mock_future is not None
    assert mock_future.cancel.called


@pytest.mark.asyncio
async def test_async_tool_timeout(monkeypatch):
    """验证异步工具超时返回标准结构。"""
    monkeypatch.setattr(settings, "tool_timeout_seconds", 0.05)

    async def slow_async_handler(args):
        await asyncio.sleep(0.2)
        return "done"

    register_tool("slow_async_test", "slow async tool", slow_async_handler, inputSchema={"type": "object"})

    req = JSONRPCRequest(
        id="req-timeout-2",
        method="tools/call",
        params={"name": "slow_async_test", "arguments": {}},
    )
    resp = await _handle_tools_call(req)

    result = resp.get("result", {})
    assert result.get("isError") is True
    assert result.get("error_code") == "TOOL_TIMEOUT"
    assert result.get("_timed_out") is True
