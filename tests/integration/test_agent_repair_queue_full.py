"""集成测试：修复队列满时返回 429。

验证背压机制：maxsize=1 不启 worker，第 2 个 enqueue 触发 QueueFullError → API 429。
"""

import pytest

from app.agent.repair_queue import QueueFullError, RepairQueue


class TestRepairQueueFullBackpressure:
    """队列满 → QueueFullError 背压。"""

    @pytest.mark.asyncio
    async def test_queue_full_raises_error(self):
        """maxsize=1，第 2 个 enqueue 抛 QueueFullError。"""
        q = RepairQueue(maxsize=1, concurrency=1)
        # 不启动 worker，确保队列不消化
        j1 = await q.enqueue({"i": 1})
        assert j1

        with pytest.raises(QueueFullError):
            await q.enqueue({"i": 2})

    @pytest.mark.asyncio
    async def test_queue_full_does_not_leak_job_state(self):
        """QueueFullError 后，被拒的 job_id 不应残留在 _jobs。"""
        q = RepairQueue(maxsize=1, concurrency=1)
        await q.enqueue({"i": 1})

        try:
            rejected_id = await q.enqueue({"i": 2})
        except QueueFullError:
            rejected_id = None

        assert rejected_id is None
        # 验证 _jobs 中只有 1 个条目（第一个成功的）
        assert len(q._jobs) == 1

    @pytest.mark.asyncio
    async def test_queue_size_reports_correctly(self):
        """queue_size() 准确反映待消费任务数。"""
        q = RepairQueue(maxsize=5, concurrency=1)
        assert q.queue_size() == 0

        await q.enqueue({"i": 1})
        await q.enqueue({"i": 2})
        assert q.queue_size() == 2

    @pytest.mark.asyncio
    async def test_api_returns_429_on_queue_full(self, monkeypatch):
        """REST 端点在队列满时返回 429 + queue_size。"""
        from fastapi.testclient import TestClient

        import app.agent.repair_queue as rq
        from app.config import settings
        from app.main import app

        # 平台隔离：Windows 上 os.environ["API_KEY"]="" 物理无效（等价 unset），
        # settings 会回落读取 .env 真实 API_KEY，使 AuthMiddleware 以 401 拒绝本测试的
        # HTTP 请求。此处 monkeypatch settings 单例为「未配置」，让鉴权实时判定关闭。
        monkeypatch.setattr(settings, "api_key", None)
        monkeypatch.setattr(settings, "api_keys", "")

        # 强制开启 agent
        monkeypatch.setattr(settings, "agent_enabled", True)
        # 注入一个 maxsize=1 的队列，且不启 worker
        monkeypatch.setattr(rq, "_repair_queue", None)
        monkeypatch.setattr(settings, "agent_queue_maxsize", 1)
        monkeypatch.setattr(settings, "agent_queue_workers", 1)

        # 先用 logs API 注入一个 trace（绕过 build_context 的 404）
        from app.runtime.core.logs import create_request_id, add_log

        req_id = create_request_id()
        add_log(req_id, "request_start", {"test": True})

        client = TestClient(app)
        # 第 1 次入队成功（占用 maxsize=1）
        r1 = client.post("/api/debug/repair/async", json={"request_id": req_id})
        assert r1.status_code == 200

        # 第 2 次入队 → 429
        r2 = client.post("/api/debug/repair/async", json={"request_id": req_id})
        assert r2.status_code == 429
        body = r2.json()
        assert body["error"] == "queue_full"
        assert "queue_size" in body
