"""集成测试：AI Debug Agent 端到端真实链路。

目标：
- 验证 POST /api/debug/repair/async → GET /api/debug/repair/result/{job_id} 完整链路
- 验证 repair_plan 结构完整（patch/affected_files/validation_strategy/risk_assessment/confidence）
- 验证 sources.vector_recall / git_context 字段存在
- 验证 agent_trace 非空

说明：
- 依赖真实 OpenAI/智谱 API Key，默认 skip
- 需配置 OPENAI_API_KEY + AGENT_ENABLED=true 才能运行
"""

import asyncio

import pytest

from app.config import settings


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def enable_agent(monkeypatch):
    """强制开启 agent_enabled（测试用）。"""
    monkeypatch.setattr(settings, "agent_enabled", True)
    monkeypatch.setattr(settings, "agent_queue_maxsize", 10)
    monkeypatch.setattr(settings, "agent_queue_workers", 1)


@pytest.fixture
def fresh_repair_queue(monkeypatch):
    """每个测试用全新 RepairQueue 单例，避免状态污染。"""
    import app.agent.repair_queue as rq

    monkeypatch.setattr(rq, "_repair_queue", None)
    return rq.get_repair_queue()


async def _enqueue_and_wait(queue, context, timeout=30):
    """入队并轮询等待完成，返回最终 job。"""
    job_id = await queue.enqueue(context, model=None)
    await queue.start(1)
    try:
        for _ in range(int(timeout * 10)):
            job = queue.get_job(job_id)
            if job and job["status"] in ("done", "failed"):
                return job
            await asyncio.sleep(0.1)
        # 超时返回最后状态
        return queue.get_job(job_id)
    finally:
        await queue.drain(timeout=5.0)


@pytest.mark.llm
@pytest.mark.skipif(
    not settings.openai_api_key,
    reason="需要 OPENAI_API_KEY 才能运行 Agent e2e 测试",
)
class TestAgentRepairE2E:
    """完整链路：repair/async → repair/result。"""

    async def test_repair_returns_valid_plan(self, fresh_repair_queue):
        """RepairAgent 应返回结构完整的 repair_plan。"""
        debug_context = {
            "request_id": "e2e-test-001",
            "flow": ["input", "process", "error"],
            "errors": [
                {
                    "type": "KeyError",
                    "message": "'user_id'",
                    "fingerprint": "fp-e2e-001",
                    "frames": [
                        {"file": __file__, "line": 1, "function": "test_fn"},
                    ],
                }
            ],
            "exception": {
                "type": "KeyError",
                "message": "'user_id'",
                "frames": [
                    {"file": __file__, "line": 1, "function": "test_fn"},
                ],
            },
        }

        job = await _enqueue_and_wait(fresh_repair_queue, debug_context)

        assert job is not None
        assert job["status"] == "done", f"job failed: {job.get('error')}"
        assert job["result"] is not None

        result = job["result"]
        assert "repair_plan" in result
        assert result["repair_plan"] is not None

        plan = result["repair_plan"]
        assert "patch" in plan
        assert "affected_files" in plan
        assert "validation_strategy" in plan
        assert "risk_assessment" in plan
        assert "confidence" in plan
        assert plan["confidence"] in ("high", "medium", "low")

    async def test_repair_returns_sources_and_trace(self, fresh_repair_queue):
        """返回结果应包含 sources 和 agent_trace 字段。"""
        debug_context = {
            "request_id": "e2e-test-002",
            "errors": [
                {
                    "type": "ValueError",
                    "message": "invalid input",
                    "fingerprint": "fp-e2e-002",
                }
            ],
        }

        job = await _enqueue_and_wait(fresh_repair_queue, debug_context)

        assert job["status"] == "done", f"job failed: {job.get('error')}"
        result = job["result"]

        assert "sources" in result
        assert "vector_recall" in result["sources"]
        assert "git_context" in result["sources"]
        assert "knowledge_base_hit" in result["sources"]

        assert "agent_trace" in result
        assert len(result["agent_trace"]) >= 1
        trace = result["agent_trace"][0]
        assert trace["agent_name"] == "repair"
        assert trace["status"] in ("success", "failed")
