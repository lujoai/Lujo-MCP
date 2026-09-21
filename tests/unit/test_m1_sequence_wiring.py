"""W4-4（B15/C4）M1 ①–⑥ 序接线验收：池关闭不得先于进程终止。

对应 DESIGN_C4 §3 与 CHECKLIST W4-4：
- stdio cleanup_resources 与 HTTP lifespan 共用同一 run_m1_sequence 编排；
- ③ terminate_active_processes 先于 ⑤ 池关闭（旧序先关池 = B15 缺陷）；
- ⑤ 两个池分别 shutdown(wait=False, cancel_futures=True)，仍不统一双池；
- ① 停止接纳：closing 置位后 run_heavy_tool_blocking 拒绝新 spawn。

注入方式：monkeypatch 替换双池/执行器/terminate spy（单元确定性）；
生命周期接线以源码锁定（OS 级时序由 W4-7 覆盖）。
"""

from __future__ import annotations

import inspect

import pytest

from app.mcp.protocol.executor_lifecycle import SlotPool


def _fresh_pools(monkeypatch):
    """给 mcp_server 与 protocol_server 换上全新池（避免污染共享状态）。"""
    import app.mcp.protocol.server as protocol_server
    import app.mcp_server as stdio

    light = SlotPool("light", 2)
    heavy = SlotPool("heavy", 2)
    monkeypatch.setattr(stdio, "_light_pool", light)
    monkeypatch.setattr(stdio, "_heavy_pool", heavy)
    monkeypatch.setattr(protocol_server, "_light_pool", light)
    monkeypatch.setattr(protocol_server, "_heavy_pool", heavy)
    return light, heavy


class _FakeExecutor:
    def __init__(self, log):
        self.log = log

    def shutdown(self, wait=True, cancel_futures=False):
        self.log.append(("executor_shutdown", wait, cancel_futures))


def test_cleanup_resources_m1_order_terminate_before_pool_shutdown(monkeypatch):
    """核心时序：③ 终止在途 → ⑤ 才关池；两池分别 shutdown（不统一双池）。"""
    import app.mcp.protocol.server as protocol_server
    import app.mcp_server as stdio
    import app.mcp.protocol.heavy_process as hp

    light, heavy = _fresh_pools(monkeypatch)

    order = []
    terminate_calls = []

    def _fake_terminate():
        order.append("step3_terminate")
        terminate_calls.append(1)
        return 0

    monkeypatch.setattr(stdio, "terminate_active_processes", _fake_terminate)

    fake_light_exec = _FakeExecutor(order)
    fake_stdio_exec = _FakeExecutor(order)
    fake_heavy_dispatch_exec = _FakeExecutor(order)
    monkeypatch.setattr(protocol_server, "_LIGHT_TOOL_EXECUTOR", fake_light_exec)
    monkeypatch.setattr(stdio, "_TOOL_EXECUTOR", fake_stdio_exec)
    # W12：heavy 派发池也进 M1 ⑤（此前它不存在；不 fake 掉会真关生产池）
    monkeypatch.setattr(
        protocol_server, "_HEAVY_DISPATCH_EXECUTOR", fake_heavy_dispatch_exec
    )

    # B23 幂等键重置（隔离本用例）
    monkeypatch.setattr(stdio, "_cleaned_executor", None)
    monkeypatch.setattr(stdio, "_cleaned_pool_generations", None)

    stdio.cleanup_resources()

    # ③ 先于 ⑤（B15 现状缺陷：先关池不杀进程）
    assert terminate_calls == [1]
    assert order[0] == "step3_terminate"
    shutdown_positions = [i for i, e in enumerate(order) if e[0] == "executor_shutdown"]
    assert all(pos > order.index("step3_terminate") for pos in shutdown_positions)
    # 三个池分别 shutdown（不合并任何两池），且 wait=False + cancel_futures
    assert len(shutdown_positions) == 3
    for pos in shutdown_positions:
        _, wait, cancel = order[pos]
        assert wait is False and cancel is True
    # ① 停止接纳：两池 closing 置位；② 等待者取消已调用
    assert light.is_closing and heavy.is_closing


def test_cleanup_resources_b23_idempotency_preserved(monkeypatch):
    """B23：同代重复 cleanup 幂等（第二次不重跑 M1 序）。"""
    import app.mcp.protocol.server as protocol_server
    import app.mcp_server as stdio
    import app.mcp.protocol.heavy_process as hp

    _fresh_pools(monkeypatch)
    monkeypatch.setattr(stdio, "_cleaned_executor", None)
    monkeypatch.setattr(stdio, "_cleaned_pool_generations", None)

    calls = {"terminate": 0, "shutdown": 0}

    def _fake_terminate():
        calls["terminate"] += 1
        return 0

    # cleanup_resources 经模块顶层导入持有 terminate_active_processes 绑定，
    # spy 打在 mcp_server 的绑定上（与生产调用路径一致）
    monkeypatch.setattr(stdio, "terminate_active_processes", _fake_terminate)

    class _FakeExecutor:
        def shutdown(self, wait=True, cancel_futures=False):
            calls["shutdown"] += 1

    monkeypatch.setattr(protocol_server, "_LIGHT_TOOL_EXECUTOR", _FakeExecutor())
    monkeypatch.setattr(stdio, "_TOOL_EXECUTOR", _FakeExecutor())
    monkeypatch.setattr(protocol_server, "_HEAVY_DISPATCH_EXECUTOR", _FakeExecutor())

    stdio.cleanup_resources()
    stdio.cleanup_resources()  # 同代幂等
    assert calls["terminate"] == 1
    assert calls["shutdown"] == 3  # 三个池各一次（W12 起含 heavy 派发池）


def test_run_heavy_tool_blocking_rejects_when_closing(monkeypatch):
    """① 停止接纳：closing 置位后 run_heavy_tool_blocking 拒绝（不再 spawn）。

    **W12 有意变更既有预期**：原断言写的是 ``pytest.raises(asyncio.TimeoutError)``
    ——那正是缺陷本身（P1-HEAVY-2 / P3-HEAVY-5：closing 被当成超时，两个传输
    据此错标 TOOL_TIMEOUT 并虚构「>60s」文案、指标误记 timeout）。现按
    ``executor_lifecycle`` 早已写明的「服务关闭中 / TOOL_BUSY」语义，拒绝由
    ``HeavyServiceClosing``（RuntimeError 子类，**不是** TimeoutError）承载。
    """
    import asyncio

    import app.mcp.protocol.heavy_process as hp
    import app.mcp.protocol.server as protocol_server

    light, heavy = _fresh_pools(monkeypatch)
    light.begin_close()
    heavy.begin_close()

    spawned = []
    monkeypatch.setattr(
        hp.termination_backend, "spawn_with_backend",
        lambda *a, **kw: spawned.append(1) or (_ for _ in ()).throw(AssertionError("不得 spawn")),
        raising=False,
    )

    async def _scenario():
        return await asyncio.wait_for(
            hp.run_heavy_tool_blocking("app.mcp.protocol._heavy_selftest", "quick_ok", {}, timeout=5),
            timeout=10,
        )

    with pytest.raises(hp.HeavyServiceClosing) as excinfo:
        asyncio.run(_scenario())
    assert not isinstance(excinfo.value, asyncio.TimeoutError), (
        "关闭中拒绝不得再由 TimeoutError 承载，否则又会被映射成 TOOL_TIMEOUT"
    )
    assert spawned == []  # closing 后未 spawn


def test_lifespan_wires_m1_sequence():
    """HTTP lifespan 关闭段接入同一 run_m1_sequence（terminate 先于关池）。"""
    import app.main as main_mod

    source = inspect.getsource(main_mod)
    assert "run_m1_sequence" in source
    assert "terminate_active_processes" in source
    assert "begin_close" in source
    assert "cancel_waiters" in source
    assert "shutdown(wait=False, cancel_futures=True)" in source


def test_stdio_cleanup_uses_shared_sequence():
    """stdio cleanup 与 HTTP lifespan 共用同一编排实现（run_m1_sequence）。"""
    import app.mcp_server as stdio

    source = inspect.getsource(stdio.cleanup_resources)
    assert "run_m1_sequence" in source
    assert "terminate_active_processes" in source
