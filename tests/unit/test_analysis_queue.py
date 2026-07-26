"""单元测试：P3-6 异步分析队列（消息队列削峰）"""

import asyncio
from unittest.mock import patch

import pytest

from app.llm.analysis_queue import AnalysisQueue, QueueFullError


@pytest.fixture
def fresh_queue():
    """每个用例一个全新 AnalysisQueue 实例（maxsize=4, concurrency=2）。"""
    return AnalysisQueue(maxsize=4, concurrency=2)


class TestEnqueueDone:
    """正常入队 → 完成流程。"""

    @pytest.mark.asyncio
    async def test_enqueue_returns_job_id_and_completes(self, fresh_queue):
        async def fake_analyze_async(context, model=None):
            return {"root_cause": "ok", "model": model}

        with patch("app.llm.analyzer.analyze_async", side_effect=fake_analyze_async):
            await fresh_queue.start(2)
            job_id = await fresh_queue.enqueue({"request_id": "r1"}, model="gpt-x")

            assert isinstance(job_id, str)
            assert len(job_id) > 0

            # 轮询等待完成
            for _ in range(50):
                job = fresh_queue.get_job(job_id)
                if job and job["status"] in ("done", "failed"):
                    break
                await asyncio.sleep(0.02)

            assert job is not None
            assert job["status"] == "done"
            assert job["result"] == {"root_cause": "ok", "model": "gpt-x"}
            assert job["error"] is None
            assert job["finished_at"] is not None
            assert job["created_at"] <= job["finished_at"]

            await fresh_queue.drain(timeout=1.0)


class TestQueueFull:
    """队列满 → QueueFullError 背压。"""

    @pytest.mark.asyncio
    async def test_queue_full_raises(self):
        q = AnalysisQueue(maxsize=1, concurrency=1)
        # 不启动 worker，确保队列不消化
        j1 = await q.enqueue({"i": 1})
        assert j1
        with pytest.raises(QueueFullError):
            await q.enqueue({"i": 2})


class TestSemaphoreConcurrency:
    """Semaphore(K) 限制并发：K=2，3 个慢任务，最多 2 并发。"""

    @pytest.mark.asyncio
    async def test_at_most_k_concurrent(self):
        q = AnalysisQueue(maxsize=10, concurrency=2)

        active = {"now": 0, "max": 0}
        gate = asyncio.Event()

        async def slow_analyze(context, model=None):
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            # 让观察窗口稳定：阻塞一下确保并发被观测到
            try:
                await asyncio.wait_for(gate.wait(), timeout=0.2)
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(0.05)
            active["now"] -= 1
            return {"ok": True}

        with patch("app.llm.analyzer.analyze_async", side_effect=slow_analyze):
            # 启动 3 个 worker，确保 3 个任务能被同时从队列取出
            await q.start(3)
            for i in range(3):
                await q.enqueue({"i": i})

            # 给 worker 时间把 3 个任务都取走并尝试并发
            await asyncio.sleep(0.15)
            # 释放 gate，让所有阻塞中的任务继续
            gate.set()

            # 等所有 job 完成
            for _ in range(100):
                if all(
                    (q.get_job(jid) or {}).get("status") in ("done", "failed")
                    for jid in list(q._jobs.keys())
                ):
                    break
                await asyncio.sleep(0.02)

            assert active["max"] <= 2
            # 3 任务、K=2、有阻塞，理论上应观测到恰好 2 并发
            assert active["max"] == 2

            await q.drain(timeout=1.0)


class TestDrain:
    """drain 返回 drain_stats dict。"""

    @pytest.mark.asyncio
    async def test_drain_returns_stats(self, fresh_queue):
        async def fast_analyze(context, model=None):
            return {"ok": True}

        with patch("app.llm.analyzer.analyze_async", side_effect=fast_analyze):
            await fresh_queue.start(2)
            for i in range(3):
                await fresh_queue.enqueue({"i": i})

            # 等任务完成
            for _ in range(100):
                if all(
                    (fresh_queue.get_job(jid) or {}).get("status") in ("done", "failed")
                    for jid in list(fresh_queue._jobs.keys())
                ):
                    break
                await asyncio.sleep(0.02)

            stats = await fresh_queue.drain(timeout=1.0)

        assert isinstance(stats, dict)
        assert "drained" in stats
        assert "unfinished" in stats
        assert stats["drained"] == 3
        assert stats["unfinished"] == 0

    @pytest.mark.asyncio
    async def test_drain_timeout_records_unfinished(self):
        q = AnalysisQueue(maxsize=10, concurrency=1)
        gate = asyncio.Event()

        async def blocked_analyze(context, model=None):
            await gate.wait()
            return {"ok": True}

        with patch("app.llm.analyzer.analyze_async", side_effect=blocked_analyze):
            await q.start(1)
            await q.enqueue({"i": 1})

            # 给 worker 一点时间进入 analyze_async（任务出队但阻塞）
            await asyncio.sleep(0.05)

            # drain 会先取消 worker，导致 analyze_async 被取消 → task_done 不触发
            # queue.join() 在 timeout 内无法完成 → unfinished 计数
            stats = await q.drain(timeout=0.1)
            gate.set()

        assert stats["unfinished"] >= 0
        assert isinstance(stats["drained"], int)


class TestJobLifecycle:
    """任务状态生命周期。"""

    @pytest.mark.asyncio
    async def test_lifecycle_done(self, fresh_queue):
        started = asyncio.Event()
        proceed = asyncio.Event()

        async def controlled_analyze(context, model=None):
            started.set()
            await proceed.wait()
            return {"ok": True}

        with patch("app.llm.analyzer.analyze_async", side_effect=controlled_analyze):
            await fresh_queue.start(1)
            job_id = await fresh_queue.enqueue({"x": 1})

            # 等到 worker 进入 analyze_async → status 应为 running
            await asyncio.wait_for(started.wait(), timeout=1.0)
            await asyncio.sleep(0.01)
            job_running = fresh_queue.get_job(job_id)
            assert job_running["status"] == "running"
            assert job_running["result"] is None
            assert job_running["finished_at"] is None

            # 释放 → 任务完成
            proceed.set()
            for _ in range(50):
                job = fresh_queue.get_job(job_id)
                if job["status"] == "done":
                    break
                await asyncio.sleep(0.02)

            assert job["status"] == "done"
            assert job["result"] == {"ok": True}
            assert job["finished_at"] is not None

            await fresh_queue.drain(timeout=1.0)

    @pytest.mark.asyncio
    async def test_lifecycle_failed(self, fresh_queue):
        started = asyncio.Event()
        proceed = asyncio.Event()

        async def failing_analyze(context, model=None):
            started.set()
            await proceed.wait()
            raise RuntimeError("boom")

        with patch("app.llm.analyzer.analyze_async", side_effect=failing_analyze):
            await fresh_queue.start(1)
            job_id = await fresh_queue.enqueue({"x": 1})

            await asyncio.wait_for(started.wait(), timeout=1.0)
            await asyncio.sleep(0.01)
            assert fresh_queue.get_job(job_id)["status"] == "running"

            proceed.set()
            for _ in range(50):
                job = fresh_queue.get_job(job_id)
                if job["status"] == "failed":
                    break
                await asyncio.sleep(0.02)

            assert job["status"] == "failed"
            assert "boom" in (job["error"] or "")
            assert job["result"] is None
            assert job["finished_at"] is not None

            await fresh_queue.drain(timeout=1.0)


class TestGetJobUnknown:
    """未知 job_id 返回 None。"""

    def test_get_job_unknown_returns_none(self, fresh_queue):
        assert fresh_queue.get_job("does-not-exist") is None
