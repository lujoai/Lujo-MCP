"""工具超时与背压处理测试：验证同步/异步工具超时响应结构、背压并发控制、快速失败及 sync_future.cancel() 取消调度行为"""
import asyncio
import json
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
async def test_async_tool_gated_by_light_pool(monkeypatch):
    """FIX: v0.6.5 async 工具绕过双池 —— async 轻量工具不再绕过 light 池门控。

    旧行为（缺陷）：async handler 直接 await 执行，完全绕过 light/heavy 双池
    槽位，无并发上限并与同步工具互相影响。
    新行为：async 轻量工具与同步轻量工具共享 light 池槽位，池满时同样
    按 TOOL_BUSY fast-fail。
    """
    monkeypatch.setattr(server_module, "_tool_slots", asyncio.Semaphore(1))
    monkeypatch.setattr(settings, "tool_busy_queue_timeout", 0.05)

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
    assert resp_async["result"]["isError"] is True
    assert resp_async["result"]["error_code"] == "TOOL_BUSY"
    assert resp_async["result"]["_busy"] is True

    await sync_task


@pytest.mark.asyncio
async def test_async_heavy_tool_uses_heavy_pool(monkeypatch):
    """FIX: v0.6.5 async 工具绕过双池 —— 重型 async 工具走 heavy 池。

    light 池被同步工具占满时，heavy 池的 async 工具不受影响（双池隔离）。
    """
    monkeypatch.setattr(server_module, "_tool_slots", asyncio.Semaphore(1))
    monkeypatch.setattr(server_module, "_heavy_tool_slots", asyncio.Semaphore(2))
    monkeypatch.setattr(settings, "tool_busy_queue_timeout", 0.05)

    def _slow_light_sync(args):
        time.sleep(0.3)
        return {"light": "slow"}

    async def _fast_heavy_async(args):
        return {"heavy": "async"}

    register_tool("light_sync_occ", "Light sync occupier", _slow_light_sync, heavy=False)
    register_tool("heavy_async_tool", "Heavy async tool", _fast_heavy_async, heavy=True)

    occ = asyncio.create_task(_handle_tools_call(
        JSONRPCRequest(id=310, method="tools/call", params={"name": "light_sync_occ", "arguments": {}})
    ))
    await asyncio.sleep(0.02)

    resp = await _handle_tools_call(
        JSONRPCRequest(id=311, method="tools/call", params={"name": "heavy_async_tool", "arguments": {}})
    )
    assert resp["result"]["isError"] is False
    heavy_content = json.loads(resp["result"]["content"][0]["text"])
    assert heavy_content["heavy"] == "async"

    await occ


@pytest.mark.asyncio
async def test_async_tool_releases_slot_after_completion(monkeypatch):
    """FIX: v0.6.5 —— async 工具执行完毕后释放槽位，连续调用不会耗尽池。"""
    monkeypatch.setattr(server_module, "_tool_slots", asyncio.Semaphore(1))
    monkeypatch.setattr(settings, "tool_busy_queue_timeout", 0.1)

    async def _fast_async(args):
        return {"async": "ok"}

    register_tool("fast_async_seq", "Fast async sequential", _fast_async)

    resp1 = await _handle_tools_call(
        JSONRPCRequest(id=320, method="tools/call", params={"name": "fast_async_seq", "arguments": {}})
    )
    resp2 = await _handle_tools_call(
        JSONRPCRequest(id=321, method="tools/call", params={"name": "fast_async_seq", "arguments": {}})
    )
    assert resp1["result"]["isError"] is False
    assert resp2["result"]["isError"] is False


@pytest.mark.asyncio
async def test_slot_acquire_timeout_zero_with_free_slot_succeeds():
    """FIX: v0.6.5 超时背压 —— busy_timeout=0 且有空位时必须成功获取。

    防止 ensure_future 包装后 timeout=0 定时器把"有空位的快路径获取"
    误杀成 TOOL_BUSY（回归保护：快路径不挂起、不受定时器影响）。
    """
    sem = asyncio.Semaphore(1)
    got = await server_module._acquire_slot_or_fastfail(sem, 0)
    assert got is True
    assert sem.locked() is True  # 槽位被本调用方持有


@pytest.mark.asyncio
async def test_slot_acquire_timeout_zero_without_slot_rejects():
    """busy_timeout=0 且无空位 → 立即拒绝（Fast-Fail 文档语义）。"""
    sem = asyncio.Semaphore(0)
    got = await server_module._acquire_slot_or_fastfail(sem, 0)
    assert got is False
    assert sem.locked() is True  # 未误 release（泄漏会让 locked 变 False）


@pytest.mark.asyncio
async def test_slot_acquire_wait_timeout_no_leak():
    """等待超时且未取得槽位 → 不归还（无重复释放），槽位计数不变。"""
    sem = asyncio.Semaphore(0)
    got = await server_module._acquire_slot_or_fastfail(sem, 0.02)
    assert got is False
    assert sem.locked() is True  # 若误 release，_value 会变 1 → locked() False


@pytest.mark.asyncio
async def test_slot_acquire_same_tick_race_releases_slot(monkeypatch):
    """FIX: v0.6.5 超时背压竞态 —— 完成与超时同拍时槽位必须归还。

    模拟 CPython wait_for 竞态窗口：acquire 任务已成功取得槽位（返回 True），
    但 wait_for 仍向调用方抛 TimeoutError。旧实现按 fast-fail 返回且永不
    release → 槽位泄漏，重复 N 次后池永久占满（全部工具恒 TOOL_BUSY）。
    """
    real_wait_for = asyncio.wait_for

    class _RacingSemaphore:
        """acquire 完成较慢（确保先挂起进入等待路径），随后成功取得槽位。"""

        def __init__(self):
            self.acquired = False
            self.release_count = 0

        def locked(self):
            return not self.acquired

        async def acquire(self):
            await asyncio.sleep(0.05)
            self.acquired = True
            return True

        def release(self):
            self.release_count += 1
            self.acquired = False

    async def _racing_wait_for(fut, timeout=None, **kwargs):
        # 复现竞态：任务实际完成（槽位已取得），调用方却看到 TimeoutError
        try:
            await real_wait_for(fut, timeout=10)
        except asyncio.CancelledError:
            raise
        raise asyncio.TimeoutError()

    sem = _RacingSemaphore()
    monkeypatch.setattr(asyncio, "wait_for", _racing_wait_for)
    try:
        got = await server_module._acquire_slot_or_fastfail(sem, 0.01)
    finally:
        monkeypatch.undo()

    assert got is False                # 调用方按 fast-fail 处理
    assert sem.release_count == 1      # 但已取得的槽位被归还（防泄漏）
    assert sem.acquired is False


@pytest.mark.asyncio
async def test_busy_queue_timeout_zero_fast_fail(monkeypatch, caplog):
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
    with caplog.at_level("WARNING", logger="lujo-mcp.protocol"):
        resp = await _handle_tools_call(
            JSONRPCRequest(id=402, method="tools/call", params={"name": "sync_occupier_zero", "arguments": {}})
        )
    elapsed = time.monotonic() - t0

    assert elapsed < 0.2
    assert resp["result"]["isError"] is True
    assert resp["result"]["error_code"] == "TOOL_BUSY"
    assert resp["result"]["_busy"] is True
    assert "工具执行队列已满" in resp["result"]["content"][0]["text"]

    # 日志必须明确「不等待/立即拒绝」，且不得包含旧的「等待 >0s 超时」表达
    assert any("不等待" in record.message or "立即拒绝" in record.message
               for record in caplog.records)
    assert not any("等待 >0s 超时" in record.message for record in caplog.records)

    await sync_task


@pytest.mark.asyncio
async def test_busy_queue_timeout_positive_logs_wait_duration(monkeypatch, caplog):
    """测试 tool_busy_queue_timeout>0 时，背压拒绝日志必须包含实际等待时长语义，而非「立即拒绝」。"""
    monkeypatch.setattr(server_module, "_tool_slots", asyncio.Semaphore(1))
    monkeypatch.setattr(settings, "tool_busy_queue_timeout", 0.1)
    monkeypatch.setattr(settings, "tool_timeout_seconds", 10.0)

    def _slow_sync(args):
        time.sleep(0.5)
        return {"sync": "done"}

    register_tool("sync_occupier_pos", "Sync occupier for positive timeout", _slow_sync)

    sync_task = asyncio.create_task(_handle_tools_call(
        JSONRPCRequest(id=501, method="tools/call", params={"name": "sync_occupier_pos", "arguments": {}})
    ))
    await asyncio.sleep(0.02)

    with caplog.at_level("WARNING", logger="lujo-mcp.protocol"):
        resp = await _handle_tools_call(
            JSONRPCRequest(id=502, method="tools/call", params={"name": "sync_occupier_pos", "arguments": {}})
        )

    # Fast-Fail 与 TOOL_BUSY 响应结构保持不变
    assert resp["result"]["isError"] is True
    assert resp["result"]["error_code"] == "TOOL_BUSY"
    assert resp["result"]["_busy"] is True

    # 日志必须体现等待时长语义（等待 + 超时），且不得出现「立即拒绝」
    assert any("等待" in record.message and "超时" in record.message
               for record in caplog.records)
    assert not any("立即拒绝" in record.message for record in caplog.records)

    await sync_task


@pytest.mark.asyncio
async def test_heavy_tool_saturation_does_not_block_light_tools(monkeypatch):
    """测试重型工具池打满时，轻量级工具依然享有独立槽位并立即执行（不被饿死）。"""
    monkeypatch.setattr(server_module, "_heavy_tool_slots", asyncio.Semaphore(1))
    monkeypatch.setattr(server_module, "_tool_slots", asyncio.Semaphore(5))
    monkeypatch.setattr(settings, "tool_busy_queue_timeout", 0.05)
    monkeypatch.setattr(settings, "tool_timeout_seconds", 5.0)

    def _slow_heavy(args):
        time.sleep(0.3)
        return {"heavy": "done"}

    def _fast_light(args):
        return {"light": "ok"}

    register_tool("auto_test_mock", "Heavy mock tool", _slow_heavy, heavy=True)
    register_tool("get_debug_context_mock", "Light mock tool", _fast_light, heavy=False)

    # 1. 启动一个 heavy 任务占满 heavy 槽位 (容量=1)
    heavy_task1 = asyncio.create_task(_handle_tools_call(
        JSONRPCRequest(id=701, method="tools/call", params={"name": "auto_test_mock", "arguments": {}})
    ))
    await asyncio.sleep(0.02)

    # 2. 第二个 heavy 任务尝试获取槽位，应当因为 heavy 池满而快速返回 TOOL_BUSY
    resp_heavy2 = await _handle_tools_call(
        JSONRPCRequest(id=702, method="tools/call", params={"name": "auto_test_mock", "arguments": {}})
    )
    assert resp_heavy2["result"]["isError"] is True
    assert resp_heavy2["result"]["error_code"] == "TOOL_BUSY"

    # 3. 此时轻量级工具调用，应当完全不受 heavy 池拥堵影响，立即成功返回
    t0 = time.monotonic()
    resp_light = await _handle_tools_call(
        JSONRPCRequest(id=703, method="tools/call", params={"name": "get_debug_context_mock", "arguments": {}})
    )
    elapsed = time.monotonic() - t0

    assert resp_light["result"]["isError"] is False
    light_content = json.loads(resp_light["result"]["content"][0]["text"])
    assert light_content["light"] == "ok"
    assert elapsed < 0.1, f"Light tool was blocked! elapsed={elapsed}"

    await heavy_task1


@pytest.mark.asyncio
async def test_light_tool_saturation_does_not_block_heavy_tools(monkeypatch):
    """测试轻量工具池打满时，重型工具池独立运作不受干扰。"""
    monkeypatch.setattr(server_module, "_tool_slots", asyncio.Semaphore(1))
    monkeypatch.setattr(server_module, "_heavy_tool_slots", asyncio.Semaphore(2))
    monkeypatch.setattr(settings, "tool_busy_queue_timeout", 0.05)
    monkeypatch.setattr(settings, "tool_timeout_seconds", 5.0)

    def _slow_light(args):
        time.sleep(0.3)
        return {"light": "slow"}

    def _fast_heavy(args):
        return {"heavy": "fast"}

    register_tool("light_occupier", "Light occupier", _slow_light, heavy=False)
    register_tool("heavy_independent", "Heavy independent", _fast_heavy, heavy=True)

    # 1. 占满 light 槽位 (容量=1)
    light_task1 = asyncio.create_task(_handle_tools_call(
        JSONRPCRequest(id=801, method="tools/call", params={"name": "light_occupier", "arguments": {}})
    ))
    await asyncio.sleep(0.02)

    # 2. 第二个 light 任务触发 TOOL_BUSY
    resp_light2 = await _handle_tools_call(
        JSONRPCRequest(id=802, method="tools/call", params={"name": "light_occupier", "arguments": {}})
    )
    assert resp_light2["result"]["isError"] is True
    assert resp_light2["result"]["error_code"] == "TOOL_BUSY"

    # 3. Heavy 工具依然可以正常执行
    resp_heavy = await _handle_tools_call(
        JSONRPCRequest(id=803, method="tools/call", params={"name": "heavy_independent", "arguments": {}})
    )
    assert resp_heavy["result"]["isError"] is False
    heavy_content = json.loads(resp_heavy["result"]["content"][0]["text"])
    assert heavy_content["heavy"] == "fast"

    await light_task1


def test_heavy_tool_identification():
    """测试重型工具名称识别（通过配置与显式元数据）。"""
    from app.mcp.protocol.server import is_heavy_tool

    # 默认配置中 auto_test 和 verify_ui 为 heavy
    assert is_heavy_tool("auto_test") is True
    assert is_heavy_tool("verify_ui") is True
    assert is_heavy_tool("get_debug_context") is False
    assert is_heavy_tool("resolve_stack") is False
