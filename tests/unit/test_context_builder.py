"""单元测试：context builder"""
from app.runtime.context.builder import build_context


class TestContextBuilder:

    def test_build_basic_context(self):
        logs = [
            {"timestamp": 1.0, "step": "request_start", "data": {"key": "value"}},
            {"timestamp": 2.0, "step": "processing", "data": None},
            {"timestamp": 3.0, "step": "response_ready", "data": {"status": "ok"}},
        ]
        ctx = build_context("test-001", logs)

        assert ctx["request_id"] == "test-001"
        assert ctx["flow"] == ["request_start", "processing", "response_ready"]
        assert ctx["input"] == {"key": "value"}
        assert ctx["output"] == {"status": "ok"}
        assert ctx["errors"] == []

    def test_build_context_with_errors(self):
        logs = [
            {"timestamp": 1.0, "step": "request_start", "data": {"x": 1}},
            {"timestamp": 2.0, "step": "error", "data": "division by zero"},
            {"timestamp": 3.0, "step": "error", "data": "connection reset"},
        ]
        ctx = build_context("test-002", logs)

        assert ctx["request_id"] == "test-002"
        assert ctx["flow"] == ["request_start", "error", "error"]
        assert len(ctx["errors"]) == 2
        assert ctx["errors"] == ["division by zero", "connection reset"]
        assert ctx["output"] is None

    def test_empty_logs(self):
        ctx = build_context("test-003", [])
        assert ctx["request_id"] == "test-003"
        assert ctx["flow"] == []
        assert ctx["input"] is None
        assert ctx["output"] is None
