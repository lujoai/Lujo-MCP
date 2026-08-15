"""v0.5.1 SM3 集成测试 —— resolve_stack MCP 工具 + build_debug_context Source Map 集成。

降级矩阵核心约定：
- SOURCEMAP_ENABLED=false → resolved_frames=None，行为与 v0.5.0 完全一致
- 开启但无 map → resolved_frames=None（未命中不注入）
- 开启且命中 → resolved_frames 注入，code_snippets/fault_localization 用还原帧，
  exception.frames 保持原始 minified 帧（原始证据不丢失）
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.runtime.collectors import sourcemap_store
from app.runtime.core import trace_repo
from app.runtime.context.builder import build_debug_context


@pytest.fixture(autouse=True)
def _clean_env():
    sourcemap_store._uploads.clear()
    old_enabled = settings.sourcemap_enabled
    settings.sourcemap_enabled = False
    yield
    sourcemap_store._uploads.clear()
    settings.sourcemap_enabled = old_enabled


def _map_with_content() -> dict:
    """gen 1:0 → src0:1:0（name handleSubmit），src0 内嵌 12 行 sourcesContent。"""
    return {
        "version": 3,
        "sources": ["src/app.ts"],
        "names": ["handleSubmit"],
        "sourcesContent": [
            "".join(f"// line {i}\n" for i in range(1, 13)).replace(
                "// line 1", "export function handleSubmit(e) { throw new Error('boom'); }"
            )
        ],
        "mappings": "AAAAA",
    }


_MINIFIED_FRAMES = [
    {"file": "https://cdn.example.com/static/js/app.9f3b2c.js",
     "line": 1, "column": 0, "function": "t"},
]


# ── resolve_stack 工具 handler ──


class TestResolveStackHandler:
    def test_empty_frames_error(self):
        from app.mcp.tools.sourcemap_api import handler

        result = handler({"frames": []})
        assert "error" in result

    def test_non_list_frames_error(self):
        from app.mcp.tools.sourcemap_api import handler

        result = handler({"frames": "not-a-list"})
        assert "error" in result

    def test_disabled_returns_error(self):
        from app.mcp.tools.sourcemap_api import handler

        result = handler({"frames": _MINIFIED_FRAMES})
        assert "error" in result
        assert "disabled" in result["error"]

    def test_enabled_resolves(self):
        from app.mcp.tools.sourcemap_api import handler

        settings.sourcemap_enabled = True
        sourcemap_store.upload_sourcemap("app.9f3b2c.js", _map_with_content())
        result = handler({"frames": _MINIFIED_FRAMES})
        assert "error" not in result
        assert result["resolved_count"] == 1
        assert result["total_frames"] == 1
        assert result["resolved_frames"][0]["file"] == "src/app.ts"
        assert result["code_snippets"][0]["found"] is True

    def test_enabled_no_map_passthrough(self):
        from app.mcp.tools.sourcemap_api import handler

        settings.sourcemap_enabled = True
        result = handler({"frames": _MINIFIED_FRAMES})
        assert result["resolved_count"] == 0
        assert result["resolved_frames"] == _MINIFIED_FRAMES

    def test_explicit_artifact(self):
        from app.mcp.tools.sourcemap_api import handler

        settings.sourcemap_enabled = True
        sourcemap_store.upload_sourcemap("custom-key", _map_with_content())
        result = handler({
            "frames": [{"file": "whatever.js", "line": 1, "column": 0, "function": "t"}],
            "artifact": "custom-key",
        })
        assert result["resolved_count"] == 1

    def test_tool_registered_with_metadata(self):
        from app.mcp.protocol.server import _tool_registry
        from app.mcp.tools import register_all_tools

        _tool_registry.clear()
        register_all_tools()
        tool = _tool_registry["resolve_stack"]
        assert tool["category"] == "agent"
        assert tool["experimental"] is True
        assert "frames" in tool["inputSchema"]["properties"]

    def test_role_requirement_readonly(self):
        from app.mcp.tools import TOOL_ROLE_REQUIREMENTS

        assert TOOL_ROLE_REQUIREMENTS["resolve_stack"] == ("admin", "developer", "viewer")


# ── build_debug_context 集成 ──


class TestBuilderIntegration:
    def _save_frontend_trace(self) -> str:
        return trace_repo.save_trace(
            "TypeError",
            "Cannot read properties of undefined (reading 'map')",
            frames=_MINIFIED_FRAMES,
            source="browser-sdk",
            extra={"url": "https://app.example.com/orders", "artifact": "app.9f3b2c.js"},
        )

    def test_disabled_behavior_unchanged(self):
        tid = self._save_frontend_trace()
        ctx = build_debug_context(tid)
        assert ctx is not None
        assert ctx.resolved_frames is None
        # exception.frames 保持 minified 原帧
        assert ctx.exception["frames"] == _MINIFIED_FRAMES

    def test_enabled_no_map_not_injected(self):
        settings.sourcemap_enabled = True
        tid = self._save_frontend_trace()
        ctx = build_debug_context(tid)
        assert ctx.resolved_frames is None

    def test_enabled_hit_injects_resolved_frames(self):
        settings.sourcemap_enabled = True
        sourcemap_store.upload_sourcemap("app.9f3b2c.js", _map_with_content())
        tid = self._save_frontend_trace()
        ctx = build_debug_context(tid)

        assert ctx.resolved_frames is not None
        assert ctx.resolved_frames[0]["file"] == "src/app.ts"
        assert ctx.resolved_frames[0]["resolved"] is True
        assert ctx.resolved_frames[0]["original"]["file"].endswith("app.9f3b2c.js")
        # 原始 minified 证据不丢失
        assert ctx.exception["frames"] == _MINIFIED_FRAMES

    def test_hit_uses_sources_content_snippets(self):
        settings.sourcemap_enabled = True
        sourcemap_store.upload_sourcemap("app.9f3b2c.js", _map_with_content())
        tid = self._save_frontend_trace()
        ctx = build_debug_context(tid)

        assert ctx.code_snippets, "应注入还原帧源码片段"
        s = ctx.code_snippets[0]
        assert s["file"] == "src/app.ts"
        assert s["error_line"] == 1
        assert s["found"] is True
        assert "handleSubmit" in s["snippet"]

    def test_hit_fault_localization_uses_original_source(self):
        settings.sourcemap_enabled = True
        sourcemap_store.upload_sourcemap("app.9f3b2c.js", _map_with_content())
        tid = self._save_frontend_trace()
        ctx = build_debug_context(tid)

        fl = ctx.fault_localization
        assert fl is not None
        files = [f.get("file") for f in fl.get("suspicious_frames", [])]
        assert "src/app.ts" in files

    def test_python_trace_unaffected(self):
        settings.sourcemap_enabled = True
        sourcemap_store.upload_sourcemap("app.js", _map_with_content())
        tid = trace_repo.save_trace(
            "ValueError", "bad value",
            frames=[{"file": "app/config.py", "line": 9, "function": "Settings"}],
            source="test",
        )
        ctx = build_debug_context(tid)
        assert ctx.resolved_frames is None
        assert ctx.exception["frames"][0]["file"] == "app/config.py"

    def test_resolution_failure_does_not_break_context(self, monkeypatch):
        settings.sourcemap_enabled = True
        # 内部抛异常 → builder 静默降级，context 仍可构建
        monkeypatch.setattr(
            "app.runtime.collectors.sourcemap_store.resolve_frames_auto",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        tid = self._save_frontend_trace()
        ctx = build_debug_context(tid)
        assert ctx is not None
        assert ctx.resolved_frames is None
        assert ctx.exception["frames"] == _MINIFIED_FRAMES


# ── DebugContext schema 兼容 ──


class TestSchemaCompat:
    def test_old_data_without_resolved_frames_validates(self):
        from app.schemas import DebugContext

        ctx = DebugContext(request_id="r1")
        assert ctx.resolved_frames is None

    def test_resolved_frames_serializable(self):
        settings.sourcemap_enabled = True
        sourcemap_store.upload_sourcemap("app.9f3b2c.js", _map_with_content())
        tid = trace_repo.save_trace(
            "TypeError", "boom", frames=_MINIFIED_FRAMES, source="browser-sdk",
        )
        ctx = build_debug_context(tid)
        assert ctx is not None
        dumped = ctx.model_dump()
        assert dumped["resolved_frames"] is None or isinstance(dumped["resolved_frames"], list)
        import json
        json.dumps(dumped, ensure_ascii=False, default=str)


# ── QualityScorer 联动（v0.5.1 SM4）──


class TestQualityScorerSourcemap:
    def _ctx(self, resolved=None, snippets=None):
        return {
            "exception": {
                "type": "TypeError",
                "message": "boom",
                "frames": [{"file": "app.js", "line": 1, "function": "t"}],
                "frame_count": 1,
            },
            "resolved_frames": resolved,
            "code_snippets": snippets or [],
            "runtime": None,
        }

    def test_trace_score_boosted_by_resolution(self):
        from app.quality.scorer import evaluate

        resolved = [{"file": "src/app.ts", "line": 10, "resolved": True}]
        base = evaluate({"debug_context": self._ctx(), "repair_context": {}})
        boosted = evaluate({"debug_context": self._ctx(resolved=resolved), "repair_context": {}})
        # 1 帧 minified（0.4）→ 还原后 +0.3 = 0.7
        dim_before = base.context_completeness.dimensions
        dim_after = boosted.context_completeness.dimensions
        from app.quality.schemas import ContextDimension

        assert dim_after[ContextDimension.TRACE].score > dim_before[ContextDimension.TRACE].score
        assert "source map" in dim_after[ContextDimension.TRACE].reason

    def test_resolution_evidence_item_added(self):
        from app.quality.scorer import evaluate

        resolved = [{"file": "src/app.ts", "line": 10, "resolved": True}]
        report = evaluate({"debug_context": self._ctx(resolved=resolved), "repair_context": {}})
        sm_evidence = [e for e in report.evidence_items if e.source == "sourcemap_resolver"]
        assert len(sm_evidence) == 1
        assert "src/app.ts" in sm_evidence[0].description

    def test_no_resolution_no_boost(self):
        from app.quality.scorer import _score_trace

        score = _score_trace(self._ctx(), {})
        assert "source map" not in score.reason


# ── Benchmark A/B 对照（v0.5.1 SM4）──


class TestBenchmarkSourcemapCase:
    def test_case_registered(self):
        from benchmark.cases import BENCHMARK_CASES, get_case

        assert get_case("frontend_minified_sourcemap") is not None
        assert len(BENCHMARK_CASES) == 6

    def test_quality_score_improves_after_resolution(self):
        """核心价值证明：还原后 Quality 评分必须高于还原前。"""
        from benchmark.cases import frontend_sourcemap_ab
        from app.quality.scorer import evaluate

        ab = frontend_sourcemap_ab()
        before = evaluate({"debug_context": ab["before"], "repair_context": {}})
        after = evaluate({"debug_context": ab["after"], "repair_context": {}})

        assert after.overall_score > before.overall_score, (
            f"source map 还原后评分应提升: before={before.overall_score} "
            f"after={after.overall_score}"
        )
        # 还原前 CODE_SNIPPET 维度缺失，还原后命中
        from app.quality.schemas import ContextDimension

        dims_before = before.context_completeness.dimensions
        dims_after = after.context_completeness.dimensions
        assert not dims_before[ContextDimension.CODE_SNIPPET].present
        assert dims_after[ContextDimension.CODE_SNIPPET].present
