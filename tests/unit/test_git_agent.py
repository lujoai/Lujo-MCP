"""单元测试：GitAgent（git_agent.py）。

覆盖：无堆栈帧 SKIPPED、preassembled git_context 复用、回退 get_recent_diff、
归因摘要构建、失败静默降级。
"""

from unittest.mock import patch

import pytest

from app.agent.base import AgentContext, AgentStatus
from app.agent.git_agent import GitAgent, _MAX_RECENT_CHANGE_CHARS


def _ctx(frames=None, repair_context=None):
    return AgentContext(
        debug_context={
            "exception": {"frames": frames or []},
        },
        repair_context=repair_context or {},
    )


class TestGitAgentSkipped:
    """无堆栈帧时返回 SKIPPED（非 FAILED）。"""

    @pytest.mark.asyncio
    async def test_no_frames_returns_skipped(self):
        agent = GitAgent()
        result = await agent.run(_ctx(frames=[]))
        assert result.status == AgentStatus.SKIPPED
        assert "no stack frames" in (result.error or "")
        assert result.output == {}

    @pytest.mark.asyncio
    async def test_no_exception_key_returns_skipped(self):
        agent = GitAgent()
        ctx = AgentContext(debug_context={}, repair_context={})
        result = await agent.run(ctx)
        assert result.status == AgentStatus.SKIPPED


class TestGitAgentPreassembled:
    """优先复用 repair_context.git_context，避免重复 git 调用。"""

    @pytest.mark.asyncio
    async def test_preassembled_git_context_reused(self):
        frames = [{"file": "app/foo.py", "line": 42}]
        preassembled = [{"file": "app/foo.py", "diff": "abc123 commit"}]
        agent = GitAgent()
        result = await agent.run(
            _ctx(frames=frames, repair_context={"git_context": preassembled})
        )
        assert result.status == AgentStatus.SUCCESS
        assert result.output["recent_changes"] == preassembled
        # suspect_commits 由 _map_preassembled_to_frames 派生
        assert len(result.output["suspect_commits"]) == 1
        assert result.output["suspect_commits"][0]["file"] == "app/foo.py"

    @pytest.mark.asyncio
    async def test_preassembled_file_not_in_frames(self):
        """preassembled 中的文件不在堆栈帧时，不映射到 suspect_commits。"""
        frames = [{"file": "app/foo.py", "line": 42}]
        preassembled = [{"file": "app/other.py", "diff": "abc123"}]
        agent = GitAgent()
        result = await agent.run(
            _ctx(frames=frames, repair_context={"git_context": preassembled})
        )
        assert result.status == AgentStatus.SUCCESS
        # recent_changes 仍包含 preassembled（用于摘要）
        assert result.output["recent_changes"] == preassembled
        # suspect_commits 为空（文件不匹配）
        assert result.output["suspect_commits"] == []


class TestGitAgentFallback:
    """preassembled 缺失时回退 get_recent_diff。"""

    @pytest.mark.asyncio
    async def test_fallback_to_get_recent_diff(self):
        frames = [{"file": "app/foo.py", "line": 42}]
        agent = GitAgent()

        with patch(
            "app.mcp.core.git.get_recent_diff",
            return_value={"file": "app/foo.py", "commit": "deadbeef"},
        ):
            result = await agent.run(_ctx(frames=frames, repair_context={}))

        assert result.status == AgentStatus.SUCCESS
        assert len(result.output["suspect_commits"]) == 1
        assert result.output["suspect_commits"][0]["file"] == "app/foo.py"

    @pytest.mark.asyncio
    async def test_fallback_get_recent_diff_failure_silent(self):
        """get_recent_diff 抛异常时静默降级为空贡献，不阻断。"""
        frames = [{"file": "app/foo.py", "line": 42}]
        agent = GitAgent()

        with patch(
            "app.mcp.core.git.get_recent_diff", side_effect=RuntimeError("boom")
        ):
            result = await agent.run(_ctx(frames=frames, repair_context={}))

        assert result.status == AgentStatus.SUCCESS
        assert result.output["suspect_commits"] == []
        assert "无近期 git 改动" in result.output["attribution"]


class TestGitAgentAttributionSummary:
    """归因摘要构建。"""

    @pytest.mark.asyncio
    async def test_empty_summary(self):
        agent = GitAgent()
        summary = agent._build_attribution_summary([], [])
        assert "无近期 git 改动" in summary

    @pytest.mark.asyncio
    async def test_summary_with_commits(self):
        agent = GitAgent()
        suspects = [{"file": "app/a.py"}, {"file": "app/b.py"}]
        summary = agent._build_attribution_summary(suspects, [])
        assert "嫌疑文件 2 个" in summary
        assert "app/a.py" in summary and "app/b.py" in summary

    @pytest.mark.asyncio
    async def test_summary_truncated(self):
        agent = GitAgent()
        suspects = [{"file": f"app/file_{i}.py"} for i in range(200)]
        summary = agent._build_attribution_summary(suspects, [])
        assert len(summary) <= _MAX_RECENT_CHANGE_CHARS


class TestGitAgentFailure:
    """GitAgent 内部异常返回 FAILED。"""

    @pytest.mark.asyncio
    async def test_unexpected_exception_returns_failed(self):
        agent = GitAgent()
        # 通过 patch _safe_blame_frames 抛异常模拟内部失败
        with patch.object(
            GitAgent, "_safe_blame_frames", side_effect=RuntimeError("unexpected")
        ):
            frames = [{"file": "app/foo.py", "line": 1}]
            result = await agent.run(_ctx(frames=frames))
        assert result.status == AgentStatus.FAILED
        assert "unexpected" in (result.error or "")
