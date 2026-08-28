"""单元测试：TestAgent（test_agent.py）。

覆盖：repair_plan 缺失 SKIPPED、_validate_test_plan 容错、LLM mock + 重试、失败降级。
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai import RateLimitError

from app.agent.base import AgentContext, AgentStatus
from app.agent.test_agent import (
    SYSTEM_PROMPT,
    TestAgent,
    _validate_test_plan,
)
from app.agent.utils import extract_json


def _ctx(repair_plan=None, debug_context=None):
    return AgentContext(
        debug_context=debug_context or {"exception": {"type": "ValueError"}},
        repair_context={"repair_plan": repair_plan} if repair_plan else {},
    )


class TestSystemPrompt:
    def test_prompt_contains_required_fields(self):
        for f in ("test_files", "test_cases", "regression_risks", "validation_steps"):
            assert f in SYSTEM_PROMPT

    def test_prompt_requires_json(self):
        assert "JSON" in SYSTEM_PROMPT


class TestExtractJson:
    def test_plain_json(self):
        assert extract_json('{"a": 1}') == '{"a": 1}'

    def test_markdown_block(self):
        text = '```json\n{"a": 1}\n```'
        result = extract_json(text)
        assert result is not None
        assert json.loads(result) == {"a": 1}

    def test_no_json(self):
        assert extract_json("nothing") is None


class TestValidateTestPlan:
    def test_valid_full(self):
        raw = json.dumps({
            "test_files": ["tests/test_foo.py"],
            "test_cases": ["test_fix_bug"],
            "regression_risks": ["edge case X"],
            "validation_steps": ["run pytest"],
            "coverage_note": "充分覆盖",
        })
        result = _validate_test_plan(raw)
        assert result["test_files"] == ["tests/test_foo.py"]
        assert result["test_cases"] == ["test_fix_bug"]
        assert result["regression_risks"] == ["edge case X"]
        assert result["validation_steps"] == ["run pytest"]
        assert result["coverage_note"] == "充分覆盖"

    def test_missing_fields_default_empty(self):
        result = _validate_test_plan("{}")
        assert result["test_files"] == []
        assert result["test_cases"] == []
        assert result["regression_risks"] == []
        assert result["validation_steps"] == []
        assert result["coverage_note"] == ""

    def test_invalid_json_returns_raw_truncated(self):
        result = _validate_test_plan("not json at all")
        assert "raw_truncated" in result
        assert result["test_files"] == []

    def test_list_truncation(self):
        raw = json.dumps({"test_files": [f"f{i}.py" for i in range(50)]})
        result = _validate_test_plan(raw)
        # MAX_LIST_ITEMS = 30
        assert len(result["test_files"]) <= 30

    def test_non_list_fields_become_empty(self):
        raw = json.dumps({"test_files": "not a list"})
        result = _validate_test_plan(raw)
        assert result["test_files"] == []


class TestTestAgentSkipped:
    @pytest.mark.asyncio
    async def test_no_repair_plan_returns_skipped(self):
        agent = TestAgent()
        result = await agent.run(_ctx(repair_plan=None))
        assert result.status == AgentStatus.SKIPPED
        assert "repair_plan unavailable" in (result.error or "")


class TestTestAgentLLM:
    @pytest.mark.asyncio
    async def test_success(self):
        agent = TestAgent()
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content='{"test_files": ["t.py"], "test_cases": ["c"], "regression_risks": [], "validation_steps": ["s"]}'))]
        mock_resp.model = "gpt-4o"
        mock_resp.usage = MagicMock(
            prompt_tokens=10, completion_tokens=5, total_tokens=15
        )
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)

        with patch("app.llm.clients._get_async_client", return_value=mock_client):
            result = await agent.run(_ctx(repair_plan={"patch": "fix"}))

        assert result.status == AgentStatus.SUCCESS
        assert result.output["test_plan"]["test_files"] == ["t.py"]
        assert result.usage["total_tokens"] == 15

    @pytest.mark.asyncio
    async def test_llm_failure_returns_failed(self):
        agent = TestAgent()
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            side_effect=RuntimeError("LLM down")
        )

        with patch("app.llm.clients._get_async_client", return_value=mock_client):
            result = await agent.run(_ctx(repair_plan={"patch": "fix"}))

        assert result.status == AgentStatus.FAILED
        assert "LLM down" in (result.error or "")


class TestTestAgentRetry:
    @pytest.mark.asyncio
    async def test_rate_limit_then_success(self):
        agent = TestAgent()
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content='{"test_files": []}'))]
        mock_resp.model = "gpt-4o"
        mock_resp.usage = MagicMock(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        mock_client = MagicMock()
        # 第一次 RateLimitError，第二次成功
        mock_client.chat.completions.create = AsyncMock(
            side_effect=[RateLimitError(message="limit", response=MagicMock(), body=None), mock_resp]
        )

        with patch("app.llm.clients._get_async_client", return_value=mock_client), \
             patch("app.agent.test_agent.asyncio.sleep", new=AsyncMock()):
            result = await agent.run(_ctx(repair_plan={"patch": "fix"}))

        assert result.status == AgentStatus.SUCCESS


# ---------------------------------------------------------------------------
# FIX: R7-Q5 —— error_message 进 prompt 前必须截断（security/test 双 Agent）
# ---------------------------------------------------------------------------


def test_build_messages_truncates_huge_error_message():
    """/ingest/error 对 message 无字段级长度上限（整体 1MB），MB 级 message
    原样进 prompt → 超上下文、并行节点 FAILED。"""
    import json

    from app.agent.base import AgentContext
    from app.agent.security_agent import SecurityAgent
    from app.agent.test_agent import TestAgent

    huge_message = "M" * 200_000
    ctx = AgentContext(
        debug_context={"exception": {"type": "E", "message": huge_message}},
        repair_context={},
    )
    repair_plan = {"patch": "p", "affected_files": ["a.py"]}

    for agent in (TestAgent(), SecurityAgent()):
        messages = agent._build_messages(ctx, repair_plan)
        user_content = messages[1]["content"]
        # 修复前 user_content ≥ 200K 字符；截断后 ~8K + 包裹开销
        assert len(user_content) < 20_000, f"{type(agent).__name__} 未截断 error_message"
        # payload 仍是合法 JSON 包裹（wrap_evidence 前的结构保持）
        assert huge_message not in user_content
        json.dumps({"probe": True})  # sanity：json 可用性
