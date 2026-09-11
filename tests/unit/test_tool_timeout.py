"""工具超时与背压处理测试：验证同步/异步工具超时响应结构、背压并发控制、快速失败及 sync_future.cancel() 取消调度行为"""
import asyncio
import json
import threading
import time
from unittest.mock import MagicMock, patch
import pytest

from app.config import settings
import app.mcp.protocol.server as server_module
from app.mcp.protocol.executor_lifecycle import SlotPool
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
    """同步工具超时时，**真实** concurrent.futures.Future 会被调用 cancel()，且返回结构正确。

    **改断言三件套（DESIGN C1 §3.1）**：
    - 原断言：spy ``loop.run_in_executor`` 返回的 future，断言 ``mock_future.cancel.called``。
      它锁住的是「轻量同步走 run_in_executor」这一**实现路径**，而非行为。
    - 新断言：spy ``executor.submit`` 返回的**真实** future，断言超时时 ``cancel()``
      被调用；响应结构（isError / TOOL_TIMEOUT / _timed_out / 文本）逐项不变。
    - 为什么：新设计要求结算回调挂在真实 future 上（``run_in_executor`` 只返回 asyncio
      包装 future，拿不到真实对象），故捕获点必须前移到 ``executor.submit``。
      另：``cancel()`` 对运行中线程无效，**不再作为记账事件**——槽位由真实任务终结归还。
    """
    monkeypatch.setattr(settings, "tool_timeout_seconds", 0.05)

    def slow_sync_handler(args):
        time.sleep(0.2)
        return "done"

    register_tool("slow_sync_test", "slow sync tool", slow_sync_handler, inputSchema={"type": "object"})

    captured: list = []
    real_executor = server_module._get_light_tool_executor()
    real_submit = real_executor.submit

    def spy_submit(fn, *args, **kwargs):
        fut = real_submit(fn, *args, **kwargs)
        spy = MagicMock(side_effect=fut.cancel)
        fut.cancel = spy
        captured.append((fut, spy))
        return fut

    monkeypatch.setattr(real_executor, "submit", spy_submit)

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

    assert captured, "轻量同步路径必须经 executor.submit 提交真实任务"
    _fut, spy_cancel = captured[0]
    assert spy_cancel.called, "超时必须对真实 future 调用 cancel()（善意停止等待）"


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
    monkeypatch.setattr(server_module, "_light_pool", SlotPool("light", 2))
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
    monkeypatch.setattr(server_module, "_light_pool", SlotPool("light", 1))
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
    """FIX: v0.6.6 async 工具绕过双池 —— async 轻量工具不再绕过 light 池门控。

    旧行为（缺陷）：async handler 直接 await 执行，完全绕过 light/heavy 双池
    槽位，无并发上限并与同步工具互相影响。
    新行为：async 轻量工具与同步轻量工具共享 light 池槽位，池满时同样
    按 TOOL_BUSY fast-fail。
    """
    monkeypatch.setattr(server_module, "_light_pool", SlotPool("light", 1))
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
    """FIX: v0.6.6 async 工具绕过双池 —— 重型 async 工具走 heavy 池。

    light 池被同步工具占满时，heavy 池的 async 工具不受影响（双池隔离）。
    """
    monkeypatch.setattr(server_module, "_light_pool", SlotPool("light", 1))
    monkeypatch.setattr(server_module, "_heavy_pool", SlotPool("heavy", 2))
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
    """FIX: v0.6.6 —— async 工具执行完毕后释放槽位，连续调用不会耗尽池。"""
    monkeypatch.setattr(server_module, "_light_pool", SlotPool("light", 1))
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
    """FIX: v0.6.6 超时背压 —— busy_timeout=0 且有空位时必须成功获取。

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
    """FIX: v0.6.6 超时背压竞态 —— 完成与超时同拍时槽位必须归还。

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
async def test_slot_acquire_cancelled_while_waiting_releases_slot(monkeypatch):
    """FIX: R7-T1 —— 等槽位期间外层协程被取消：已取得的槽位必须归还。

    模拟 HTTP 断连时序：内层 acquire 已成功取得槽位，外层 wait_for 因
    调用方取消传播 CancelledError。旧实现只捕获 TimeoutError，取消路径
    不归还 → 槽位泄漏，重复发生使池容量永久缩减至恒 TOOL_BUSY。
    """
    real_wait_for = asyncio.wait_for

    class _RacingSemaphore:
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

    async def _cancelled_wait_for(fut, timeout=None, **kwargs):
        # 内层 acquire 实际完成（槽位已取得），随后外层取消传播 CancelledError
        try:
            await real_wait_for(fut, timeout=10)
        except asyncio.CancelledError:
            raise
        raise asyncio.CancelledError()

    sem = _RacingSemaphore()
    monkeypatch.setattr(asyncio, "wait_for", _cancelled_wait_for)
    with pytest.raises(asyncio.CancelledError):
        await server_module._acquire_slot_or_fastfail(sem, 0.01)

    assert sem.release_count == 1      # 已取得的槽位被归还（防泄漏）
    assert sem.acquired is False


@pytest.mark.asyncio
async def test_busy_queue_timeout_zero_fast_fail(monkeypatch, caplog):
    """测试 tool_busy_queue_timeout=0 时，槽位被占满后发起新调用立即可靠返回 TOOL_BUSY，不阻塞。"""
    monkeypatch.setattr(server_module, "_light_pool", SlotPool("light", 1))
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
    monkeypatch.setattr(server_module, "_light_pool", SlotPool("light", 1))
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
async def test_sync_busy_log_prints_actual_wait_not_config(monkeypatch, caplog):
    """FIX(v0.7.1-b1-4) 回归：同步池拒绝日志打印实际等待时长。

    此前误打配置超时 busy_timeout：monkeypatch 槽位获取立即失败
    （实际等待 ≈ 0s），busy_timeout=5.0 时旧实现日志「等待 5.000s 超时」
    与事实不符，误导排障。
    """

    async def _fail_immediately(slots, busy_timeout, pool=None):
        return False

    monkeypatch.setattr(server_module, "_acquire_slot_or_fastfail", _fail_immediately)
    monkeypatch.setattr(server_module, "_light_pool", SlotPool("light", 1))
    monkeypatch.setattr(settings, "tool_busy_queue_timeout", 5.0)
    monkeypatch.setattr(settings, "tool_timeout_seconds", 10.0)

    def _handler(args):
        return {"sync": "done"}

    register_tool("sync_busy_log_actual", "Sync tool for actual wait log", _handler)

    import logging
    with caplog.at_level(logging.WARNING, logger="lujo-mcp.protocol"):
        resp = await _handle_tools_call(
            JSONRPCRequest(id=601, method="tools/call", params={"name": "sync_busy_log_actual", "arguments": {}})
        )

    assert resp["result"]["error_code"] == "TOOL_BUSY"

    wait_logs = [
        r.getMessage() for r in caplog.records
        if "等待" in r.message and "超时" in r.message
    ]
    assert wait_logs, "必须打出「等待 …s 超时」拒绝日志"
    assert "5.000s" not in wait_logs[0], (
        f"日志不得打印配置超时 5.000s（应为实际等待 ≈0s）：{wait_logs[0]}"
    )


@pytest.mark.asyncio
async def test_async_tool_records_wait_metric(monkeypatch):
    """FIX(v0.7.1-b1-5) 回归：async 工具取得槽位后必须补记 record_mcp_tool_wait。"""

    async def _fast_async(args):
        return {"async": "ok"}

    register_tool("async_wait_metric", "Async tool for wait metric", _fast_async, inputSchema={"type": "object"})

    recorded = []
    monkeypatch.setattr(
        server_module, "record_mcp_tool_wait",
        lambda name, pool, sec: recorded.append((name, pool, sec)),
    )

    resp = await _handle_tools_call(
        JSONRPCRequest(id=602, method="tools/call", params={"name": "async_wait_metric", "arguments": {}})
    )

    assert resp["result"].get("isError") is not True
    assert recorded, "async 工具成功取得槽位后必须记录 wait 指标（与同步分支口径一致）"
    name, pool, sec = recorded[0]
    assert name == "async_wait_metric"
    assert pool in ("light", "heavy")
    assert sec >= 0


@pytest.mark.asyncio
async def test_heavy_tool_saturation_does_not_block_light_tools(monkeypatch):
    """测试重型工具池打满时，轻量级工具依然享有独立槽位并立即执行（不被饿死）。"""
    monkeypatch.setattr(server_module, "_heavy_pool", SlotPool("heavy", 1))
    monkeypatch.setattr(server_module, "_light_pool", SlotPool("light", 5))
    monkeypatch.setattr(settings, "tool_busy_queue_timeout", 0.05)
    monkeypatch.setattr(settings, "tool_timeout_seconds", 5.0)

    # FIX: C2 —— 同步重活改走子进程（run_heavy_tool_blocking）。本测试聚焦
    # 槽位背压隔离，故打桩子进程执行器：占用 heavy 槽位 0.3s 模拟慢重活。
    def _slow_heavy_subprocess(module, name, arguments, timeout):
        time.sleep(0.3)
        return {"heavy": "done"}

    monkeypatch.setattr(server_module, "run_heavy_tool_blocking", _slow_heavy_subprocess)

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
    monkeypatch.setattr(server_module, "_light_pool", SlotPool("light", 1))
    monkeypatch.setattr(server_module, "_heavy_pool", SlotPool("heavy", 2))
    monkeypatch.setattr(settings, "tool_busy_queue_timeout", 0.05)
    monkeypatch.setattr(settings, "tool_timeout_seconds", 5.0)

    # FIX: C2 —— 同步重活改走子进程（run_heavy_tool_blocking）。本测试聚焦
    # 槽位隔离，打桩子进程执行器为快速返回。
    def _fast_heavy_subprocess(module, name, arguments, timeout):
        return {"heavy": "fast"}

    monkeypatch.setattr(server_module, "run_heavy_tool_blocking", _fast_heavy_subprocess)

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


# ---------------------------------------------------------------------------
# W1-9 · B07 专项验收：超时响应已返回、真实线程仍在运行期间，槽位仍被占用
# （DESIGN C1 §3.1；观察时点纪律：响应已决定 / 真实执行已结束 / 许可已归还
#   三者分开——len(registry)==0 不等于 semaphore 已恢复，容量恢复须等 RETURNED）
# ---------------------------------------------------------------------------


class _CountingSemaphore(asyncio.Semaphore):
    """用例 3 的计数 spy：统计 acquire 成功 / release 次数（继承原语义）。"""

    def __init__(self, value: int):
        super().__init__(value)
        self.acquire_count = 0
        self.release_count = 0

    async def acquire(self):
        await super().acquire()
        self.acquire_count += 1

    def release(self):
        self.release_count += 1
        super().release()


def _make_blocking_handler():
    """started / allow_finish / finished 三个**独立**同步点（用例 4 纪律）。

    只检查 started 仍为 True 不能证明线程还在执行；handler 只有在
    allow_finish 放行后才可能置位 finished，故「finished 未成立」即可证
    真实线程尚未结束。
    """
    started = threading.Event()
    allow_finish = threading.Event()
    finished = threading.Event()

    def handler(_arguments):
        started.set()
        allow_finish.wait(timeout=15)
        finished.set()
        return {"done": True}

    return handler, started, allow_finish, finished


async def _wait_until(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    """轮询等待 predicate 成立（W1-9 纪律：上限 10s，禁止无限期挂起）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def _call_tool(request_id: int, name: str) -> dict:
    return await _handle_tools_call(
        JSONRPCRequest(id=request_id, method="tools/call", params={"name": name, "arguments": {}})
    )


@pytest.mark.asyncio
async def test_b07_six_timeout_rounds_slot_occupied_until_reaped(monkeypatch):
    """用例 1 + 2：1 worker 连续 6 轮 timeout=0.5——每轮先等条目 REAPED
    （轮询上限 10s）再断言活动表清空；占用窗口内立即重试必须 TOOL_BUSY，
    REAPED 后必须成功。

    红灯含义（B07 旧实现已由 W1-3…W1-6 修复）：旧实现 finally 无条件
    release 会在超时响应返回的瞬间归还槽位 → 重试不会 TOOL_BUSY。
    """
    pool = SlotPool("light", 1)
    monkeypatch.setattr(server_module, "_light_pool", pool)
    monkeypatch.setattr(settings, "tool_timeout_seconds", 0.5)
    monkeypatch.setattr(settings, "tool_busy_queue_timeout", 0.1)

    next_id = 900

    for _round in range(6):
        handler, started, allow_finish, finished = _make_blocking_handler()
        register_tool("b07_round_tool", "B07 acceptance round tool", handler)

        # 超时响应返回；真实线程仍在运行
        next_id += 1
        resp = await _call_tool(next_id, "b07_round_tool")
        assert resp["result"]["isError"] is True
        assert resp["result"]["error_code"] == "TOOL_TIMEOUT"
        assert await _wait_until(started.is_set, timeout=5), "真实线程必须已启动"
        assert not finished.is_set(), "allow_finish 未放行，真实线程必须尚未结束"

        # 槽位仍被占用（活动表 1 条、P==1、容量 0）
        assert pool.snapshot()["active_tokens"] == 1
        assert pool.counters().P == 1
        assert pool.semaphore._value == 0

        # 占用窗口内立即重试 → TOOL_BUSY（B07：超时不得提前归还槽位）
        next_id += 1
        resp_busy = await _call_tool(next_id, "b07_round_tool")
        assert resp_busy["result"]["isError"] is True
        assert resp_busy["result"]["error_code"] == "TOOL_BUSY"

        # 放行真实线程 → 等 REAPED（活动表清空）→ 容量恢复（两个观察点分开）
        allow_finish.set()
        assert await _wait_until(finished.is_set, timeout=10)
        assert await _wait_until(
            lambda: pool.snapshot()["active_tokens"] == 0, timeout=10
        ), "真实任务终结后活动表必须清空（REAPED）"
        assert await _wait_until(
            lambda: pool.semaphore._value == 1, timeout=10
        ), "容量恢复必须等到 RETURNED 之后"

        # REAPED 后调用必须成功
        next_id += 1
        resp_ok = await _call_tool(next_id, "b07_round_tool")
        assert resp_ok["result"]["isError"] is False


@pytest.mark.asyncio
async def test_b07_slot_ledger_acquire_matches_release_after_real_end(monkeypatch):
    """用例 3：计数器 spy ``slots.acquire/release``——整轮后 acquire == release
    且 ``_value`` 回到初始；超时响应已返回、真实线程仍在运行时
    中途在途并发 == 1（acquire 已计数、release 未发生）。"""
    pool = SlotPool("light", 1)
    counting = _CountingSemaphore(1)
    monkeypatch.setattr(server_module, "_light_pool", pool)
    monkeypatch.setattr(pool, "_semaphore", counting)
    monkeypatch.setattr(settings, "tool_timeout_seconds", 0.5)
    monkeypatch.setattr(settings, "tool_busy_queue_timeout", 0.1)

    handler, _started, allow_finish, finished = _make_blocking_handler()
    register_tool("b07_ledger_tool", "B07 ledger tool", handler)

    resp = await _call_tool(950, "b07_ledger_tool")
    assert resp["result"]["error_code"] == "TOOL_TIMEOUT"
    assert await _wait_until(finished.is_set, timeout=0.1) is False

    # B07 本体：响应已返回、真实线程仍运行 → acquire 已发生、release 未发生
    assert counting.acquire_count == 1
    assert counting.release_count == 0
    assert counting._value == 0
    assert counting.acquire_count - counting.release_count <= 1

    # 占用窗口内重试不消耗 acquire（未取得许可即被取消）
    resp_busy = await _call_tool(951, "b07_ledger_tool")
    assert resp_busy["result"]["error_code"] == "TOOL_BUSY"
    assert counting.acquire_count == 1, "TOOL_BUSY 拒绝不得计入 acquire"
    assert counting.release_count == 0

    # 真实任务终结后：acquire == release，_value 回到初始
    allow_finish.set()
    assert await _wait_until(
        lambda: counting.acquire_count == counting.release_count, timeout=10
    )
    assert await _wait_until(lambda: counting._value == 1, timeout=10)
    assert counting.acquire_count == 1 and counting.release_count == 1


@pytest.mark.asyncio
async def test_b07_thread_still_running_proven_by_three_sync_points(monkeypatch):
    """用例 4：轻量超时反例必须用 started / allow_finish / finished 三个
    独立同步点——响应超时后保持 allow_finish 未放行，确认 finished 未成立，
    再断言第二次调用 TOOL_BUSY。只检查 started 为 True 不构成执行中证据。"""
    pool = SlotPool("light", 1)
    monkeypatch.setattr(server_module, "_light_pool", pool)
    monkeypatch.setattr(settings, "tool_timeout_seconds", 0.5)
    monkeypatch.setattr(settings, "tool_busy_queue_timeout", 0.1)

    handler, started, allow_finish, finished = _make_blocking_handler()
    register_tool("b07_syncpoint_tool", "B07 sync-point tool", handler)

    resp = await _call_tool(960, "b07_syncpoint_tool")
    assert resp["result"]["error_code"] == "TOOL_TIMEOUT"

    # 同步点 1：线程已启动
    assert await _wait_until(started.is_set, timeout=5)
    # 同步点 2：allow_finish 未放行 + finished 未成立 → handler 只能在
    # allow_finish 之后退出，故真实线程必然仍在执行（结构性证明）
    assert not allow_finish.is_set()
    assert not finished.is_set()

    # 第二次调用必须 TOOL_BUSY
    resp_busy = await _call_tool(961, "b07_syncpoint_tool")
    assert resp_busy["result"]["error_code"] == "TOOL_BUSY"
    assert pool.semaphore._value == 0

    # 同步点 3：放行后 finished 才成立，随后槽位恢复
    allow_finish.set()
    assert await _wait_until(finished.is_set, timeout=10)
    assert await _wait_until(lambda: pool.semaphore._value == 1, timeout=10)
