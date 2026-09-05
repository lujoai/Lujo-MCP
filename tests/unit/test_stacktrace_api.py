"""单元测试：MCP stacktrace 工具（FR11 源码片段 + 各分支覆盖）"""

from unittest.mock import MagicMock

from app.mcp.tools import stacktrace_api


class TestToolDefinition:
    def test_tool_def_schema(self):
        assert stacktrace_api.TOOL_DEF["name"] == "stacktrace"
        assert "description" in stacktrace_api.TOOL_DEF
        assert stacktrace_api.TOOL_DEF["inputSchema"]["type"] == "object"


class TestStacktraceHandler:
    """覆盖 handler() 的各分支：无异常无 request_id / request_id 无错误 / request_id 有错误 / 当前异常 / 空参回退最新错误"""

    def test_no_exception_no_request_id(self, monkeypatch):
        """无异常且 errors 存储为空 → 保持旧空结果消息（兼容既有调用方）。"""
        monkeypatch.setattr("app.runtime.core.errors.get_latest", lambda: None)
        result = stacktrace_api.handler({})
        assert result == {"message": "当前上下文中没有异常"}

    def test_empty_args_falls_back_to_latest_stored_error(self, monkeypatch):
        """FIX(v0.7.3): 无 request_id 且无活跃异常 → 读取 errors 存储最近一条
        （浏览器 SDK / ingest_error 上报的错误在此），不再返回死路消息。"""
        err = {
            "error_id": "err-latest",
            "type": "TypeError",
            "message": "browser boom",
            "traceback": "t",
            "frames": [{"file": "app.js", "line": 7, "function": "onClick"}],
            "frame_count": 1,
        }
        monkeypatch.setattr("app.runtime.core.errors.get_latest", lambda: err)
        monkeypatch.setattr(stacktrace_api, "get_snippets_for_frames", lambda frames: [])
        result = stacktrace_api.handler({})
        assert result["error_id"] == "err-latest"
        assert result["exception"]["type"] == "TypeError"
        assert result["exception"]["message"] == "browser boom"
        assert "ai_summary" in result

    def test_request_id_no_error(self, monkeypatch):
        monkeypatch.setattr(
            stacktrace_api,
            "get_logs",
            lambda request_id: [{"step": "info", "data": "x"}],
        )
        result = stacktrace_api.handler({"request_id": "abc"})
        assert result["request_id"] == "abc"
        assert result["message"] == "当前请求没有捕获到异常"

    def test_request_id_with_error(self, monkeypatch):
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

    def test_request_id_missing_arg(self):
        """arguments 不含 request_id 时不应抛 KeyError"""
        result = stacktrace_api.handler({})
        assert "message" in result

    def test_current_exception_captured_with_snippets(self, monkeypatch):
        mock_snippet = MagicMock()
        mock_snippet.model_dump.return_value = {"file": "app.py", "line": 10, "code": "pass"}

        captured = {
            "type": "ValueError",
            "message": "bad",
            "traceback": "tb",
            "frames": [{"filename": "app.py", "lineno": 10}],
            "frame_count": 1,
        }
        monkeypatch.setattr(stacktrace_api, "capture_exception", lambda exc: captured)
        monkeypatch.setattr(stacktrace_api, "format_trace_for_ai", lambda exc: "AI summary text")
        monkeypatch.setattr(stacktrace_api, "get_snippets_for_frames", lambda frames: [mock_snippet])

        try:
            raise ValueError("bad")
        except ValueError:
            result = stacktrace_api.handler({})

        assert result["exception"] is captured
        assert len(result["code_snippets"]) == 1
        assert result["code_snippets"][0]["file"] == "app.py"
        assert result["ai_summary"] == "AI summary text"

    def test_invoke_wrapper(self, monkeypatch):
        class Body:
            request_id = "r1"

        monkeypatch.setattr(stacktrace_api, "get_logs", lambda rid: [])
        result = stacktrace_api.invoke(Body())
        assert result["request_id"] == "r1"


class TestGetStacktrace:
    """覆盖 get_stacktrace()：无记录无异常 / 按 error_id 取 / 取最新 / 当前系统异常降级 / metadata 观测"""

    def test_no_trace_no_exception(self, monkeypatch):
        monkeypatch.setattr("app.runtime.core.errors.get_latest", lambda: None)
        result = stacktrace_api.get_stacktrace()
        assert result == {"message": "当前没有捕获到异常"}

    def test_by_error_id_with_snippets(self, monkeypatch):
        mock_snippet = MagicMock()
        mock_snippet.model_dump.return_value = {"file": "server.py", "line": 42}

        err = {
            "error_id": "e1",
            "type": "ValueError",
            "message": "m",
            "traceback": "t",
            "frames": [{"file": "server.py", "line": 42, "function": "main", "code": "raise", "locals": {}}],
            "frame_count": 1,
        }
        monkeypatch.setattr("app.runtime.core.errors.get_by_id", lambda tid: err)
        monkeypatch.setattr(stacktrace_api, "get_snippets_for_frames", lambda frames: [mock_snippet])
        result = stacktrace_api.get_stacktrace("e1")
        assert result["error_id"] == "e1"
        assert result["exception"]["type"] == "ValueError"
        assert len(result["code_snippets"]) == 1
        assert result["code_snippets"][0]["file"] == "server.py"

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

    def test_fallback_to_active_sys_exception(self, monkeypatch):
        monkeypatch.setattr("app.runtime.core.errors.get_latest", lambda: None)
        captured = {
            "error_id": "live-1",
            "type": "ZeroDivisionError",
            "message": "division by zero",
            "traceback": "tb",
            "frames": [],
            "frame_count": 0,
        }
        monkeypatch.setattr(stacktrace_api, "capture_exception", lambda exc: captured)
        monkeypatch.setattr(stacktrace_api, "get_snippets_for_frames", lambda frames: [])

        try:
            raise ZeroDivisionError("division by zero")
        except ZeroDivisionError:
            result = stacktrace_api.get_stacktrace()

        assert result["error_id"] == "live-1"
        assert result["exception"]["type"] == "ZeroDivisionError"
