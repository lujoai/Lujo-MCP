"""单元测试：DAG 拓扑（dag.py）与 Coordinator Phase 2 多 Agent DAG 调度。

覆盖：
- dag.py 节点注册表与拓扑定义
- Coordinator Phase 1 兼容路径（agent_multi_agent_enabled=False）
- Coordinator Phase 2 DAG（agent_multi_agent_enabled=True）：RepairAgent 先行 + 三 Agent 并行
- 静默降级：RepairAgent 失败 → 下游 SKIPPED + dag_degraded 信号
- 并行 Agent 失败独立降级
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.agent.base import (
    AgentResult,
    AgentStatus,
    BaseAgent,
)
from app.agent.coordinator import Coordinator
from app.agent.dag import (
    PHASE2_FIRST_NODES,
    PHASE2_PARALLEL_NODES,
    build_phase2_agents,
    get_phase2_agent_names,
)


class TestDagTopology:
    """dag.py 拓扑定义契约。"""

    def test_phase2_agent_names(self):
        names = get_phase2_agent_names()
        assert set(names) == {"repair", "git", "test", "security"}

    def test_phase2_first_nodes(self):
        assert PHASE2_FIRST_NODES == ["repair"]

    def test_phase2_parallel_nodes(self):
        assert set(PHASE2_PARALLEL_NODES) == {"git", "test", "security"}

    def test_build_phase2_agents_returns_instances(self):
        agents = build_phase2_agents()
        assert set(agents.keys()) == {"repair", "git", "test", "security"}
        # 每次调用返回新实例
        agents2 = build_phase2_agents()
        assert agents["repair"] is not agents2["repair"]

    def test_phase2_agents_all_baseagent(self):
        for agent in build_phase2_agents().values():
            assert isinstance(agent, BaseAgent)


class TestCoordinatorPhase1Compat:
    """agent_multi_agent_enabled=False 时走 Phase 1 单 Agent 串行。"""

    @pytest.mark.asyncio
    async def test_phase1_returns_multi_agent_mode_false(self, monkeypatch):
        # 隔离本机 .env 泄露到全局单例 settings 的 AGENT_* 布尔开关：
        # 仅覆盖 agent_multi_agent_enabled 不够——.env 里 AGENT_VERIFY_LOOP_ENABLED=true
        # 会让 get_agent_mode() 派生出 VERIFY_LOOP，从而误走 DAG/verify_loop 分支。
        monkeypatch.setattr("app.config.settings.agent_mode", "off")
        monkeypatch.setattr("app.config.settings.agent_enabled", False)
        monkeypatch.setattr("app.config.settings.agent_multi_agent_enabled", False)
        monkeypatch.setattr("app.config.settings.agent_verify_loop_enabled", False)
        monkeypatch.setattr("app.config.settings.agent_iterative_repair_enabled", False)

        coord = Coordinator()
        # mock assembler + repair agent
        with patch.object(
            coord._assembler, "assemble", new=AsyncMock(return_value={"sources": {}})
        ), patch.object(
            coord._agents["repair"], "run", new=AsyncMock(
                return_value=AgentResult(
                    agent_name="repair",
                    status=AgentStatus.SUCCESS,
                    output={"repair_plan": {"patch": "fix"}},
                    started_at=0.0,
                    finished_at=1.0,
                )
            )
        ):
            result = await coord.run({"request_id": "r1"})

        assert result["multi_agent_mode"] is False
        assert result["repair_plan"] == {"patch": "fix"}
        assert "git_attribution" not in result
        assert len(result["agent_trace"]) == 1


class TestCoordinatorPhase2Dag:
    """agent_multi_agent_enabled=True 时走 Phase 2 多 Agent DAG。"""

    @pytest.mark.asyncio
    async def test_phase2_full_success(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.agent_multi_agent_enabled", True)

        coord = Coordinator()
        with patch.object(
            coord._assembler, "assemble", new=AsyncMock(
                return_value={"sources": {}, "git_context": [{"file": "app/x.py"}]}
            )
        ), patch.object(
            coord._phase2_agents["repair"], "run", new=AsyncMock(
                return_value=AgentResult(
                    agent_name="repair", status=AgentStatus.SUCCESS,
                    output={"repair_plan": {"patch": "fix"}},
                    started_at=0.0, finished_at=1.0,
                )
            )
        ), patch.object(
            coord._phase2_agents["git"], "run", new=AsyncMock(
                return_value=AgentResult(
                    agent_name="git", status=AgentStatus.SUCCESS,
                    output={"suspect_commits": [], "attribution": "ok"},
                    started_at=0.0, finished_at=1.0,
                )
            )
        ), patch.object(
            coord._phase2_agents["test"], "run", new=AsyncMock(
                return_value=AgentResult(
                    agent_name="test", status=AgentStatus.SUCCESS,
                    output={"test_plan": {"test_files": ["t.py"]}},
                    started_at=0.0, finished_at=1.0,
                )
            )
        ), patch.object(
            coord._phase2_agents["security"], "run", new=AsyncMock(
                return_value=AgentResult(
                    agent_name="security", status=AgentStatus.SUCCESS,
                    output={"security_review": {"overall_severity": "none"}},
                    started_at=0.0, finished_at=1.0,
                )
            )
        ):
            result = await coord.run({"request_id": "r1"})

        assert result["multi_agent_mode"] is True
        assert result["repair_plan"] == {"patch": "fix"}
        assert result["git_attribution"]["attribution"] == "ok"
        assert result["test_plan"] == {"test_files": ["t.py"]}
        assert result["security_review"]["overall_severity"] == "none"
        # 4 个 trace：repair + git + test + security
        assert len(result["agent_trace"]) == 4
        assert result["dag_degraded"] is False

    @pytest.mark.asyncio
    async def test_phase2_repair_failed_skips_dependents(self, monkeypatch):
        """RepairAgent 失败 → 下游 Test/Security SKIPPED；Git 仍执行（不依赖 repair_plan）。"""
        monkeypatch.setattr("app.config.settings.agent_multi_agent_enabled", True)

        coord = Coordinator()
        with patch.object(
            coord._assembler, "assemble", new=AsyncMock(return_value={"sources": {}})
        ), patch.object(
            coord._phase2_agents["repair"], "run", new=AsyncMock(
                return_value=AgentResult(
                    agent_name="repair", status=AgentStatus.FAILED,
                    output={}, error="LLM down",
                    started_at=0.0, finished_at=1.0,
                )
            )
        ), patch.object(
            coord._phase2_agents["git"], "run", new=AsyncMock(
                return_value=AgentResult(
                    agent_name="git", status=AgentStatus.SUCCESS,
                    output={"attribution": "ok"},
                    started_at=0.0, finished_at=1.0,
                )
            )
        ), patch.object(
            coord._phase2_agents["test"], "run", new=AsyncMock(
                return_value=AgentResult(
                    agent_name="test", status=AgentStatus.SKIPPED,
                    output={}, error="repair_plan unavailable",
                    started_at=0.0, finished_at=1.0,
                )
            )
        ), patch.object(
            coord._phase2_agents["security"], "run", new=AsyncMock(
                return_value=AgentResult(
                    agent_name="security", status=AgentStatus.SKIPPED,
                    output={}, error="repair_plan unavailable",
                    started_at=0.0, finished_at=1.0,
                )
            )
        ):
            result = await coord.run({"request_id": "r1"})

        assert result["repair_plan"] is None
        assert result["test_plan"] is None
        assert result["security_review"] is None
        # git 仍执行（不依赖 repair_plan）
        assert result["git_attribution"]["attribution"] == "ok"
        # FIX: P1-9g repair 层失败也计入 degraded（此前 repair 失败不计入，
        # 整个 DAG 失效却被报告健康）—— 本场景 repair 失败 → degraded=True
        assert result["dag_degraded"] is True

    @pytest.mark.asyncio
    async def test_phase2_parallel_failure_triggers_dag_degraded(self, monkeypatch):
        """并行节点失败数 >= 阈值（默认 2）时 dag_degraded=True。"""
        monkeypatch.setattr("app.config.settings.agent_multi_agent_enabled", True)
        monkeypatch.setattr("app.config.settings.agent_dag_failure_threshold", 2)

        coord = Coordinator()
        with patch.object(
            coord._assembler, "assemble", new=AsyncMock(return_value={"sources": {}})
        ), patch.object(
            coord._phase2_agents["repair"], "run", new=AsyncMock(
                return_value=AgentResult(
                    agent_name="repair", status=AgentStatus.SUCCESS,
                    output={"repair_plan": {"patch": "fix"}},
                    started_at=0.0, finished_at=1.0,
                )
            )
        ), patch.object(
            coord._phase2_agents["git"], "run", new=AsyncMock(
                return_value=AgentResult(
                    agent_name="git", status=AgentStatus.FAILED,
                    output={}, error="git timeout",
                    started_at=0.0, finished_at=1.0,
                )
            )
        ), patch.object(
            coord._phase2_agents["test"], "run", new=AsyncMock(
                return_value=AgentResult(
                    agent_name="test", status=AgentStatus.FAILED,
                    output={}, error="LLM down",
                    started_at=0.0, finished_at=1.0,
                )
            )
        ), patch.object(
            coord._phase2_agents["security"], "run", new=AsyncMock(
                return_value=AgentResult(
                    agent_name="security", status=AgentStatus.SUCCESS,
                    output={"security_review": {"overall_severity": "none"}},
                    started_at=0.0, finished_at=1.0,
                )
            )
        ):
            result = await coord.run({"request_id": "r1"})

        # 2 个 FAILED >= threshold 2
        assert result["dag_degraded"] is True
        # 但仍返回成功聚合的结果（静默降级，不阻断）
        assert result["repair_plan"] == {"patch": "fix"}
        assert result["security_review"]["overall_severity"] == "none"

    @pytest.mark.asyncio
    async def test_phase2_parallel_unexpected_exception_caught(self, monkeypatch):
        """并行 Agent 抛异常时 Coordinator 防御性兜底，转为 FAILED。"""
        monkeypatch.setattr("app.config.settings.agent_multi_agent_enabled", True)

        coord = Coordinator()
        with patch.object(
            coord._assembler, "assemble", new=AsyncMock(return_value={"sources": {}})
        ), patch.object(
            coord._phase2_agents["repair"], "run", new=AsyncMock(
                return_value=AgentResult(
                    agent_name="repair", status=AgentStatus.SUCCESS,
                    output={"repair_plan": {"patch": "fix"}},
                    started_at=0.0, finished_at=1.0,
                )
            )
        ), patch.object(
            coord._phase2_agents["git"], "run", new=AsyncMock(
                side_effect=RuntimeError("unexpected boom")
            )
        ), patch.object(
            coord._phase2_agents["test"], "run", new=AsyncMock(
                return_value=AgentResult(
                    agent_name="test", status=AgentStatus.SUCCESS,
                    output={"test_plan": {}},
                    started_at=0.0, finished_at=1.0,
                )
            )
        ), patch.object(
            coord._phase2_agents["security"], "run", new=AsyncMock(
                return_value=AgentResult(
                    agent_name="security", status=AgentStatus.SUCCESS,
                    output={"security_review": {}},
                    started_at=0.0, finished_at=1.0,
                )
            )
        ):
            result = await coord.run({"request_id": "r1"})

        # git 异常被兜底为 FAILED，其他 Agent 仍正常
        git_trace = next(t for t in result["agent_trace"] if t["agent_name"] == "git")
        assert git_trace["status"] == "failed"
        assert "unexpected boom" in (git_trace["error"] or "")
        assert result["test_plan"] == {}
        assert result["security_review"] == {}
