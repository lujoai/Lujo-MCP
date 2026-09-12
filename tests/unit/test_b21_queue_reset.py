"""W4-5（B21）queue drain 后复位单例验收：两轮 lifespan（真关旧 loop）。

对应 DESIGN_C4 §5.2 与 CHECKLIST W4-5：
- drain 后把模块单例置 None（下一轮 lifespan 经 start 重建）；
- **先确认 worker 已 cancel 并 await 完成**再复位（不得先复位再取消）；
- 不启用 Repair Loop（is_agent_active 默认关闭口径不变）；
- 两轮 lifespan 用例必须**真正关闭旧 loop 后新建**（同 loop 两轮不构成
  B20/B21 证据，C4 §7 R13）：断言单例不复位 → 第二轮绑旧 loop（红）；
  复位+重建（绿）；worker 数不翻倍；可 enqueue/drain。

既有断言不删除：本文件只新增 B21 复位断言（既有 drain 语义测试继续有效）。
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from app.llm.analysis_queue import (
    drain_analysis_queue,
    start_analysis_queue,
)
from app.llm import analysis_queue as aq_mod
from app.agent import repair_queue as rq_mod
from app.agent.repair_queue import (
    drain_repair_queue,
    start_repair_queue,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _reset_singletons():
    aq_mod._analysis_queue = None
    rq_mod._repair_queue = None
    yield
    aq_mod._analysis_queue = None
    rq_mod._repair_queue = None


def _two_rounds(module, start_fn, drain_fn, workers_setting):
    """两轮 lifespan（真关旧 loop 后新建）：返回 (q1, q2, workers_per_round)。"""
    # ── 第一轮：loop1 ──
    loop1 = asyncio.new_event_loop()
    try:
        async def _round1():
            await start_fn()
            q = module._analysis_queue if module is aq_mod else module._repair_queue
            return q, len(q._workers)
        q1, w1 = loop1.run_until_complete(_round1())
        # enqueue/drain 可用
        loop1.run_until_complete(drain_fn(timeout=2.0))
    finally:
        loop1.close()  # 真正关闭旧 loop（R13：同 loop 两轮不构成证据）

    # ── 第二轮：loop2（全新）──
    loop2 = asyncio.new_event_loop()
    try:
        async def _round2():
            await start_fn()
            q = module._analysis_queue if module is aq_mod else module._repair_queue
            return q, len(q._workers)
        q2, w2 = loop2.run_until_complete(_round2())
        loop2.run_until_complete(drain_fn(timeout=2.0))
    finally:
        loop2.close()

    return q1, q2, w1, w2


def test_analysis_queue_two_rounds_fresh_singleton_no_worker_growth():
    """B21：两轮 lifespan（真关旧 loop）→ 单例不复位则第二轮绑旧 loop（旧缺陷）；
    复位后第二轮全新队列、worker 数恰好一轮配置量（不翻倍）。"""
    from app.config import settings

    module = aq_mod

    # 第一轮
    loop1 = asyncio.new_event_loop()
    try:
        async def _round1():
            await start_analysis_queue()
            q = module._analysis_queue
            return q, len(q._workers)
        q1, w1 = loop1.run_until_complete(_round1())
        loop1.run_until_complete(drain_analysis_queue(timeout=2.0))
    finally:
        loop1.close()

    # drain 后：模块单例必须已复位为 None（W4-5 新断言）
    assert module._analysis_queue is None, "drain 后单例未复位（B21 根因未修）"

    # 第二轮（全新 loop）
    loop2 = asyncio.new_event_loop()
    try:
        async def _round2():
            await start_analysis_queue()
            q = module._analysis_queue
            return q, len(q._workers)
        q2, w2 = loop2.run_until_complete(_round2())
        loop2.run_until_complete(drain_analysis_queue(timeout=2.0))
    finally:
        loop2.close()

    assert q2 is not q1, "第二轮复用了绑旧 loop 的队列单例（B21 缺陷）"
    expected = settings.llm_queue_workers
    assert w1 == expected and w2 == expected, f"worker 数翻倍: {w1} -> {w2}"
    assert module._analysis_queue is None  # 第二轮 drain 后同样复位


def test_repair_queue_two_rounds_fresh_singleton_no_worker_growth():
    """repair 队列同口径（is_agent_active 默认关闭口径不变——本测试直接调
    队列模块，不经 main.py 的 agent gating）。"""
    from app.config import settings

    module = rq_mod

    loop1 = asyncio.new_event_loop()
    try:
        async def _round1():
            await start_repair_queue()
            q = module._repair_queue
            return q, len(q._workers)
        q1, w1 = loop1.run_until_complete(_round1())
        loop1.run_until_complete(drain_repair_queue(timeout=2.0))
    finally:
        loop1.close()

    assert module._repair_queue is None

    loop2 = asyncio.new_event_loop()
    try:
        async def _round2():
            await start_repair_queue()
            q = module._repair_queue
            return q, len(q._workers)
        q2, w2 = loop2.run_until_complete(_round2())
        loop2.run_until_complete(drain_repair_queue(timeout=2.0))
    finally:
        loop2.close()

    assert q2 is not q1
    expected = settings.agent_queue_workers
    assert w1 == expected and w2 == expected, f"worker 数翻倍: {w1} -> {w2}"


def test_enqueue_still_works_across_rounds():
    """可 enqueue/drain：复位+重建后入队与排空语义不变（C 类既有回归）。"""
    module = aq_mod

    async def _round():
        await start_analysis_queue()
        q = module._analysis_queue
        job_id = await q.enqueue({"payload": {"x": 1}})
        stats = await drain_analysis_queue(timeout=2.0)
        return job_id, stats

    # 第一轮
    loop1 = asyncio.new_event_loop()
    try:
        job1, stats1 = loop1.run_until_complete(_round())
        assert job1
        assert stats1["drained"] >= 0
    finally:
        loop1.close()

    # 第二轮（真关旧 loop 后新建）
    loop2 = asyncio.new_event_loop()
    try:
        job2, stats2 = loop2.run_until_complete(_round())
        assert job2
    finally:
        loop2.close()
    assert module._analysis_queue is None
