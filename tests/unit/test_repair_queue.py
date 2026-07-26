"""单元测试：AI Debug Agent 修复队列（repair_queue.py）。

结构对称 test_analysis_queue.py：enqueue/get_job/drain/QueueFull/Semaphore。
"""

import asyncio
from unittest.mock import patch

import pytest

from app.agent.repair_queue import QueueFullError, RepairQueue


@pytest.fixture
def fresh_queue():
    """每个用例一个全新 RepairQueue 实例（maxsize=4, concurrency=2）。"""
    return RepairQueue(maxsize=4, concurrency=2)


def _make_repair_result():
    """构造一个 Coordinator.run 的 fake 返回值。"""
    return {
        "repair_plan": {
            "patch": "modify line 42",
            "affected_files": ["app/foo.py"],
            "validation_strategy": "run pytest",
            "risk_assessment": "low",
            "confidence": "high",
        },
        "sources": {"vector_recall": [], "git_context": [], "knowledge_base_hit": False},
        "agent_trace": [{"agent_name": "repair", "status": "success"}],
    }


class TestEnqueueDone:
    """正常入队 → 完成流程。"""

    @pytest.mark.asyncio
    async def test_enqueue_returns_job_id_and_completes(self, fresh_queue):
        async def fake_coordinator_run(context, model=None):
            return _make_repair_result()

        with patch(
            "app.agent.coordinator.Coordinator.run", side_effect=fake_coordinator_run
        ):
            await fresh_queue.start(2)
            job_id = await fresh_queue.enqueue({"request_id": "r1"}, model="gpt-x")

            assert isinstance(job_id, str)
            assert len(job_id) > 0

            # 轮询等待完成
            job = None
            for _ in range(50):
                job = fresh_queue.get_job(job_id)
                if job and job["status"] in ("done", "failed"):
                    break
                await asyncio.sleep(0.02)

            assert job is not None
            assert job["status"] == "done"
            assert job["result"] == _make_repair_result()
            assert job["error"] is None
            assert job["finished_at"] is not None
            assert job["created_at"] <= job["finished_at"]

            await fresh_queue.drain(timeout=1.0)


class TestQueueFull:
    """队列满 → QueueFullError 背压。"""

    @pytest.mark.asyncio
    async def test_queue_full_raises(self):
        q = RepairQueue(maxsize=1, concurrency=1)
        # 不启动 worker，确保队列不消化
        j1 = await q.enqueue({"i": 1})
        assert j1
        with pytest.raises(QueueFullError):
            await q.enqueue({"i": 2})


class TestSemaphoreConcurrency:
    """Semaphore(K) 限制并发：K=2，3 个慢任务，最多 2 并发。"""

    @pytest.mark.asyncio
    async def test_at_most_k_concurrent(self):
        q = RepairQueue(maxsize=10, concurrency=2)

        active = {"now": 0, "max": 0}
        gate = asyncio.Event()

        async def slow_coordinator(context, model=None):
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            try:
                await asyncio.wait_for(gate.wait(), timeout=0.2)
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(0.05)
            active["now"] -= 1
            return _make_repair_result()

        with patch(
            "app.agent.coordinator.Coordinator.run", side_effect=slow_coordinator
        ):
            await q.start(2)
            for i in range(3):
                await q.enqueue({"i": i})

            # 等待所有任务完成
            await asyncio.sleep(0.4)
            gate.set()
            await q.drain(timeout=1.0)

        assert active["max"] <= 2, f"并发超过 K=2: max={active['max']}"


class TestJobFailure:
    """Coordinator 抛异常 → job 标 failed。"""

    @pytest.mark.asyncio
    async def test_job_failed_on_exception(self, fresh_queue):
        async def boom(context, model=None):
            raise RuntimeError("coordinator crashed")

        with patch(
            "app.agent.coordinator.Coordinator.run", side_effect=boom
        ):
            await fresh_queue.start(1)
            job_id = await fresh_queue.enqueue({"x": 1})

            job = None
            for _ in range(50):
                job = fresh_queue.get_job(job_id)
                if job and job["status"] in ("done", "failed"):
                    break
                await asyncio.sleep(0.02)

            assert job is not None
            assert job["status"] == "failed"
            assert "coordinator crashed" in job["error"]
            assert job["result"] is None

            await fresh_queue.drain(timeout=1.0)


class TestDrainStats:
    """drain 返回 {drained, unfinished} 统计。"""

    @pytest.mark.asyncio
    async def test_drain_returns_stats(self, fresh_queue):
        async def fake_run(context, model=None):
            return _make_repair_result()

        with patch(
            "app.agent.coordinator.Coordinator.run", side_effect=fake_run
        ):
            await fresh_queue.start(2)
            await fresh_queue.enqueue({"i": 1})
            await fresh_queue.enqueue({"i": 2})

            # 等待完成
            await asyncio.sleep(0.2)
            stats = await fresh_queue.drain(timeout=1.0)

        assert "drained" in stats
        assert "unfinished" in stats
        assert stats["drained"] >= 2
        assert stats["unfinished"] == 0


class TestGetJobUnknown:
    """未知 job_id 返回 None。"""

    def test_get_job_unknown_returns_none(self, fresh_queue):
        assert fresh_queue.get_job("nonexistent") is None

    def test_queue_size_empty(self, fresh_queue):
        assert fresh_queue.queue_size() == 0
