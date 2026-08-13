"""Phase 3 D5 单元测试：MCP Debug Context 可观察性增强

覆盖 DebugContextTrace / observe_context / attach_metadata，
以及 context_api / debug_api / stacktrace_api 的 metadata 注入。
"""

from app import __version__
from app.mcp import observability
from app.mcp.observability import (
    DebugContextTrace,
    observe_context,
    attach_metadata,
)


class TestDebugContextTrace:
    """DebugContextTrace 纯观测结构：不修改业务逻辑、不调用 RAG。"""

    def test_defaults(self):
        trace = DebugContextTrace()
        assert trace.runtime_context_available is False
        assert trace.runtime_context_size == 0
        assert trace.experience_enabled is False
        assert trace.experience_hit_count == 0

    def test_to_metadata_shape(self):
        trace = DebugContextTrace(
            request_id="r1",
            runtime_context_available=True,
            runtime_context_size=123,
            experience_enabled=True,
            experience_hit_count=2,
            context_build_duration=0.01,
            response_duration=0.02,
        )
        md = trace.to_metadata()
        assert md["version"] == __version__
        assert md["runtime_context_available"] is True
        assert md["runtime_context_size"] == 123
        assert md["experience_enabled"] is True
        assert md["experience_hit_count"] == 2
        assert md["context_build_duration_ms"] == 10.0
        assert md["response_duration_ms"] == 20.0
        # metadata 只描述 Context，不含 AI 推理/Agent 状态字段
        assert "repair_plan" not in md
        assert "agent" not in md


class TestObserveContext:
    """observe_context 纯观察：不主动调用 RAG 检索。"""

    def test_runtime_context_present(self, monkeypatch):
        monkeypatch.setattr(observability.settings, "debug_experience_enabled", False)
        trace = observe_context(
            request_id="r1",
            context={"trace_id": "t1", "errors": []},
            context_build_duration=0.005,
        )
        assert trace.runtime_context_available is True
        assert trace.runtime_context_size > 0
        assert trace.experience_enabled is False
        assert trace.experience_hit_count == 0

    def test_runtime_context_missing(self):
        trace = observe_context(request_id="r1", context=None)
        assert trace.runtime_context_available is False
        assert trace.runtime_context_size == 0
        assert trace.experience_hit_count == 0

    def test_experience_hits_observed_from_existing_context(self, monkeypatch):
        """已有 Context 携带 debug_experience 时，记录命中数（不主动检索）。"""
        monkeypatch.setattr(observability.settings, "debug_experience_enabled", True)
        context = {
            "trace_id": "t1",
            "debug_experience": [{"fingerprint": "f1"}, {"fingerprint": "f2"}, {"fingerprint": "f3"}],
        }
        trace = observe_context(context=context)
        assert trace.experience_enabled is True
        assert trace.experience_hit_count == 3

    def test_experience_disabled_zero_count(self, monkeypatch):
        monkeypatch.setattr(observability.settings, "debug_experience_enabled", False)
        context = {"debug_experience": [{"fingerprint": "f1"}]}
        trace = observe_context(context=context)
        assert trace.experience_enabled is False
        assert trace.experience_hit_count == 1  # 仍观察已有携带，但标记开关为 False

    def test_experience_alt_key(self):
        """兼容 'experience' 键名。"""
        trace = observe_context(context={"experience": [{"x": 1}, {"y": 2}]})
        assert trace.experience_hit_count == 2

    def test_exception_degrade_on_serialize(self, monkeypatch):
        """序列化失败时静默降级，不抛出。"""
        class Bad:
            def __repr__(self):
                raise ValueError("boom")

        monkeypatch.setattr("json.dumps", lambda *a, **k: (_ for _ in ()).throw(TypeError("no")))
        trace = observe_context(context={"trace_id": "t1", "bad": Bad()})
        assert trace.runtime_context_available is True
        assert trace.runtime_context_size == 0


class TestAttachMetadata:
    """attach_metadata 向后兼容：只新增可选字段，不改旧字段。"""

    def test_attach_preserves_old_fields(self):
        result = {
            "request_id": "r1",
            "trace": [{"step": "info"}],
            "context": {"trace_id": "t1"},
        }
        trace = DebugContextTrace(
            request_id="r1",
            runtime_context_available=True,
            runtime_context_size=10,
        )
        out = attach_metadata(result, trace)
        assert out is result  # 就地修改
        assert out["request_id"] == "r1"
        assert out["trace"] == [{"step": "info"}]
        assert out["context"] == {"trace_id": "t1"}
        assert out["metadata"]["runtime_context_available"] is True


class TestToolIntegration:
    """验证 MCP 工具 handler / get_debug_context 注入 metadata。"""

    def test_context_api_handler_injects_metadata(self, monkeypatch):
        from app.mcp.tools import context_api
        monkeypatch.setattr(
            context_api, "get_logs", lambda request_id: [{"step": "info", "data": "x"}]
        )
        monkeypatch.setattr(
            context_api, "build_context",
            lambda request_id, trace: {"request_id": request_id, "errors": []},
        )
        monkeypatch.setattr(
            context_api, "get_snippets_for_frames", lambda frames: []
        )
        result = context_api.handler({"request_id": "abc"})
        assert result["request_id"] == "abc"
        md = result["metadata"]
        assert md["runtime_context_available"] is True
        assert md["experience_enabled"] is False
        assert md["context_build_duration_ms"] >= 0

    def test_context_api_missing_trace_no_metadata(self, monkeypatch):
        """无 trace 时返回错误分支，不注入 metadata（保持精确结构）。"""
        from app.mcp.tools import context_api
        monkeypatch.setattr(context_api, "get_logs", lambda request_id: [])
        result = context_api.handler({"request_id": "ghost"})
        assert "metadata" not in result
        assert result["error"]

    def test_debug_api_handler_injects_metadata(self, monkeypatch):
        from app.mcp.tools import debug_api
        monkeypatch.setattr(debug_api, "create_request_id", lambda: "rid-1")
        monkeypatch.setattr(debug_api, "add_log", lambda *a, **k: None)
        monkeypatch.setattr(debug_api, "get_logs", lambda rid: [{"step": "info"}])
        monkeypatch.setattr(
            debug_api, "build_context",
            lambda request_id, trace: {"request_id": request_id, "errors": []},
        )
        result = debug_api.handler({"payload": {"a": 1}})
        assert result["request_id"] == "rid-1"
        assert result["metadata"]["runtime_context_available"] is True
        assert result["metadata"]["experience_enabled"] is False

    def test_get_debug_context_available_injects_metadata(self, monkeypatch):
        from app.mcp.tools import debug_api
        from app.schemas import DebugContext
        monkeypatch.setattr(
            debug_api, "build_debug_context",
            lambda tid: DebugContext(request_id=tid, trace_id=tid, runtime={})
        )
        result = debug_api.get_debug_context("t1")
        assert result["trace_id"] == "t1"
        assert result["metadata"]["runtime_context_available"] is True

    def test_get_debug_context_none_no_metadata(self, monkeypatch):
        from app.mcp.tools import debug_api
        monkeypatch.setattr(debug_api, "build_debug_context", lambda tid: None)
        result = debug_api.get_debug_context("t1")
        assert result == {"message": "暂无捕获到的错误上下文"}
        assert "metadata" not in result

    def test_get_stacktrace_injects_metadata(self, monkeypatch):
        from app.mcp.tools import stacktrace_api
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
        assert result["metadata"]["runtime_context_available"] is True

    def test_get_stacktrace_none_no_metadata(self, monkeypatch):
        from app.mcp.tools import stacktrace_api
        monkeypatch.setattr("app.runtime.core.errors.get_latest", lambda: None)
        monkeypatch.setattr("app.runtime.core.errors.get_by_id", lambda tid: None)
        import sys
        monkeypatch.setattr(sys, "exc_info", lambda: (None, None, None))
        result = stacktrace_api.get_stacktrace()
        assert result == {"message": "当前没有捕获到异常"}
        assert "metadata" not in result