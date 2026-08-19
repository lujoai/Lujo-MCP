"""DebugContext Runtime Integration 测试。

验证：
1. build_debug_context() 返回 DebugContext 实例（非 dict）
2. MCP get_debug_context 输出 JSON 结构与修改前一致
3. Dashboard trace endpoint 输出 JSON 结构不变
4. model_dump() 产出与原始 dict 等价的 JSON
"""
import json


from app.schemas import DebugContext
from app.runtime.core import trace_repo
from app.runtime.context.builder import build_debug_context


# ── 1. 返回类型验证 ──

class TestReturnType:
    """build_debug_context() 必须返回 DebugContext 实例（非 dict）。"""

    def test_returns_debug_context_instance(self):
        tid = trace_repo.save_trace("ValueError", "msg", [])
        ctx = build_debug_context(tid)
        assert ctx is not None
        assert isinstance(ctx, DebugContext)

    def test_returns_none_for_missing_trace(self):
        ctx = build_debug_context("nonexistent-trace-id")
        assert ctx is None

    def test_not_dict(self):
        """返回值不应是 plain dict。"""
        tid = trace_repo.save_trace("E", "m", [])
        ctx = build_debug_context(tid)
        assert ctx is not None
        assert not isinstance(ctx, dict)


# ── 2. MCP get_debug_context JSON 结构不变 ──

class TestMcpGetDebugContextJsonStructure:
    """MCP tool get_debug_context() 返回的 dict 结构应与修改前一致。"""

    def test_output_has_all_20_fields_plus_metadata(self):
        from app.mcp.tools.debug_api import get_debug_context

        tid = trace_repo.save_trace(
            "ValueError", "bad value",
            frames=[{"file": "app/config.py", "line": 9, "function": "Settings"}],
            source="test",
        )
        result = get_debug_context(tid)

        # 原始 20 字段 + v0.5.1 resolved_frames
        expected_keys = {
            "request_id", "trace_id", "trace_kind", "flow", "input", "output",
            "errors", "exception", "source", "extra", "code_snippets",
            "static_analysis", "git_blame", "recent_diffs", "related_specs",
            "network_trace", "ui_events", "spec_diffs", "runtime",
            "fault_localization", "resolved_frames",
        }
        # metadata 由 attach_metadata 注入
        expected_keys.add("metadata")

        for key in expected_keys:
            assert key in result, f"Missing key in MCP output: {key}"

    def test_output_json_serializable(self):
        """MCP 输出必须可 JSON 序列化（MCP 协议要求）。"""
        from app.mcp.tools.debug_api import get_debug_context

        tid = trace_repo.save_trace("E", "m", [])
        result = get_debug_context(tid)
        # 不抛异常即通过
        json.dumps(result, ensure_ascii=False, default=str)

    def test_output_trace_id_matches(self):
        from app.mcp.tools.debug_api import get_debug_context

        tid = trace_repo.save_trace("E", "m", [])
        result = get_debug_context(tid)
        assert result["trace_id"] == tid

    def test_output_none_trace_returns_message(self):
        from app.mcp.tools.debug_api import get_debug_context

        result = get_debug_context("nonexistent")
        assert result == {"message": "暂无捕获到的错误上下文"}


# ── 3. Dashboard trace endpoint JSON 不变 ──

class TestDashboardTraceEndpointJsonStructure:
    """Dashboard /trace/{trace_id} 返回结构应与修改前一致。"""

    def test_trace_detail_has_expected_fields(self):
        from app.api.dashboard import get_trace_detail

        tid = trace_repo.save_trace(
            "ValueError", "bad value",
            frames=[{"file": "app/config.py", "line": 9, "function": "Settings"}],
            source="test",
        )
        result = get_trace_detail(tid)

        expected_keys = {
            "trace_id", "trace_kind", "exception", "errors",
            "spec_diffs", "code_snippets", "source", "extra",
            "quality_report",
        }
        for key in expected_keys:
            assert key in result, f"Missing key in dashboard output: {key}"

    def test_trace_detail_trace_id_matches(self):
        from app.api.dashboard import get_trace_detail

        tid = trace_repo.save_trace("E", "m", [])
        result = get_trace_detail(tid)
        assert result["trace_id"] == tid

    def test_trace_quality_has_expected_fields(self):
        from app.api.dashboard import get_trace_quality

        tid = trace_repo.save_trace("E", "m", [])
        result = get_trace_quality(tid)
        assert "trace_id" in result
        assert "quality_report" in result
        assert result["trace_id"] == tid


# ── 4. model_dump() 与原始 dict 等价 ──

class TestModelDumpEquivalence:
    """DebugContext.model_dump() 产出的 dict 应与原始 build_debug_context 的 dict 等价。"""

    def test_model_dump_has_all_20_fields(self):
        tid = trace_repo.save_trace(
            "ValueError", "msg",
            frames=[{"file": "app/config.py", "line": 9, "function": "Settings"}],
            source="test",
        )
        ctx = build_debug_context(tid)
        dumped = ctx.model_dump()

        expected_fields = {
            "request_id", "trace_id", "trace_kind", "flow", "input", "output",
            "errors", "exception", "runtime", "source", "extra", "code_snippets",
            "static_analysis", "git_blame", "recent_diffs", "related_specs",
            "network_trace", "ui_events", "spec_diffs", "fault_localization",
            "resolved_frames",
        }
        assert set(dumped.keys()) == expected_fields

    def test_model_dump_values_match_attributes(self):
        """model_dump() 的值应与属性访问一致。"""
        tid = trace_repo.save_trace("ValueError", "msg", [])
        ctx = build_debug_context(tid)
        dumped = ctx.model_dump()

        assert dumped["request_id"] == ctx.request_id
        assert dumped["trace_id"] == ctx.trace_id
        assert dumped["trace_kind"] == ctx.trace_kind
        assert dumped["flow"] == ctx.flow
        assert dumped["errors"] == ctx.errors
        assert dumped["exception"] == ctx.exception
        assert dumped["runtime"] == ctx.runtime

    def test_model_dump_json_serializable(self):
        """model_dump() 结果必须可 JSON 序列化。"""
        tid = trace_repo.save_trace("E", "m", [])
        ctx = build_debug_context(tid)
        dumped = ctx.model_dump()
        json.dumps(dumped, ensure_ascii=False, default=str)


# ── 5. analyze_with_llm 适配 ──

class TestAnalyzeWithLlmAdaptation:
    """analyze_with_llm 应正确将 DebugContext 转为 dict 传给 analyze()。"""

    def test_analyze_with_llm_returns_dict(self, monkeypatch):
        """analyze_with_llm 返回 dict（非 DebugContext）。

        隔离 .env 真实 Key：本地 .env 若配置了无效/不可达的 provider，
        真实调用会因 socket 连接挂起 + 重试阻塞测试（环境依赖非代码回归）。
        强制无 Key 快速回退，不发任何网络请求。
        """
        from app.mcp.tools.debug_api import analyze_with_llm
        from app.llm import clients
        from app.config import settings

        monkeypatch.setattr(settings, "openai_api_key", "")
        monkeypatch.setattr(clients, "_client", None)

        tid = trace_repo.save_trace("E", "m", [])
        result = analyze_with_llm(tid)
        assert isinstance(result, dict)
        assert not isinstance(result, DebugContext)
