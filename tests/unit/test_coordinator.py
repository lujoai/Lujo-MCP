"""单元测试：Coordinator 编排器（coordinator.py）。

覆盖：单 Agent 串行流程、agent_trace 收集、RepairAgent 失败时降级。
"""

import pytest
from unittest.mock import patch

from app.agent.base import AgentResult, AgentStatus, AgentContext
from app.agent.coordinator import Coordinator
from app.config import AgentMode, Settings


@pytest.fixture(autouse=True)
def _force_phase1(monkeypatch):
    """本文件覆盖 Phase 1 单 Agent 串行逻辑。

    若 .env 中开启了 AGENT_MULTI_AGENT_ENABLED / AGENT_VERIFY_LOOP_ENABLED，
    Coordinator.run 会走 Phase 2 DAG / Verify Loop 真实 LLM 路径，导致 mock
    的 coord._agents["repair"] 不生效。此处强制关闭，保证测试确定走 Phase 1。
    """
    monkeypatch.setattr("app.config.settings.agent_multi_agent_enabled", False)
    monkeypatch.setattr("app.config.settings.agent_verify_loop_enabled", False)


def _make_debug_context():
    return {
        "request_id": "r1",
        "exception": {"type": "ValueError", "message": "bad input"},
    }


def _make_assemble_result():
    """RepairContextAssembler.assemble 的 fake 返回值。"""
    return {
        "debug_context": _make_debug_context(),
        "prior_analysis": {"analysis": {"root_cause": "x"}, "knowledge_base_hit": False},
        "vector_recall": [{"fingerprint": "fp1"}],
        "git_context": [{"file": "/app/foo.py", "diff": "..."}],
        "sources": {
            "vector_recall": [{"fingerprint": "fp1"}],
            "git_context": [{"file": "/app/foo.py", "diff": "..."}],
            "knowledge_base_hit": False,
        },
    }


class TestCoordinatorSuccess:
    """Coordinator 正常流程。"""

    @pytest.mark.asyncio
    async def test_run_returns_repair_plan_and_trace(self):
        coord = Coordinator()

        fake_agent_result = AgentResult(
            agent_name="repair",
            status=AgentStatus.SUCCESS,
            output={
                "repair_plan": {
                    "patch": "modify line 42",
                    "affected_files": ["app/foo.py"],
                    "validation_strategy": "pytest",
                    "risk_assessment": "low",
                    "confidence": "high",
                }
            },
            started_at=1.0,
            finished_at=2.0,
            usage={"total_tokens": 100},
        )

        with patch.object(
            coord._assembler, "assemble", return_value=_make_assemble_result()
        ), patch.object(
            coord._agents["repair"], "run", return_value=fake_agent_result
        ):
            result = await coord.run(_make_debug_context())

        assert result["repair_plan"]["patch"] == "modify line 42"
        assert result["repair_plan"]["confidence"] == "high"
        assert len(result["agent_trace"]) == 1
        assert result["agent_trace"][0]["agent_name"] == "repair"
        assert result["agent_trace"][0]["status"] == "success"
        assert result["sources"]["knowledge_base_hit"] is False

    @pytest.mark.asyncio
    async def test_sources_propagated_from_assembler(self):
        """sources 字段从 assembler 透传到最终输出。"""
        coord = Coordinator()
        assemble_result = _make_assemble_result()
        assemble_result["sources"]["knowledge_base_hit"] = True

        fake_agent_result = AgentResult(
            agent_name="repair",
            status=AgentStatus.SUCCESS,
            output={"repair_plan": {"patch": "fix"}},
        )

        with patch.object(
            coord._assembler, "assemble", return_value=assemble_result
        ), patch.object(
            coord._agents["repair"], "run", return_value=fake_agent_result
        ):
            result = await coord.run(_make_debug_context())

        assert result["sources"]["knowledge_base_hit"] is True
        assert len(result["sources"]["vector_recall"]) == 1


class TestCoordinatorDegradation:
    """RepairAgent 失败时静默降级。"""

    @pytest.mark.asyncio
    async def test_agent_failed_returns_none_plan(self):
        coord = Coordinator()

        fake_agent_result = AgentResult(
            agent_name="repair",
            status=AgentStatus.FAILED,
            output={},
            error="LLM timeout",
        )

        with patch.object(
            coord._assembler, "assemble", return_value=_make_assemble_result()
        ), patch.object(
            coord._agents["repair"], "run", return_value=fake_agent_result
        ):
            result = await coord.run(_make_debug_context())

        assert result["repair_plan"] is None
        assert len(result["agent_trace"]) == 1
        assert result["agent_trace"][0]["status"] == "failed"
        assert result["agent_trace"][0]["error"] == "LLM timeout"

    @pytest.mark.asyncio
    async def test_agent_exception_caught_by_coordinator(self):
        """RepairAgent.run 抛异常（不应发生，但 Coordinator 要兜底）。"""
        coord = Coordinator()

        with patch.object(
            coord._assembler, "assemble", return_value=_make_assemble_result()
        ), patch.object(
            coord._agents["repair"], "run", side_effect=RuntimeError("unexpected")
        ):
            result = await coord.run(_make_debug_context())

        assert result["repair_plan"] is None
        assert len(result["agent_trace"]) == 1
        assert result["agent_trace"][0]["status"] == "failed"
        assert "unexpected" in result["agent_trace"][0]["error"]


class TestCoordinatorTraceStructure:
    """agent_trace 审计记录结构。"""

    @pytest.mark.asyncio
    async def test_trace_contains_duration(self):
        coord = Coordinator()

        fake_agent_result = AgentResult(
            agent_name="repair",
            status=AgentStatus.SUCCESS,
            output={"repair_plan": {"patch": "fix"}},
            started_at=10.0,
            finished_at=12.5,
        )

        with patch.object(
            coord._assembler, "assemble", return_value=_make_assemble_result()
        ), patch.object(
            coord._agents["repair"], "run", return_value=fake_agent_result
        ):
            result = await coord.run(_make_debug_context())

        trace = result["agent_trace"][0]
        assert "duration_s" in trace
        assert trace["duration_s"] == 2.5
        assert "usage" in trace

    @pytest.mark.asyncio
    async def test_trace_id_extracted_from_context(self):
        """trace_id 从 debug_context 的 request_id 提取。"""
        coord = Coordinator()
        ctx = {"request_id": "trace-abc-123", "exception": {}}

        fake_agent_result = AgentResult(
            agent_name="repair",
            status=AgentStatus.SUCCESS,
            output={"repair_plan": {"patch": "fix"}},
        )

        captured_ctx = {}

        async def fake_run(ctx: AgentContext):
            captured_ctx["trace_id"] = ctx.trace_id
            return fake_agent_result

        with patch.object(
            coord._assembler, "assemble", return_value=_make_assemble_result()
        ), patch.object(
            coord._agents["repair"], "run", side_effect=fake_run
        ):
            await coord.run(ctx)

        assert captured_ctx["trace_id"] == "trace-abc-123"


class TestCoordinatorAgentModeDispatch:
    """基于 AgentMode 枚举的调度分发验证。"""

    @pytest.mark.asyncio
    async def test_agent_mode_dag_dispatches_to_dag(self, monkeypatch):
        coord = Coordinator()
        # 显式固定 AgentMode.DAG，避免 .env 布尔开关污染全局单例 get_agent_mode() 派生结果
        monkeypatch.setattr(Settings, "get_agent_mode", lambda self: AgentMode.DAG)

        fake_dag_output = {
            "repair_plan": {"patch": "dag_fix"},
            "multi_agent_mode": True,
            "agent_trace": [],
        }

        with patch.object(
            coord._assembler, "assemble", return_value=_make_assemble_result()
        ), patch.object(coord, "_run_dag", return_value=fake_dag_output) as mock_dag:
            result = await coord.run(_make_debug_context())

        assert mock_dag.called
        assert result["repair_plan"]["patch"] == "dag_fix"
        assert result["multi_agent_mode"] is True

    @pytest.mark.asyncio
    async def test_agent_mode_single_dispatches_to_phase1(self, monkeypatch):
        coord = Coordinator()
        # 显式固定 AgentMode.SINGLE，避免 .env 布尔开关污染全局单例 get_agent_mode() 派生结果
        monkeypatch.setattr(Settings, "get_agent_mode", lambda self: AgentMode.SINGLE)

        fake_p1_output = {
            "repair_plan": {"patch": "single_fix"},
            "multi_agent_mode": False,
            "agent_trace": [],
        }

        with patch.object(
            coord._assembler, "assemble", return_value=_make_assemble_result()
        ), patch.object(
            coord, "_run_phase1", return_value=fake_p1_output
        ) as mock_p1:
            result = await coord.run(_make_debug_context())

        assert mock_p1.called
        assert result["repair_plan"]["patch"] == "single_fix"


    @pytest.mark.asyncio
    async def test_coordinator_output_contains_quality_and_experience(self):
        coord = Coordinator()
        assemble_result = _make_assemble_result()
        assemble_result["quality_report"] = {"overall_score": 0.9}
        assemble_result["debug_experience"] = [{"fingerprint": "fp1"}]

        fake_agent_result = AgentResult(
            agent_name="repair",
            status=AgentStatus.SUCCESS,
            output={"repair_plan": {"patch": "fix"}},
        )

        with patch.object(
            coord._assembler, "assemble", return_value=assemble_result
        ), patch.object(
            coord._agents["repair"], "run", return_value=fake_agent_result
        ):
            result = await coord.run(_make_debug_context())

        assert result["quality_report"] == {"overall_score": 0.9}
        assert result["debug_experience"] == [{"fingerprint": "fp1"}]
