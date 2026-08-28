"""单元测试：SecurityAgent（security_agent.py）。

覆盖：repair_plan 缺失 SKIPPED、_validate_security_review 容错（risks 数组校验、
severity/category 合法化）、LLM mock + 失败降级。
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.base import AgentContext, AgentStatus
from app.agent.security_agent import (
    SYSTEM_PROMPT,
    SecurityAgent,
    _validate_security_review,
)
from app.agent.utils import extract_json


def _ctx(repair_plan=None):
    return AgentContext(
        debug_context={"exception": {"type": "ValueError"}},
        repair_context={"repair_plan": repair_plan} if repair_plan else {},
    )


class TestSystemPrompt:
    def test_prompt_contains_security_focus(self):
        for kw in ("LFI", "SSRF", "SQL", "注入"):
            assert kw in SYSTEM_PROMPT

    def test_prompt_requires_json(self):
        assert "JSON" in SYSTEM_PROMPT


class TestExtractJson:
    def test_plain_json(self):
        assert extract_json('{"a": 1}') == '{"a": 1}'

    def test_markdown_block(self):
        text = '```json\n{"a": 1}\n```'
        result = extract_json(text)
        assert result is not None

    def test_no_json(self):
        assert extract_json("nothing") is None


class TestValidateSecurityReview:
    def test_valid_full(self):
        raw = json.dumps({
            "risks": [
                {"category": "LFI", "severity": "high", "description": "任意文件读", "location": "line 10"}
            ],
            "recommendations": ["加路径白名单"],
            "overall_severity": "high",
            "summary": "存在高危",
        })
        result = _validate_security_review(raw)
        assert len(result["risks"]) == 1
        assert result["risks"][0]["category"] == "LFI"
        assert result["risks"][0]["severity"] == "high"
        assert result["recommendations"] == ["加路径白名单"]
        assert result["overall_severity"] == "high"

    def test_no_risks_overall_none(self):
        raw = json.dumps({"risks": [], "overall_severity": "none"})
        result = _validate_security_review(raw)
        assert result["risks"] == []
        assert result["overall_severity"] == "none"

    def test_invalid_category_normalized(self):
        raw = json.dumps({"risks": [{"category": "UnknownCat", "severity": "high"}]})
        result = _validate_security_review(raw)
        assert result["risks"][0]["category"] == "Other"

    def test_invalid_severity_normalized_fail_safe(self):
        """R7-S1 回归：非法 severity 不得静默降级为 low（fail-open）。

        旧实现把 "critical"/"High" 一律降为 "low"，verify_loop 安全门的
        ("critical", "high") 分支对真实 LLM 输出不可达。现 fail-safe 归 high。
        """
        raw = json.dumps({"risks": [{"category": "LFI", "severity": "critical"}]})
        result = _validate_security_review(raw)
        assert result["risks"][0]["severity"] == "high"

    def test_severity_case_variant_normalized(self):
        """大小写变体 "High" 归一化为 "high"，不得降级。"""
        raw = json.dumps({"risks": [{"category": "LFI", "severity": "High"}]})
        result = _validate_security_review(raw)
        assert result["risks"][0]["severity"] == "high"

    def test_unknown_severity_fails_safe_to_high(self):
        """无法识别的 severity 保守按 high 处理，交由安全门钳制。"""
        raw = json.dumps({"risks": [{"category": "LFI", "severity": "severe"}]})
        result = _validate_security_review(raw)
        assert result["risks"][0]["severity"] == "high"

    def test_valid_severity_unchanged(self):
        raw = json.dumps({"risks": [{"category": "LFI", "severity": "medium"}]})
        result = _validate_security_review(raw)
        assert result["risks"][0]["severity"] == "medium"

    def test_invalid_overall_severity(self):
        raw = json.dumps({"overall_severity": "critical"})
        result = _validate_security_review(raw)
        # critical 不在 VALID_SEVERITY，归一化为 unknown
        assert result["overall_severity"] == "unknown"

    def test_missing_fields_default(self):
        result = _validate_security_review("{}")
        assert result["risks"] == []
        assert result["recommendations"] == []
        assert result["summary"] == ""

    def test_invalid_json_returns_raw_truncated(self):
        result = _validate_security_review("not json")
        assert "raw_truncated" in result
        assert result["risks"] == []

    def test_risks_truncation(self):
        raw = json.dumps({"risks": [{"category": "LFI", "severity": "low"} for _ in range(50)]})
        result = _validate_security_review(raw)
        # MAX_RISKS = 20
        assert len(result["risks"]) <= 20

    def test_non_dict_risk_skipped(self):
        raw = json.dumps({"risks": ["not a dict", {"category": "LFI", "severity": "low"}]})
        result = _validate_security_review(raw)
        assert len(result["risks"]) == 1
        assert result["risks"][0]["category"] == "LFI"


class TestSecurityAgentSkipped:
    @pytest.mark.asyncio
    async def test_no_repair_plan_returns_skipped(self):
        agent = SecurityAgent()
        result = await agent.run(_ctx(repair_plan=None))
        assert result.status == AgentStatus.SKIPPED
        assert "repair_plan unavailable" in (result.error or "")


class TestSecurityAgentLLM:
    @pytest.mark.asyncio
    async def test_success(self):
        agent = SecurityAgent()
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content='{"risks": [{"category": "LFI", "severity": "high"}], "overall_severity": "high", "recommendations": ["fix"], "summary": "bad"}'))]
        mock_resp.model = "gpt-4o"
        mock_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)

        with patch("app.llm.clients._get_async_client", return_value=mock_client):
            result = await agent.run(_ctx(repair_plan={"patch": "fix"}))

        assert result.status == AgentStatus.SUCCESS
        assert result.output["security_review"]["overall_severity"] == "high"
        assert result.usage["total_tokens"] == 15

    @pytest.mark.asyncio
    async def test_llm_failure_returns_failed(self):
        agent = SecurityAgent()
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            side_effect=RuntimeError("LLM down")
        )

        with patch("app.llm.clients._get_async_client", return_value=mock_client):
            result = await agent.run(_ctx(repair_plan={"patch": "fix"}))

        assert result.status == AgentStatus.FAILED
        assert "LLM down" in (result.error or "")
