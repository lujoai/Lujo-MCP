"""单元测试：MCP stacktrace 工具（FR11 源码片段 + 各分支覆盖）"""

import sys

from app.mcp.tools import stacktrace_api


class TestStacktraceHandler:
    """覆盖 handler() 的各分支：无异常无 request_id / request_id 无错误 / request_id 有错误 / 当前异常"""

    def test_no_exception_no_request_id(self, monkeypatch):
        monkeypatch.setattr(sys, "exc_info", lambda: (None, None, None))
        result = stacktrace_api.handler({})
        assert result == {"message": "当前上下文中没有异常"}

    def test_request_id_no_error(self, monkeypatch):
        monkeypatch.setattr(sys, "exc_info", lambda: (None, None, None))
        monkeypatch.setattr(
            stacktrace_api,
            "get_logs",
            lambda request_id: [{"step": "info", "data": "x"}],
        )
        result = stacktrace_api.handler({"request_id": "abc"})
        assert result["request_id"] == "abc"
        assert result["message"] == "当前请求没有捕获到异常"

    def test_request_id_with_error(self, monkeypatch):
        monkeypatch.setattr(sys, "exc_info", lambda: (None, None, None))
        monkeypatch.setattr(
            stacktrace_api,
            "get_logs",
            lambda request_id: [{"step": "error", "data": "boom"}],
        )
        result = stacktrace_api.handler({"request_id": "abc"})
        assert result["request_id"] == "abc"
        exc = result["exception"]
        assert exc["type"] == "LoggedError"
        assert exc["message"] == "boom"
        assert exc["frame_count"] == 0
        assert result["code_snippets"] == []
        assert "ai_summary" in result

    def test_request_id_missing_arg(self, monkeypatch):
        """arguments 不含 request_id 时不应抛 KeyError"""
        monkeypatch.setattr(sys, "exc_info", lambda: (None, None, None))
        result = stacktrace_api.handler({})
        assert "message" in result

    def test_current_exception_captured(self, monkeypatch):
        captured = {
            "type": "ValueError",
            "message": "bad",
            "traceback": "tb",
            "frames": [],
            "frame_count": 0,
        }
        monkeypatch.setattr(sys, "exc_info", lambda: (None, ValueError("bad"), None))
        monkeypatch.setattr(stacktrace_api, "capture_exception", lambda exc: captured)
        monkeypatch.setattr(stacktrace_api, "get_snippets_for_frames", lambda frames: [])
        result = stacktrace_api.handler({})
        assert result["exception"] is captured
        assert result["code_snippets"] == []

    def test_invoke_wrapper(self, monkeypatch):
        monkeypatch.setattr(sys, "exc_info", lambda: (None, None, None))

        class Body:
            request_id = "r1"

        monkeypatch.setattr(stacktrace_api, "get_logs", lambda rid: [])
        result = stacktrace_api.invoke(Body())
        assert result["request_id"] == "r1"


class TestGetStacktrace:
    """覆盖 get_stacktrace()：无记录无异常 / 按 error_id 取 / 取最新"""

    def test_no_trace_no_exception(self, monkeypatch):
        monkeypatch.setattr("app.runtime.core.errors.get_latest", lambda: None)
        monkeypatch.setattr(sys, "exc_info", lambda: (None, None, None))
        result = stacktrace_api.get_stacktrace()
        assert result == {"message": "当前没有捕获到异常"}

    def test_by_error_id(self, monkeypatch):
        err = {
            "error_id": "e1",
            "type": "ValueError",
            "message": "m",
            "traceback": "t",
            "frames": [],
            "frame_count": 0,
        }
        monkeypatch.setattr("app.runtime.core.errors.get_by_id", lambda tid: err)
        monkeypatch.setattr(stacktrace_api, "get_snippets_for_frames", lambda frames: [])
        result = stacktrace_api.get_stacktrace("e1")
        assert result["error_id"] == "e1"
        assert result["exception"]["type"] == "ValueError"
        assert result["code_snippets"] == []

    def test_fallback_to_latest(self, monkeypatch):
        err = {
            "error_id": "e2",
            "type": "KeyError",
            "message": "k",
            "traceback": "t",
            "frames": [],
            "frame_count": 0,
        }
        monkeypatch.setattr("app.runtime.core.errors.get_latest", lambda: err)
        monkeypatch.setattr(stacktrace_api, "get_snippets_for_frames", lambda frames: [])
        result = stacktrace_api.get_stacktrace()
        assert result["error_id"] == "e2"
