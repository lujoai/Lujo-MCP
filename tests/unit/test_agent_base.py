"""单元测试：AI Debug Agent 基础框架契约（base.py）。"""

import pytest

from app.agent.base import (
    AgentContext,
    AgentResult,
    AgentStatus,
    AgentTrace,
    BaseAgent,
)


class TestAgentStatus:
    """AgentStatus 枚举值与字符串契约。"""

    def test_status_values(self):
        assert AgentStatus.PENDING == "pending"
        assert AgentStatus.RUNNING == "running"
        assert AgentStatus.SUCCESS == "success"
        assert AgentStatus.FAILED == "failed"
        assert AgentStatus.SKIPPED == "skipped"

    def test_status_is_str_enum(self):
        """AgentStatus 继承 str，可直接序列化为 JSON。"""
        assert isinstance(AgentStatus.SUCCESS, str)
        assert AgentStatus.SUCCESS.value == "success"


class TestAgentContext:
    """AgentContext dataclass 字段约束。"""

    def test_default_fields(self):
        ctx = AgentContext(
            debug_context={"request_id": "r1"},
            repair_context={"sources": {}},
        )
        assert ctx.debug_context == {"request_id": "r1"}
        assert ctx.repair_context == {"sources": {}}
        assert ctx.model is None
        assert ctx.trace_id is None

    def test_with_optional_fields(self):
        ctx = AgentContext(
            debug_context={},
            repair_context={},
            model="gpt-4o",
            trace_id="trace-123",
        )
        assert ctx.model == "gpt-4o"
        assert ctx.trace_id == "trace-123"


class TestAgentResult:
    """AgentResult dataclass 字段约束。"""

    def test_default_usage(self):
        result = AgentResult(
            agent_name="repair",
            status=AgentStatus.SUCCESS,
            output={"repair_plan": {}},
        )
        assert result.agent_name == "repair"
        assert result.status == AgentStatus.SUCCESS
        assert result.error is None
        assert result.started_at == 0.0
        assert result.finished_at == 0.0
        assert result.usage == {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    def test_with_error(self):
        result = AgentResult(
            agent_name="repair",
            status=AgentStatus.FAILED,
            output={},
            error="LLM timeout",
        )
        assert result.status == AgentStatus.FAILED
        assert result.error == "LLM timeout"


class TestAgentTrace:
    """AgentTrace 审计记录序列化。"""

    def test_to_dict_success(self):
        trace = AgentTrace(
            agent_name="repair",
            status=AgentStatus.SUCCESS,
            duration_s=1.234,
            usage={"total_tokens": 100},
        )
        d = trace.to_dict()
        assert d["agent_name"] == "repair"
        assert d["status"] == "success"
        assert d["duration_s"] == 1.234
        assert d["error"] is None
        assert d["usage"] == {"total_tokens": 100}

    def test_to_dict_failed(self):
        trace = AgentTrace(
            agent_name="git",
            status=AgentStatus.FAILED,
            duration_s=0.5,
            error="git command timeout",
        )
        d = trace.to_dict()
        assert d["status"] == "failed"
        assert d["error"] == "git command timeout"


class TestBaseAgent:
    """BaseAgent 抽象基类契约。"""

    def test_cannot_instantiate_abstract(self):
        """BaseAgent 是抽象类，不能直接实例化。"""
        with pytest.raises(TypeError):
            BaseAgent()  # type: ignore[abstract]

    def test_subclass_must_implement_run(self):
        """子类必须实现 run 方法。"""

        class IncompleteAgent(BaseAgent):
            name = "incomplete"
            # 缺少 run 实现

        with pytest.raises(TypeError):
            IncompleteAgent()  # type: ignore[abstract]

    def test_subclass_with_run_works(self):
        """完整实现的子类可正常实例化。"""

        class FakeAgent(BaseAgent):
            name = "fake"

            async def run(self, ctx: AgentContext) -> AgentResult:
                return AgentResult(
                    agent_name=self.name,
                    status=AgentStatus.SUCCESS,
                    output={"fake": True},
                    started_at=0.0,
                    finished_at=1.0,
                )

        agent = FakeAgent()
        assert agent.name == "fake"

    def test_trace_helper(self):
        """_trace 静态方法从 AgentResult 派生 AgentTrace。"""
        result = AgentResult(
            agent_name="repair",
            status=AgentStatus.SUCCESS,
            output={},
            started_at=10.0,
            finished_at=12.5,
            usage={"total_tokens": 50},
        )
        trace = BaseAgent._trace(result)
        assert trace.agent_name == "repair"
        assert trace.status == AgentStatus.SUCCESS
        assert trace.duration_s == 2.5
        assert trace.usage == {"total_tokens": 50}


class TestCallLlmFallback:
    """_call_llm fallback 分支：成功返回结构不变，失败抛统一 RuntimeError（P3-3）。"""

    @staticmethod
    def _make_agent():
        class FakeAgent(BaseAgent):
            name = "fake"

            async def run(self, ctx: AgentContext) -> AgentResult:
                return AgentResult(
                    agent_name=self.name,
                    status=AgentStatus.SUCCESS,
                    output={},
                )

        return FakeAgent()

    @staticmethod
    def _chat_response(content: str):
        from unittest.mock import MagicMock

        response = MagicMock()
        choice = MagicMock()
        choice.message.content = content
        response.choices = [choice]
        response.usage = None
        return response

    @pytest.mark.asyncio
    async def test_fallback_success_keeps_return_shape(self):
        """主模型失败后 fallback 成功 → 返回结构不变（{"analysis": ..., "usage": ...}）。"""
        from unittest.mock import AsyncMock, MagicMock
        from openai import APIError

        agent = self._make_agent()
        client = MagicMock()
        client.chat.completions.create = AsyncMock(
            side_effect=[
                APIError("primary down", None, body=None),
                self._chat_response('{"root_cause": "x", "impact": "", "fix": ""}'),
            ]
        )

        result = await agent._call_llm(
            client=client,
            model="primary",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.3,
            max_retries=0,
            validate_fn=lambda content: {"root_cause": "x", "impact": "", "fix": ""},
            fallback_model="fallback",
        )

        assert result["analysis"]["root_cause"] == "x"
        assert result["usage"] == {}
        assert client.chat.completions.create.call_count == 2

    @pytest.mark.asyncio
    async def test_fallback_failure_raises_unified_runtime_error(self):
        """主模型与 fallback 均失败 → 抛统一 RuntimeError（聚合 last_error）。"""
        from unittest.mock import AsyncMock, MagicMock
        from openai import APIError

        agent = self._make_agent()
        client = MagicMock()
        client.chat.completions.create = AsyncMock(
            side_effect=APIError("fallback also down", None, body=None)
        )

        with pytest.raises(RuntimeError) as excinfo:
            await agent._call_llm(
                client=client,
                model="primary",
                messages=[{"role": "user", "content": "hi"}],
                temperature=0.3,
                max_retries=0,
                validate_fn=lambda content: {"root_cause": "x"},
                fallback_model="fallback",
            )

        assert "fake LLM 调用失败" in str(excinfo.value)
        assert "fallback also down" in str(excinfo.value)
        assert client.chat.completions.create.call_count == 2
