"""单元测试：LLM analyzer（mock OpenAI 响应）"""
import pytest
from unittest.mock import patch, MagicMock


class TestAnalyzer:

    def test_truncate_context_basic(self):
        from app.llm.analyzer import truncate_context

        ctx = {
            "request_id": "001",
            "flow": ["start", "error"],
            "input": {"data": "x" * 10000},
            "output": None,
            "errors": ["test error"],
        }
        result = truncate_context(ctx, max_tokens=10)
        assert result["request_id"] == "001"
        assert result.get("_truncated") is True

    def test_truncate_context_short(self):
        from app.llm.analyzer import truncate_context

        ctx = {
            "request_id": "002",
            "flow": ["start", "end"],
        }
        result = truncate_context(ctx, max_tokens=10000)
        assert result.get("_truncated") is not True

    def test_build_analysis_prompt(self):
        from app.llm.analyzer import build_analysis_prompt

        ctx = {
            "request_id": "003",
            "flow": ["request_start", "error"],
            "input": {"operation": "test"},
            "errors": ["something went wrong"],
        }
        prompt = build_analysis_prompt(ctx)
        assert "请求 ID: 003" in prompt
        assert "request_start" in prompt
        assert "error" in prompt
        assert "something went wrong" in prompt

    @patch("app.llm.analyzer._get_client")
    def test_analyze_with_mock(self, mock_get_client):
        from app.llm.analyzer import analyze, truncate_context
        import json

        mock_client = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps({
            "root_cause": "测试根因",
            "impact": "无影响",
            "fix": "无需修复",
            "confidence": "high",
        })
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.model = "gpt-4o-mock"
        mock_response.usage.prompt_tokens = 100
        mock_response.usage.completion_tokens = 50
        mock_response.usage.total_tokens = 150
        mock_client.chat.completions.create.return_value = mock_response
        mock_get_client.return_value = mock_client

        ctx = {
            "request_id": "004",
            "flow": ["request_start", "error"],
            "errors": ["test error"],
        }

        result = analyze(ctx, model="gpt-4o-mock")
        assert "analysis" in result
        assert result["analysis"]["root_cause"] == "测试根因"
        assert result["model"] == "gpt-4o-mock"
        assert result["usage"]["total_tokens"] == 150
        assert result["attempts"] == 1

    @patch("app.llm.analyzer._get_client")
    def test_analyze_redacts_sensitive_values_before_llm(self, mock_get_client):
        from app.llm.analyzer import analyze
        import json

        mock_client = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps({"root_cause": "ok"})
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.model = "gpt-4o-mock"
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5
        mock_response.usage.total_tokens = 15
        mock_client.chat.completions.create.return_value = mock_response
        mock_get_client.return_value = mock_client

        ctx = {
            "request_id": "005",
            "input": {"api_key": "sk-live-secret", "nested": {"token": "abc123"}},
            "exception": {
                "frames": [
                    {
                        "file": "x.py",
                        "line": 1,
                        "function": "boom",
                        "code": "raise",
                        "locals": {"password": "pw-123", "normal": 'api_key = "inline-secret"'},
                    }
                ]
            },
        }

        analyze(ctx, model="gpt-4o-mock")
        kwargs = mock_client.chat.completions.create.call_args.kwargs
        sent = kwargs["messages"][1]["content"]
        assert "sk-live-secret" not in sent
        assert "abc123" not in sent
        assert "pw-123" not in sent
        assert "inline-secret" not in sent
        assert "***REDACTED***" in sent
        assert '"api_key": "***REDACTED***"' in sent


class TestLLMProvider:

    def test_resolve_base_url_openai_default(self):
        """openai provider 默认无 base_url（用 OpenAI SDK 默认地址）"""
        from app.config import settings

        saved_provider = settings.llm_provider
        saved_base_url = settings.llm_base_url
        try:
            settings.llm_provider = "openai"
            settings.llm_base_url = ""
            from app.llm.analyzer import _resolve_base_url
            assert _resolve_base_url() == ""
        finally:
            settings.llm_provider = saved_provider
            settings.llm_base_url = saved_base_url

    def test_resolve_base_url_zhipu(self):
        """zhipu provider 自动使用智谱 API 地址"""
        from app.config import settings

        saved_provider = settings.llm_provider
        saved_base_url = settings.llm_base_url
        try:
            settings.llm_provider = "zhipu"
            settings.llm_base_url = ""
            from app.llm.analyzer import _resolve_base_url
            assert _resolve_base_url() == "https://open.bigmodel.cn/api/paas/v4/"
        finally:
            settings.llm_provider = saved_provider
            settings.llm_base_url = saved_base_url

    def test_resolve_base_url_custom_overrides_provider(self):
        """显式 llm_base_url 覆盖 provider 默认值"""
        from app.config import settings

        saved_provider = settings.llm_provider
        saved_base_url = settings.llm_base_url
        try:
            settings.llm_provider = "zhipu"
            settings.llm_base_url = "https://my-proxy.example.com/v1"
            from app.llm.analyzer import _resolve_base_url
            assert _resolve_base_url() == "https://my-proxy.example.com/v1"
        finally:
            settings.llm_provider = saved_provider
            settings.llm_base_url = saved_base_url

    def test_resolve_base_url_unknown_provider(self):
        """未知 provider 无默认 base_url"""
        from app.config import settings

        saved_provider = settings.llm_provider
        saved_base_url = settings.llm_base_url
        try:
            settings.llm_provider = "unknown"
            settings.llm_base_url = ""
            from app.llm.analyzer import _resolve_base_url
            assert _resolve_base_url() == ""
        finally:
            settings.llm_provider = saved_provider
            settings.llm_base_url = saved_base_url

    def test_provider_config_fields_exist(self):
        """config 中存在 llm_provider 和 llm_base_url 字段"""
        from app.config import settings
        assert hasattr(settings, "llm_provider")
        assert hasattr(settings, "llm_base_url")
        assert settings.llm_provider in ("openai", "zhipu", "custom", "unknown")
