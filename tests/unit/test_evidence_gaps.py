"""证据缺口提示（missing_evidence）纯函数测试。

覆盖三类场景：全缺 / 全有 / 部分缺失，以及防御性输入与提示文案契约。
被测对象 app/runtime/context/evidence_gaps.compute_missing_evidence 是
纯函数：只读输入 dict、无 I/O、无 LLM。
"""
from app.runtime.context.evidence_gaps import compute_missing_evidence

ALL_DIMENSIONS = {
    "trace",
    "code_snippet",
    "runtime",
    "git_context",
    "network",
    "ui_event",
    "spec",
}


def _full_ctx() -> dict:
    """构造证据齐备的最小上下文（形状与 build_debug_context 输出一致）。"""
    return {
        "trace_id": "t1",
        "exception": {
            "type": "ValueError",
            "message": "boom",
            "frames": [{"file": "app/x.py", "line": 1, "function": "f"}],
            "frame_count": 1,
        },
        "code_snippets": [{"file": "app/x.py", "found": True, "snippet": "x = 1"}],
        "runtime": {"process": {"pid": 1234}, "system": {}},
        "git_blame": [{"file": "app/x.py", "author": "a"}],
        "recent_diffs": [{"file": "app/x.py", "diff": "-x\n+y"}],
        "network_trace": [{"method": "GET", "url": "http://x/api"}],
        "ui_events": [{"event_type": "click"}],
        "spec_diffs": [{"field": "status", "expected": 200, "actual": 500}],
    }


# ── 1. 全缺 ──────────────────────────────────────────────────────────


class TestAllMissing:
    def test_empty_dict_reports_all_dimensions(self):
        missing = compute_missing_evidence({})
        assert {m["dimension"] for m in missing} == ALL_DIMENSIONS

    def test_none_input_returns_empty_list(self):
        assert compute_missing_evidence(None) == []

    def test_each_item_shape_and_nonempty_hint(self):
        for m in compute_missing_evidence({}):
            assert set(m.keys()) == {"dimension", "hint"}
            assert m["dimension"] in ALL_DIMENSIONS
            assert m["hint"].strip()


# ── 2. 全有 ──────────────────────────────────────────────────────────


class TestAllPresent:
    def test_full_ctx_returns_empty(self):
        assert compute_missing_evidence(_full_ctx()) == []

    def test_frame_count_only_still_counts_trace_present(self):
        """无 frames 列表但 frame_count>0 的旧形状仍算有堆栈。"""
        ctx = _full_ctx()
        ctx["exception"] = {"type": "E", "message": "m", "frames": [], "frame_count": 3}
        dims = {m["dimension"] for m in compute_missing_evidence(ctx)}
        assert "trace" not in dims


# ── 3. 部分缺失 ──────────────────────────────────────────────────────


class TestPartialMissing:
    def test_only_network_present(self):
        ctx = {"network_trace": [{"method": "GET", "url": "http://x"}]}
        dims = {m["dimension"] for m in compute_missing_evidence(ctx)}
        assert "network" not in dims
        assert dims == ALL_DIMENSIONS - {"network"}

    def test_all_snippets_unfound_counted_missing(self):
        """源码片段存在但全部 found=False → 视为缺失（对齐 scorer 口径）。"""
        ctx = _full_ctx()
        ctx["code_snippets"] = [{"file": "a.js", "found": False}]
        dims = {m["dimension"] for m in compute_missing_evidence(ctx)}
        assert "code_snippet" in dims
        assert "network" not in dims

    def test_related_specs_only_counts_as_spec_present(self):
        ctx = _full_ctx()
        ctx["spec_diffs"] = None
        ctx["related_specs"] = [{"file": "docs/spec.md", "content": "..."}]
        dims = {m["dimension"] for m in compute_missing_evidence(ctx)}
        assert "spec" not in dims

    def test_git_blame_alone_counts_as_git_present(self):
        ctx = _full_ctx()
        ctx["recent_diffs"] = None
        dims = {m["dimension"] for m in compute_missing_evidence(ctx)}
        assert "git_context" not in dims

    def test_runtime_without_pid_counted_missing(self):
        """runtime 存在但缺 process.pid（旧形状）→ 视为缺失（对齐 scorer）。"""
        ctx = _full_ctx()
        ctx["runtime"] = {"pid": 1234}
        dims = {m["dimension"] for m in compute_missing_evidence(ctx)}
        assert "runtime" in dims


# ── 4. 提示文案契约：必须给 AI 可执行的下一步 ────────────────────────


class TestHintContract:
    def test_network_hint_mentions_tool(self):
        ctx = {"network_trace": None}
        hint = next(
            m["hint"] for m in compute_missing_evidence(ctx) if m["dimension"] == "network"
        )
        assert "get_network_trace" in hint

    def test_git_hint_mentions_both_tools(self):
        ctx = {"git_blame": None, "recent_diffs": None}
        hint = next(
            m["hint"] for m in compute_missing_evidence(ctx) if m["dimension"] == "git_context"
        )
        assert "get_blame_for_frame" in hint
        assert "get_recent_diff" in hint

    def test_spec_hint_mentions_verify(self):
        ctx = {"spec_diffs": None, "related_specs": None}
        hint = next(
            m["hint"] for m in compute_missing_evidence(ctx) if m["dimension"] == "spec"
        )
        assert "verify" in hint

    def test_kb_and_llm_dimensions_not_reported(self):
        """KB / LLM 属修复链路维度，不在 DebugContext 判定范围。"""
        dims = {m["dimension"] for m in compute_missing_evidence({})}
        assert "knowledge_base" not in dims
        assert "llm_analysis" not in dims


# ── 5. 防御性输入：畸形数据不抛异常 ──────────────────────────────────


class TestDefensiveInputs:
    def test_malformed_values_do_not_raise(self):
        ctx = {
            "exception": "not-a-dict",
            "code_snippets": "not-a-list",
            "runtime": 123,
            "git_blame": True,  # 非列表真值——宽松判定，不抛异常即可
            "network_trace": [{"x": 1}],
            "ui_events": [None, "str"],
        }
        missing = compute_missing_evidence(ctx)
        dims = {m["dimension"] for m in missing}
        assert {"trace", "code_snippet", "runtime"} <= dims
        assert "network" not in dims

    def test_frame_count_bool_not_treated_as_int(self):
        """frame_count=True（bool）不得被当成正整数帧数。"""
        ctx = {"exception": {"type": "E", "message": "m", "frames": [], "frame_count": True}}
        dims = {m["dimension"] for m in compute_missing_evidence(ctx)}
        assert "trace" in dims
