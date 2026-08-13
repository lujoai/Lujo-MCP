"""DebugContext v0.5 Schema 对齐测试。

覆盖：
- 新字段存在且 Optional
- 老数据（仅 7 字段）仍可 validate
- unknown field 兼容（extra="allow"）
- model_dump(exclude_none=True) 正确排除 None
- build_debug_context() 完整输出可被 DebugContext validate
"""
import pytest
from pydantic import ValidationError

from app.schemas import DebugContext


# ── 1. 新字段存在 ──

class TestNewFieldsExist:
    """v0.5 新增的 13 个字段全部存在于 model 中。"""

    EXPECTED_NEW_FIELDS = [
        "trace_id",
        "trace_kind",
        "source",
        "extra",
        "code_snippets",
        "static_analysis",
        "git_blame",
        "recent_diffs",
        "related_specs",
        "network_trace",
        "ui_events",
        "spec_diffs",
        "fault_localization",
    ]

    def test_all_new_fields_present_in_model(self):
        for field in self.EXPECTED_NEW_FIELDS:
            assert field in DebugContext.model_fields, f"Missing field: {field}"

    def test_total_field_count_is_20(self):
        """7 基础 + 13 新增 = 20"""
        assert len(DebugContext.model_fields) == 20

    def test_new_fields_are_optional(self):
        """所有新增字段必须有默认值（Optional）。"""
        for field in self.EXPECTED_NEW_FIELDS:
            field_info = DebugContext.model_fields[field]
            assert field_info.is_required() is False, (
                f"Field {field} should be Optional (not required)"
            )

    def test_extra_config_is_allow(self):
        """model_config extra='allow' 支持未来扩展。"""
        assert DebugContext.model_config.get("extra") == "allow"


# ── 2. 老数据仍可 validate ──

class TestBackwardCompatibility:
    """v0.4 格式的数据（仅 request_id + flow）必须仍能 validate。"""

    def test_minimal_old_data_validates(self):
        """仅 request_id（最老格式）可 validate。"""
        ctx = DebugContext(request_id="req-001")
        assert ctx.request_id == "req-001"
        assert ctx.flow == []
        assert ctx.errors == []
        assert ctx.extra == {}

    def test_v04_format_with_7_fields_validates(self):
        """v0.4 标准 7 字段数据可 validate。"""
        ctx = DebugContext(
            request_id="req-001",
            flow=["step1", "step2"],
            input={"url": "/api/test"},
            output={"status": 200},
            errors=[{"type": "ValueError", "message": "bad"}],
            exception={"type": "ValueError", "message": "bad", "frames": []},
            runtime={"pid": 1234, "cpu_percent": 10.5},
        )
        assert ctx.request_id == "req-001"
        assert ctx.flow == ["step1", "step2"]
        assert ctx.exception["type"] == "ValueError"
        # 新字段默认值
        assert ctx.trace_id is None
        assert ctx.code_snippets is None
        assert ctx.fault_localization is None

    def test_request_id_is_still_required(self):
        """request_id 仍然是唯一必填字段。"""
        with pytest.raises(ValidationError) as exc_info:
            DebugContext()
        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("request_id",)


# ── 3. unknown field 兼容 ──

class TestUnknownFieldCompatibility:
    """extra='allow' 保证未来 build_debug_context 新增字段不破坏 schema。"""

    def test_unknown_field_accepted(self):
        ctx = DebugContext(
            request_id="r1",
            future_field_1="some value",
            future_field_2={"nested": "data"},
        )
        assert ctx.request_id == "r1"
        # Pydantic v2 extra='allow' 将未知字段存入 __pydantic_extra__
        assert ctx.model_extra is not None
        assert "future_field_1" in ctx.model_extra
        assert ctx.model_extra["future_field_1"] == "some value"

    def test_unknown_field_in_dump(self):
        """未知字段应出现在 model_dump() 中。"""
        ctx = DebugContext(request_id="r1", custom_data=[1, 2, 3])
        dumped = ctx.model_dump()
        assert dumped["custom_data"] == [1, 2, 3]

    def test_unknown_field_excluded_by_exclude_none(self):
        """exclude_none=True 不影响未知字段（非 None 的仍保留）。"""
        ctx = DebugContext(request_id="r1", custom_data="x")
        dumped = ctx.model_dump(exclude_none=True)
        assert dumped["custom_data"] == "x"


# ── 4. model_dump(exclude_none=True) ──

class TestExcludeNoneDump:
    """exclude_none=True 正确排除所有 None 字段，仅保留有值字段。"""

    def test_only_non_none_fields_in_dump(self):
        ctx = DebugContext(
            request_id="r1",
            flow=["a"],
            trace_id="t1",
            code_snippets=[{"file": "x.py", "line": 1}],
        )
        dumped = ctx.model_dump(exclude_none=True)
        # 有值的字段保留
        assert dumped["request_id"] == "r1"
        assert dumped["flow"] == ["a"]
        assert dumped["trace_id"] == "t1"
        assert dumped["code_snippets"] == [{"file": "x.py", "line": 1}]
        # None 字段被排除
        assert "output" not in dumped
        assert "exception" not in dumped
        assert "runtime" not in dumped
        assert "git_blame" not in dumped
        assert "fault_localization" not in dumped

    def test_empty_defaults_included_in_dump(self):
        """非 None 的默认值（空 list/dict）不被 exclude_none 排除。"""
        ctx = DebugContext(request_id="r1")
        dumped = ctx.model_dump(exclude_none=True)
        assert dumped["flow"] == []
        assert dumped["errors"] == []
        assert dumped["extra"] == {}


# ── 5. build_debug_context 完整输出可 validate ──

class TestBuildDebugContextOutputValidates:
    """build_debug_context() 返回的 dict 应能被 DebugContext(**dict) 验证。"""

    def test_full_output_validates(self):
        """模拟 build_debug_context 的完整 20 字段输出。"""
        full_output = {
            "request_id": "trace-001",
            "trace_id": "trace-001",
            "trace_kind": "exception",
            "flow": ["error"],
            "input": None,
            "output": None,
            "errors": [{"type": "ValueError", "message": "bad value"}],
            "exception": {
                "type": "ValueError",
                "message": "bad value",
                "frames": [{"file": "x.py", "line": 10, "function": "f"}],
                "frame_count": 1,
            },
            "source": "test",
            "extra": {},
            "code_snippets": [{"file": "x.py", "error_line": 10, "snippet": "x = 1", "found": True}],
            "static_analysis": {"issues": []},
            "git_blame": None,
            "recent_diffs": None,
            "related_specs": None,
            "network_trace": [{"method": "GET", "url": "http://x", "status_code": 200}],
            "ui_events": [{"event_type": "click", "target_selector": "#btn"}],
            "spec_diffs": None,
            "runtime": {"pid": 1234, "cpu_percent": 5.0, "memory_mb": 100.0},
            "fault_localization": {"method": "stack_heuristic", "suspicious_frames": []},
        }
        ctx = DebugContext(**full_output)
        assert ctx.request_id == "trace-001"
        assert ctx.trace_id == "trace-001"
        assert ctx.trace_kind == "exception"
        assert ctx.code_snippets[0]["file"] == "x.py"
        assert ctx.network_trace[0]["method"] == "GET"
        assert ctx.fault_localization["method"] == "stack_heuristic"

    def test_output_with_none_optionals_validates(self):
        """build_debug_context 中多个字段为 None 时仍可 validate。"""
        output_with_nones = {
            "request_id": "trace-002",
            "trace_id": "trace-002",
            "trace_kind": "exception",
            "flow": ["error"],
            "input": None,
            "output": None,
            "errors": [],
            "exception": {"type": "E", "message": "m", "frames": [], "frame_count": 0},
            "source": None,
            "extra": {},
            "code_snippets": None,
            "static_analysis": None,
            "git_blame": None,
            "recent_diffs": None,
            "related_specs": None,
            "network_trace": None,
            "ui_events": None,
            "spec_diffs": None,
            "runtime": None,
            "fault_localization": None,
        }
        ctx = DebugContext(**output_with_nones)
        assert ctx.request_id == "trace-002"
        assert ctx.code_snippets is None
        assert ctx.runtime is None
