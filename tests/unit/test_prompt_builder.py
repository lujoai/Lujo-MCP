"""单元测试：FR12 调试提示词生成（prompt_builder + GET /api/debug/prompt 端点）"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.debug import router
from app.llm.prompt_builder import DEFAULT_PROMPT_TEMPLATE, build_debug_prompt, load_prompt_template


def _sample_context() -> dict:
    """一个带异常 + 运行时的最小调试上下文。"""
    return {
        "request_id": "req-123",
        "flow": ["request_start", "processing", "error"],
        "input": {"user_id": 1},
        "output": None,
        "errors": [{"type": "ValueError", "message": "bad value"}],
        "exception": {
            "type": "ValueError",
            "message": "bad value",
            "frames": [{"file": "app/demo.py", "line": 42, "function": "run"}],
        },
        "runtime": {"python": {"version": "3.12"}, "system": {"cpu_percent": 5.0}},
    }


class TestBuildDebugPrompt:

    def test_default_template_contains_request_id_and_context(self):
        prompt = build_debug_prompt(_sample_context())
        assert "req-123" in prompt          # $request_id 已替换
        assert "bad value" in prompt        # 上下文异常信息
        assert "app/demo.py" in prompt      # 异常帧
        assert "排障专家" in prompt          # 内置模板指令文本

    def test_context_is_redacted_before_substitution(self):
        """发送给模板前上下文已脱敏，敏感字段不出现。"""
        ctx = _sample_context()
        ctx["input"] = {"api_key": "sk-12345", "name": "ok"}
        prompt = build_debug_prompt(ctx)
        assert "sk-12345" not in prompt
        assert "***REDACTED***" in prompt

    def test_custom_template_file(self, tmp_path):
        tpl = tmp_path / "prompt.txt"
        tpl.write_text("custom-prefix\nrequest=$request_id\nctx:\n$context\n", encoding="utf-8")
        prompt = build_debug_prompt(_sample_context(), template_path=str(tpl))
        assert "custom-prefix" in prompt
        assert "request=req-123" in prompt
        assert "bad value" in prompt

    def test_missing_template_file_falls_back_to_default(self):
        prompt = build_debug_prompt(_sample_context(), template_path="Z:/no/such/file.txt")
        assert "排障专家" in prompt  # 回退内置模板

    def test_template_with_stray_dollar_does_not_raise(self, tmp_path):
        """safe_substitute：模板中非法 $5 原样保留，不抛 ValueError。"""
        tpl = tmp_path / "prompt.txt"
        tpl.write_text("price: $5 and ctx:\n$context\n", encoding="utf-8")
        prompt = build_debug_prompt(_sample_context(), template_path=str(tpl))
        assert "price: $5" in prompt


class TestLoadPromptTemplate:

    def test_empty_path_returns_default(self):
        assert load_prompt_template(None) == DEFAULT_PROMPT_TEMPLATE
        assert load_prompt_template("") == DEFAULT_PROMPT_TEMPLATE

    def test_invalid_path_returns_default(self):
        assert load_prompt_template("Z:/definitely/not/exists.txt") == DEFAULT_PROMPT_TEMPLATE


class TestPromptEndpoint:

    @pytest.fixture
    def client(self):
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    @staticmethod
    def _fake_debug_context():
        return SimpleNamespace(model_dump=lambda: _sample_context())

    def test_prompt_returns_plain_text_prompt(self, client):
        fake_logs = [{"timestamp": 1.0, "step": "request_start", "data": {}}]
        with patch("app.api.debug.get_logs", return_value=fake_logs), patch(
            "app.api.debug.build_debug_context", return_value=self._fake_debug_context()
        ):
            resp = client.get("/api/debug/prompt", params={"request_id": "req-123"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["request_id"] == "req-123"
        assert "bad value" in body["prompt"]
        assert "排障专家" in body["prompt"]

    def test_prompt_unknown_request_returns_404(self, client):
        with patch("app.api.debug.get_logs", return_value=[]):
            resp = client.get("/api/debug/prompt", params={"request_id": "nope"})
        assert resp.status_code == 404

    def test_prompt_missing_request_id_returns_422(self, client):
        with patch("app.api.debug.get_logs", return_value=[]):
            resp = client.get("/api/debug/prompt")
        assert resp.status_code == 422  # 必需 query 参数缺失
