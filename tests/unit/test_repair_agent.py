"""单元测试：RepairAgent（repair_agent.py）。

覆盖：_validate_repair_plan 容错 JSON、LLM mock + 重试、AgentResult 状态映射。
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.agent.base import AgentContext, AgentStatus
from app.agent.repair_agent import (
    RepairAgent,
    _validate_repair_plan,
    SYSTEM_PROMPT,
)
from app.agent.utils import extract_json


class TestSystemPrompt:
    """SYSTEM_PROMPT 契约。"""

    def test_prompt_contains_required_fields(self):
        """SYSTEM_PROMPT 必须声明所有必填字段。"""
        for field in ("patch", "affected_files", "validation_strategy", "risk_assessment", "confidence"):
            assert field in SYSTEM_PROMPT, f"SYSTEM_PROMPT 缺少字段 {field}"

    def test_prompt_requires_json_output(self):
        assert "JSON" in SYSTEM_PROMPT


class TestExtractJson:
    """extract_json 容错提取。"""

    def test_plain_json(self):
        text = '{"patch": "fix"}'
        assert extract_json(text) == '{"patch": "fix"}'

    def test_markdown_code_block(self):
        text = '```json\n{"patch": "fix"}\n```'
        result = extract_json(text)
        assert result is not None
        assert json.loads(result) == {"patch": "fix"}

    def test_markdown_without_lang(self):
        text = '```\n{"patch": "fix"}\n```'
        result = extract_json(text)
        assert result is not None

    def test_embedded_in_text(self):
        text = 'Here is the plan:\n{"patch": "fix"}\nDone.'
        result = extract_json(text)
        assert result is not None
        assert "patch" in result

    def test_no_json_returns_none(self):
        assert extract_json("no json here") is None


class TestValidateRepairPlan:
    """_validate_repair_plan schema 校验与降级。"""

    def test_valid_complete_output(self):
        raw = json.dumps({
            "patch": "modify line 42",
            "affected_files": ["app/foo.py", "app/bar.py"],
            "validation_strategy": "run pytest tests/",
            "risk_assessment": "low risk",
            "confidence": "high",
            "rationale": "root cause is null deref",
        })
        result = _validate_repair_plan(raw)
        assert result["patch"] == "modify line 42"
        assert result["affected_files"] == ["app/foo.py", "app/bar.py"]
        assert result["confidence"] == "high"
        assert result["rationale"] == "root cause is null deref"

    def test_markdown_code_block_input(self):
        raw = '```json\n{"patch": "fix", "affected_files": [], "validation_strategy": "v", "risk_assessment": "r", "confidence": "medium"}\n```'
        result = _validate_repair_plan(raw)
        assert result["patch"] == "fix"
        assert result["confidence"] == "medium"

    def test_missing_fields_default_empty(self):
        raw = json.dumps({"patch": "fix"})
        result = _validate_repair_plan(raw)
        assert result["patch"] == "fix"
        assert result["affected_files"] == []
        assert result["validation_strategy"] == ""
        assert result["risk_assessment"] == ""

    def test_invalid_confidence_defaults_low(self):
        raw = json.dumps({
            "patch": "fix",
            "affected_files": [],
            "validation_strategy": "v",
            "risk_assessment": "r",
            "confidence": "invalid_value",
        })
        result = _validate_repair_plan(raw)
        assert result["confidence"] == "low"

    def test_missing_confidence_defaults_low(self):
        raw = json.dumps({
            "patch": "fix",
            "affected_files": [],
            "validation_strategy": "v",
            "risk_assessment": "r",
        })
        result = _validate_repair_plan(raw)
        assert result["confidence"] == "low"

    def test_unparseable_returns_fallback(self):
        """完全无法解析 → 返回带 raw_truncated 的 fallback。"""
        result = _validate_repair_plan("not json at all")
        assert result["patch"] == ""
        assert result["affected_files"] == []
        assert "raw_truncated" in result

    def test_affected_files_must_be_list(self):
        """affected_files 非数组 → 归一化为空数组。"""
        raw = json.dumps({
            "patch": "fix",
            "affected_files": "not a list",
            "validation_strategy": "v",
            "risk_assessment": "r",
        })
        result = _validate_repair_plan(raw)
        assert result["affected_files"] == []

    def test_field_truncation(self):
        """超长字段被截断。"""
        long_patch = "x" * 10000
        raw = json.dumps({
            "patch": long_patch,
            "affected_files": [],
            "validation_strategy": "v",
            "risk_assessment": "r",
        })
        result = _validate_repair_plan(raw)
        assert len(result["patch"]) <= 4000


def _make_ctx():
    """构造 AgentContext。"""
    return AgentContext(
        debug_context={"request_id": "r1", "exception": {"type": "ValueError"}},
        repair_context={
            "debug_context": {"request_id": "r1"},
            "prior_analysis": None,
            "vector_recall": [],
            "git_context": [],
        },
    )


def _make_chat_response(content: str):
    """构造 AsyncOpenAI chat.completions.create 的 fake 返回。"""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.model = "gpt-4o"
    response.usage = MagicMock()
    response.usage.prompt_tokens = 100
    response.usage.completion_tokens = 50
    response.usage.total_tokens = 150
    return response


class TestRepairAgentRun:
    """RepairAgent.run 成功/失败路径。"""

    @pytest.mark.asyncio
    async def test_run_success(self):
        agent = RepairAgent()
        fake_response = _make_chat_response(json.dumps({
            "patch": "modify line 42",
            "affected_files": ["app/foo.py"],
            "validation_strategy": "run pytest",
            "risk_assessment": "low",
            "confidence": "high",
            "rationale": "null deref",
        }))

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_response)

        with patch("app.llm.clients._get_async_client", return_value=mock_client):
            result = await agent.run(_make_ctx())

        assert result.status == AgentStatus.SUCCESS
        assert result.agent_name == "repair"
        assert result.output["repair_plan"]["patch"] == "modify line 42"
        assert result.output["repair_plan"]["confidence"] == "high"
        assert result.usage["total_tokens"] == 150
        assert result.error is None

    @pytest.mark.asyncio
    async def test_run_llm_failure_returns_failed(self):
        agent = RepairAgent()

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(
            side_effect=RuntimeError("LLM unavailable")
        )

        with patch("app.llm.clients._get_async_client", return_value=mock_client):
            result = await agent.run(_make_ctx())

        assert result.status == AgentStatus.FAILED
        assert "LLM unavailable" in result.error
        assert result.output == {}

    @pytest.mark.asyncio
    async def test_run_uses_agent_model_when_set(self, monkeypatch):
        """ctx.model 优先于 settings.agent_model 优先于 settings.llm_model。"""
        monkeypatch.setattr("app.config.settings.agent_model", "")
        monkeypatch.setattr("app.config.settings.llm_model", "default-model")

        agent = RepairAgent()
        captured_model = {}

        async def fake_create(**kwargs):
            captured_model["model"] = kwargs.get("model")
            return _make_chat_response(json.dumps({
                "patch": "fix", "affected_files": [],
                "validation_strategy": "v", "risk_assessment": "r",
            }))

        mock_client = AsyncMock()
        mock_client.chat.completions.create = fake_create

        ctx = AgentContext(
            debug_context={},
            repair_context={"debug_context": {}, "prior_analysis": None, "vector_recall": [], "git_context": []},
            model="custom-model",
        )

        with patch("app.llm.clients._get_async_client", return_value=mock_client):
            await agent.run(ctx)

        assert captured_model["model"] == "custom-model"

    @pytest.mark.asyncio
    async def test_run_falls_back_to_llm_model(self, monkeypatch):
        """agent_model 为空时回退到 llm_model。"""
        monkeypatch.setattr("app.config.settings.agent_model", "")
        monkeypatch.setattr("app.config.settings.llm_model", "fallback-model")

        agent = RepairAgent()
        captured_model = {}

        async def fake_create(**kwargs):
            captured_model["model"] = kwargs.get("model")
            return _make_chat_response(json.dumps({
                "patch": "fix", "affected_files": [],
                "validation_strategy": "v", "risk_assessment": "r",
            }))

        mock_client = AsyncMock()
        mock_client.chat.completions.create = fake_create

        with patch("app.llm.clients._get_async_client", return_value=mock_client):
            await agent.run(_make_ctx())

        assert captured_model["model"] == "fallback-model"


class TestRepairAgentBuildMessages:
    """_build_messages 消息构建。"""

    def test_messages_structure(self):
        agent = RepairAgent()
        ctx = _make_ctx()
        messages = agent._build_messages(ctx)

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == SYSTEM_PROMPT
        assert messages[1]["role"] == "user"
        # user content 应包含 debug_context
        assert "request_id" in messages[1]["content"]
