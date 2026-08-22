"""工具超时与背压处理测试：验证同步/异步工具超时响应结构、背压并发控制、快速失败及 sync_future.cancel() 取消调度行为"""
import asyncio
import time
from unittest.mock import MagicMock, patch
import pytest

from app.config import settings
import app.mcp.protocol.server as server_module
from app.mcp.protocol.jsonrpc import JSONRPCRequest
from app.mcp.protocol.server import _handle_tools_call, _tool_registry, register_tool


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


@pytest.mark.asyncio
async def test_sync_tool_busy_queue_fast_fail(monkeypatch):
    """测试并发槽位占满时，新同步调用在 tool_busy_queue_timeout 内快速拒绝并返回 TOOL_BUSY。"""
    monkeypatch.setattr(server_module, "_tool_slots", asyncio.Semaphore(2))
    monkeypatch.setattr(settings, "tool_busy_queue_timeout", 0.1)
    monkeypatch.setattr(settings, "tool_timeout_seconds", 5.0)

    def _slow_sync(args):
        time.sleep(0.4)
        return {"sync": "ok"}

    register_tool("test_slow_occupier", "Slow sync tool occupying slot", _slow_sync)

    t1 = asyncio.create_task(_handle_tools_call(
        JSONRPCRequest(id=101, method="tools/call", params={"name": "test_slow_occupier", "arguments": {}})
    ))
    t2 = asyncio.create_task(_handle_tools_call(
        JSONRPCRequest(id=102, method="tools/call", params={"name": "test_slow_occupier", "arguments": {}})
    ))
    await asyncio.sleep(0.03)

    t0 = time.monotonic()
    resp3 = await _handle_tools_call(
        JSONRPCRequest(id=103, method="tools/call", params={"name": "test_slow_occupier", "arguments": {}})
    )
    elapsed = time.monotonic() - t0

    assert elapsed < 0.35
    assert resp3["result"]["isError"] is True
    assert resp3["result"]["error_code"] == "TOOL_BUSY"
    assert resp3["result"]["_busy"] is True
    assert "工具执行队列已满" in resp3["result"]["content"][0]["text"]

    await asyncio.gather(t1, t2)


@pytest.mark.asyncio
async def test_sync_tool_slots_released_after_completion(monkeypatch):
    """测试同步工具执行完毕后槽位正常释放，后续调用可正常获取槽位执行。"""
    monkeypatch.setattr(server_module, "_tool_slots", asyncio.Semaphore(1))
    monkeypatch.setattr(settings, "tool_busy_queue_timeout", 0.1)

    def _fast_sync(args):
        return {"result": "success"}

    register_tool("test_fast_sync", "Fast sync tool", _fast_sync)

    resp1 = await _handle_tools_call(
        JSONRPCRequest(id=201, method="tools/call", params={"name": "test_fast_sync", "arguments": {}})
    )
    assert resp1["result"]["isError"] is False

    resp2 = await _handle_tools_call(
        JSONRPCRequest(id=202, method="tools/call", params={"name": "test_fast_sync", "arguments": {}})
    )
    assert resp2["result"]["isError"] is False


@pytest.mark.asyncio
async def test_async_tool_not_blocked_by_sync_tool_slots(monkeypatch):
    """测试同步槽位全部被占满时，异步工具仍可并发执行不受影响。"""
    monkeypatch.setattr(server_module, "_tool_slots", asyncio.Semaphore(1))
    monkeypatch.setattr(settings, "tool_busy_queue_timeout", 0.1)

    def _slow_sync(args):
        time.sleep(0.3)
        return {"sync": "done"}

    async def _fast_async(args):
        return {"async": "done"}

    register_tool("sync_occupier", "Sync occupier", _slow_sync)
    register_tool("fast_async_tool", "Fast async", _fast_async)

    sync_task = asyncio.create_task(_handle_tools_call(
        JSONRPCRequest(id=301, method="tools/call", params={"name": "sync_occupier", "arguments": {}})
    ))
    await asyncio.sleep(0.02)

    resp_async = await _handle_tools_call(
        JSONRPCRequest(id=302, method="tools/call", params={"name": "fast_async_tool", "arguments": {}})
    )
    assert resp_async["result"]["isError"] is False

    await sync_task


@pytest.mark.asyncio
async def test_busy_queue_timeout_zero_fast_fail(monkeypatch):
    """测试 tool_busy_queue_timeout=0 时，槽位被占满后发起新调用立即可靠返回 TOOL_BUSY，不阻塞。"""
    monkeypatch.setattr(server_module, "_tool_slots", asyncio.Semaphore(1))
    monkeypatch.setattr(settings, "tool_busy_queue_timeout", 0)
    monkeypatch.setattr(settings, "tool_timeout_seconds", 10.0)

    def _slow_sync(args):
        time.sleep(0.3)
        return {"sync": "done"}

    register_tool("sync_occupier_zero", "Sync occupier for zero timeout", _slow_sync)

    # 启动同步任务占满唯一槽位
    sync_task = asyncio.create_task(_handle_tools_call(
        JSONRPCRequest(id=401, method="tools/call", params={"name": "sync_occupier_zero", "arguments": {}})
    ))
    await asyncio.sleep(0.02)

    # 发起第 2 个请求：由于 busy_timeout=0 且槽位已被占满，应立即 Fast-Fail 返回 TOOL_BUSY
    t0 = time.monotonic()
    resp = await _handle_tools_call(
        JSONRPCRequest(id=402, method="tools/call", params={"name": "sync_occupier_zero", "arguments": {}})
    )
    elapsed = time.monotonic() - t0

    assert elapsed < 0.2
    assert resp["result"]["isError"] is True
    assert resp["result"]["error_code"] == "TOOL_BUSY"
    assert resp["result"]["_busy"] is True
    assert "工具执行队列已满" in resp["result"]["content"][0]["text"]

    await sync_task
