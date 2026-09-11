"""DESIGN C1 §5 B20 验收：容量代际 getter、等待者取消与真·双 loop 重建。

对应 docs/internal/CHECKLIST_C_BATCH.md W1-7 与 DESIGN_C1_SLOT_ACCOUNTING.md §5/§6。
分类：**B 类·新机制验收**（代际属主 getter / 关闭取消等待者 / 真·双 loop 重建）。

R13 纪律：B20 的红灯证据必须「真正 close 旧 loop 后新建第二个 loop」——
同一 loop 上跑两轮不构成证据。
"""

from __future__ import annotations

import asyncio

import pytest

from app.mcp.protocol import server as server_module
from app.mcp.protocol.executor_lifecycle import SlotPool

_LIGHT_TOOL_NAME = "__gen_test_light_tool__"
_HEAVY_TOOL_NAME = "__gen_test_heavy_tool__"


def _fresh_pools(monkeypatch, light_capacity: int = 1, heavy_capacity: int = 2):
    """用全新 SlotPool 替换 server 模块的代际属主（账目与信号量双双隔离）。"""
    light_pool = SlotPool("light", light_capacity)
    heavy_pool = SlotPool("heavy", heavy_capacity)
    monkeypatch.setattr(server_module, "_light_pool", light_pool)
    monkeypatch.setattr(server_module, "_heavy_pool", heavy_pool)
    return light_pool, heavy_pool


def _register_heavy_probe(monkeypatch) -> None:
    monkeypatch.setitem(
        server_module._tool_registry, _HEAVY_TOOL_NAME, {"heavy": True}
    )


# ------------------------------------------------------- B20 核心：代际 getter


@pytest.mark.asyncio
async def test_getter_follows_generation_rebuild(monkeypatch):
    """getter 必须每次从代际属主重读当前代，禁止缓存裸对象。

    红灯形态（旧实现）：模块级裸 ``_tool_slots``/``_heavy_tool_slots`` 在
    ``start_new_generation`` 之后仍被原样返回——池已换代、调用方还拿旧
    semaphore（import 即永生，B20 缺陷本体）。
    """
    light_pool, heavy_pool = _fresh_pools(monkeypatch)
    _register_heavy_probe(monkeypatch)

    _, sem_light_gen1, pool_type = server_module._get_tool_executor_and_slots(
        _LIGHT_TOOL_NAME
    )
    assert pool_type == "light"
    assert sem_light_gen1 is light_pool.semaphore, "getter 必须返回属主当前代信号量"
    _, sem_heavy_gen1, pool_type = server_module._get_tool_executor_and_slots(
        _HEAVY_TOOL_NAME
    )
    assert pool_type == "heavy"
    assert sem_heavy_gen1 is heavy_pool.semaphore

    # 换代：五项条件在无在途 token 时序贯成立（begin_close → retire → 新代）
    light_pool.begin_close()
    assert light_pool.retire() is True
    assert light_pool.start_new_generation() == 2
    heavy_pool.begin_close()
    assert heavy_pool.retire() is True
    assert heavy_pool.start_new_generation() == 2

    _, sem_light_gen2, _ = server_module._get_tool_executor_and_slots(_LIGHT_TOOL_NAME)
    _, sem_heavy_gen2, _ = server_module._get_tool_executor_and_slots(_HEAVY_TOOL_NAME)

    assert sem_light_gen2 is not sem_light_gen1, "B20：换代后 getter 不得再返回旧代信号量"
    assert sem_light_gen2 is light_pool.semaphore
    assert sem_light_gen2._value == light_pool.capacity, "第二代容量必须恢复满额"
    assert sem_heavy_gen2 is not sem_heavy_gen1
    assert sem_heavy_gen2 is heavy_pool.semaphore


# --------------------------------------- 代际关闭：等待者取消 + TOOL_BUSY 映射


@pytest.mark.asyncio
async def test_generation_close_cancels_waiters_as_tool_busy(monkeypatch):
    """关闭代际时已登记等待者统一取消、按 TOOL_BUSY 失败，绝不迁移到新代。

    ``_acquire_slot_or_fastfail`` 返回 False 即调用方的 TOOL_BUSY 响应路径。
    迁移 = 两代容量叠加 = 容量翻倍（DESIGN C1 §5 规则 2 明令禁止），
    以「等待者结果为 False 且新代满容量」双向证明。
    """
    pool, _ = _fresh_pools(monkeypatch)
    sem1 = pool.semaphore
    await sem1.acquire()  # 占满唯一许可（模拟在途真实任务）

    waiter = asyncio.ensure_future(
        server_module._acquire_slot_or_fastfail(sem1, 5.0, pool=pool)
    )
    for _ in range(5):
        await asyncio.sleep(0)
    assert not waiter.done(), "容量已占满，等待者必须仍阻塞在 acquire 上"

    pool.begin_close()
    assert pool.cancel_waiters() == 1
    assert await waiter is False, "关闭代际的等待者必须按 TOOL_BUSY 失败（返回 False）"

    pool.assert_conservation()
    assert pool.retire() is True
    assert pool.start_new_generation() == 2
    sem2 = pool.semaphore
    assert sem2 is not sem1
    assert sem2._value == pool.capacity, (
        "禁止迁移等待者：新代必须满容量（若迁移则此处必然缺员）"
    )
    assert pool.snapshot()["waiters"] == 0, "等待者必须在退出后注销，注册表不残留"


# ------------------------------- R13 强制形态：真·双 loop 重建，无泄漏无翻倍


def test_two_real_event_loops_rebuild_without_leak(monkeypatch):
    """真正 close 旧 loop 后新建第二个 loop：换代容量恢复、无绑定异常、
    无许可泄漏、守恒成立。同一 loop 两轮不构成 B20 证据（R13）。

    两个 loop 均为 ``asyncio.new_event_loop`` 显式创建的**真实独立 loop**
    （本测试自身是同步用例，不借用 pytest-asyncio 的 loop）。
    """
    pool, _ = _fresh_pools(monkeypatch)

    loop1 = asyncio.new_event_loop()
    try:

        async def phase1():
            sem1 = pool.semaphore
            await sem1.acquire()  # 许可由「真实在途任务」持有
            token = pool.acquire_token()  # 生产同型：先 acquire 后登记
            waiter = asyncio.ensure_future(
                server_module._acquire_slot_or_fastfail(sem1, 5.0, pool=pool)
            )
            for _ in range(5):
                await asyncio.sleep(0)
            pool.begin_close()
            assert pool.cancel_waiters() == 1
            assert await waiter is False, "第一代等待者按 TOOL_BUSY 失败且不迁移"
            pool.settle(token)  # 在途任务终结 → RETURN_PENDING
            await asyncio.sleep(0)  # 让属主 loop 上的归还回调跑完（R 确定性入账）
            return sem1

        sem1 = loop1.run_until_complete(phase1())
    finally:
        loop1.close()  # 真·关闭旧 loop（R13）

    # 旧 loop 已死，代际收口：P=0 且 Q=0 → 原子退役 → 新代
    assert pool.retire() is True
    assert pool.start_new_generation() == 2

    loop2 = asyncio.new_event_loop()
    try:

        async def phase2():
            _, got, pool_type = server_module._get_tool_executor_and_slots(
                _LIGHT_TOOL_NAME
            )
            assert pool_type == "light"
            assert got is not sem1, "B20：新 loop 上 getter 必须给出新代信号量"
            assert got is pool.semaphore
            assert got._value == pool.capacity, (
                "第二代容量恢复（旧 loop 死亡不得泄漏许可、不得翻倍叠加）"
            )
            await got.acquire()  # 新 loop 上完整走一遍 acquire/release
            got.release()
            pool.assert_conservation()

        loop2.run_until_complete(phase2())
    finally:
        loop2.close()
