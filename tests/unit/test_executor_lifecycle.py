"""DESIGN C1 实现验收：token 状态机、许可归还与代际退役。

对应 docs/internal/DESIGN_C1_SLOT_ACCOUNTING.md §3.4 / §5.1 / §5.2 / §7.1。
分类：**B 类·新机制验收**（新构件不变量；旧实现上无此构件）。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading

import pytest

from app.mcp.protocol.executor_lifecycle import (
    ExecutorLifecycle,
    SlotPool,
    TokenState,
)


# --------------------------------------------------------------------- 基础


@pytest.mark.asyncio
async def test_acquire_then_settle_releases_slot():
    pool = SlotPool("t-basic", 2)
    sem = pool.semaphore
    await sem.acquire()
    token = pool.acquire_token()
    assert token.state is TokenState.ACTIVE

    c = pool.counters()
    assert (c.A, c.P, c.Q, c.R, c.X) == (1, 1, 0, 0, 0)
    pool.assert_conservation()

    assert pool.settle(token) is True
    # 结算只到 RETURN_PENDING，归还尚未发生
    assert token.state is TokenState.RETURN_PENDING
    c = pool.counters()
    assert (c.A, c.P, c.Q, c.R, c.X) == (1, 0, 1, 0, 0)
    pool.assert_conservation()

    # 归还回调在属主 loop 上执行
    await asyncio.sleep(0)
    assert token.state is TokenState.RETURNED
    c = pool.counters()
    assert (c.A, c.P, c.Q, c.R, c.X) == (1, 0, 0, 1, 0)
    pool.assert_conservation()
    assert sem._value == 2  # 许可已实际恢复


@pytest.mark.asyncio
async def test_settle_is_idempotent():
    pool = SlotPool("t-idem", 1)
    await pool.semaphore.acquire()
    token = pool.acquire_token()

    assert pool.settle(token) is True
    assert pool.settle(token) is False
    assert pool.settle(token) is False
    await asyncio.sleep(0)

    c = pool.counters()
    assert (c.A, c.P, c.Q, c.R, c.X) == (1, 0, 0, 1, 0)
    pool.assert_conservation()
    assert pool.semaphore._value == 1


@pytest.mark.asyncio
async def test_settle_does_not_credit_before_callback():
    """投递成功 ≠ 已归还：回调执行前 Q 必须仍为 1。"""
    pool = SlotPool("t-pending", 1)
    await pool.semaphore.acquire()
    token = pool.acquire_token()
    pool.settle(token)

    # 尚未让 loop 跑回调
    assert pool.counters().Q == 1
    assert pool.counters().R == 0
    assert token.state is TokenState.RETURN_PENDING

    await asyncio.sleep(0)
    assert pool.counters().Q == 0
    assert pool.counters().R == 1


# --------------------------------------------------------------------- 退役


@pytest.mark.asyncio
async def test_retire_refused_while_active_keeps_counters():
    pool = SlotPool("t-retire-active", 2)
    await pool.semaphore.acquire()
    await pool.semaphore.acquire()
    t1 = pool.acquire_token()
    t2 = pool.acquire_token()

    pool.settle(t1)
    await asyncio.sleep(0)  # t1 归还

    before = pool.counters().as_dict()
    assert pool.retire() is False, "仍有 ACTIVE 时禁止退役"
    after = pool.counters().as_dict()
    assert before == after, "拒绝退役时账目不得被改动（禁止强制归零）"
    assert t2.state is TokenState.ACTIVE


@pytest.mark.asyncio
async def test_retire_converts_pending_to_x():
    pool = SlotPool("t-retire", 1)
    await pool.semaphore.acquire()
    token = pool.acquire_token()
    pool.settle(token)

    # 用已关闭的 loop 模拟「投递成功但回调永不执行」的收口路径
    dead = asyncio.new_event_loop()
    dead.close()
    token.loop = dead
    with pytest.raises(RuntimeError):
        dead.call_soon_threadsafe(lambda: None)

    assert pool.retire() is True
    c = pool.counters()
    assert (c.P, c.Q, c.R, c.X) == (0, 0, 0, 1)
    assert c.retired is True
    pool.assert_conservation()
    # 退役不得要求旧 semaphore 恢复初值
    assert pool.semaphore._value == 0


@pytest.mark.asyncio
async def test_delivery_failure_keeps_pending_and_counts_gap():
    pool = SlotPool("t-deliver-fail", 1)
    await pool.semaphore.acquire()
    token = pool.acquire_token()

    dead = asyncio.new_event_loop()
    dead.close()
    token.loop = dead

    assert pool.settle(token) is True
    assert token.state is TokenState.RETURN_PENDING
    assert pool.counters().Q == 1
    assert pool.delivery_failures == 1
    pool.assert_conservation()


# ------------------------------------------------------------------ 新代创建


@pytest.mark.asyncio
async def test_new_generation_requires_conditions():
    pool = SlotPool("t-gen", 1)
    await pool.semaphore.acquire()
    token = pool.acquire_token()

    with pytest.raises(RuntimeError):
        pool.start_new_generation()  # 未 closing

    pool.begin_close()
    with pytest.raises(RuntimeError):
        pool.start_new_generation()  # 仍有 ACTIVE

    pool.settle(token)
    await asyncio.sleep(0)
    assert pool.retire() is True
    assert pool.can_rebuild() is True

    gen = pool.start_new_generation()
    assert gen == 2
    assert pool.semaphore._value == 1  # 新代容量恢复
    c = pool.counters()
    assert (c.A, c.P, c.Q, c.R, c.X) == (0, 0, 0, 0, 0)


# ------------------------------------------------------------- 活动表有界性


@pytest.mark.asyncio
async def test_active_table_stays_bounded():
    pool = SlotPool("t-bounded", 1)
    for _ in range(50):
        await pool.semaphore.acquire()
        token = pool.acquire_token()
        pool.settle(token)
        await asyncio.sleep(0)

    snap = pool.snapshot()
    assert snap["active_tokens"] == 0, "完成的 token 必须移出活动表"
    c = pool.counters()
    assert c.A == 50 and c.R == 50
    pool.assert_conservation()


# ------------------------------------------------------- 跨线程 / 真实 future


@pytest.mark.asyncio
async def test_worker_thread_settle_returns_on_owner_loop():
    pool = SlotPool("t-thread", 1)
    await pool.semaphore.acquire()
    token = pool.acquire_token()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()

    done = threading.Event()
    settle_thread: list[int] = []

    def _worker() -> None:
        # 模拟池工作线程回调：只做结算，不碰 asyncio.Semaphore
        settle_thread.append(threading.get_ident())
        pool.settle(token)
        done.set()

    th = threading.Thread(target=_worker)
    th.start()
    await loop.run_in_executor(None, done.wait, 5.0)
    th.join(timeout=5.0)

    # 结算确实发生在工作线程（非属主 loop 线程）
    assert settle_thread and settle_thread[0] != loop_thread
    # 归还最终由属主 loop 完成
    await asyncio.sleep(0)
    assert token.state is TokenState.RETURNED
    assert pool.semaphore._value == 1
    pool.assert_conservation()
    assert pool.counters().R == 1


@pytest.mark.asyncio
async def test_attach_to_real_future_settles_on_completion():
    pool = SlotPool("t-future", 1)
    await pool.semaphore.acquire()
    token = pool.acquire_token()

    real: concurrent.futures.Future = concurrent.futures.Future()
    pool.attach(real, token)

    # 任务尚未结束：不得结算
    await asyncio.sleep(0)
    assert token.state is TokenState.ACTIVE
    assert pool.counters().P == 1

    th = threading.Thread(target=lambda: real.set_result("done"))
    th.start()
    await asyncio.sleep(0.05)
    th.join(timeout=5.0)

    assert token.state is TokenState.RETURNED
    pool.assert_conservation()
    assert pool.semaphore._value == 1


# ------------------------------------------------------------------- 注册表


@pytest.mark.asyncio
async def test_lifecycle_registry_reuses_pool_by_name():
    lc = ExecutorLifecycle()
    a = lc.pool("light", 4)
    b = lc.pool("light", 4)
    assert a is b
    assert lc.names() == ["light"]

    await a.semaphore.acquire()
    t = a.acquire_token()
    a.settle(t)
    await asyncio.sleep(0)
    lc.assert_conservation()
    assert lc.snapshot()["light"]["counters"][1]["R"] == 1
