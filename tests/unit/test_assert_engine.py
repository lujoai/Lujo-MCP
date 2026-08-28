"""单元测试：断言引擎 assert_engine"""
from app.runtime.verifier.assert_engine import assert_behavior


class TestAssertBehaviorApi:

    def test_match_status_and_body(self):
        actual = {"status_code": 200, "body": {"user": {"name": "Alice"}}}
        spec = {
            "id": "spec-1",
            "kind": "api",
            "target": "GET /api/user",
            "expect": {
                "status": 200,
                "body_rules": {"user.name": "Alice"},
            },
        }
        result = assert_behavior(actual, spec)
        assert result["matched"] is True
        assert result["diffs"] == []
        assert result["silent_failure"] is False
        assert result["spec_id"] == "spec-1"

    def test_status_mismatch(self):
        actual = {"status_code": 500, "body": {}}
        spec = {"kind": "api", "expect": {"status": 200}}
        result = assert_behavior(actual, spec)
        assert result["matched"] is False
        assert result["diffs"] == [{"field": "status_code", "expected": 200, "actual": 500}]
        # 500 是有错误，不是静默失败
        assert result["silent_failure"] is False

    def test_body_field_mismatch(self):
        actual = {"status_code": 200, "body": {"count": 5}}
        spec = {
            "kind": "api",
            "expect": {"body_rules": {"count": 10}},
        }
        result = assert_behavior(actual, spec)
        assert result["matched"] is False
        assert result["diffs"] == [
            {"field": "body.count", "expected": 10, "actual": 5},
        ]

    def test_nested_body_field_mismatch(self):
        actual = {"status_code": 200, "body": {"data": {"items": []}}}
        spec = {
            "kind": "api",
            "expect": {"body_rules": {"data.items.length": 3}},
        }
        result = assert_behavior(actual, spec)
        assert result["matched"] is False
        assert result["diffs"][0]["field"] == "body.data.items.length"
        assert result["diffs"][0]["expected"] == 3
        assert result["diffs"][0]["actual"] is None

    def test_silent_failure_200_but_mismatch(self):
        """返回 200 OK，无异常，但 body 字段不符合预期 → 静默失败"""
        actual = {"status_code": 200, "body": {"success": True, "data": None}}
        spec = {
            "kind": "api",
            "expect": {"body_rules": {"success": False}},
        }
        result = assert_behavior(actual, spec)
        assert result["matched"] is False
        assert result["silent_failure"] is True

    def test_silent_failure_4xx_not_silent(self):
        """4xx 是有错误，不算静默失败"""
        actual = {"status_code": 404, "body": {}}
        spec = {"kind": "api", "expect": {"status": 200}}
        result = assert_behavior(actual, spec)
        assert result["silent_failure"] is False

    def test_silent_failure_with_exception(self):
        """有异常时不算静默失败"""
        actual = {"status_code": 200, "body": {}, "error": "timeout"}
        spec = {"kind": "api", "expect": {"body_rules": {"x": 1}}}
        result = assert_behavior(actual, spec)
        assert result["silent_failure"] is False


class TestAssertBehaviorUi:

    def test_ui_match(self):
        actual = {"state_changes": {"route_change": "/dashboard", "dom_change": ".modal[open]"}}
        spec = {
            "id": "spec-ui-1",
            "kind": "ui",
            "target": "click #submit-btn",
            "expect": {"state_change": {"route_change": "/dashboard", "dom_change": ".modal[open]"}},
        }
        result = assert_behavior(actual, spec)
        assert result["matched"] is True
        assert result["diffs"] == []

    def test_ui_route_mismatch(self):
        actual = {"state_changes": {"route_change": "/login"}}
        spec = {
            "kind": "ui",
            "expect": {"state_change": {"route_change": "/dashboard"}},
        }
        result = assert_behavior(actual, spec)
        assert result["matched"] is False
        assert result["diffs"][0] == {
            "field": "state_change.route_change",
            "expected": "/dashboard",
            "actual": "/login",
        }

    def test_ui_missing_state_change(self):
        """期望的状态变化完全没发生"""
        actual = {"state_changes": {}}
        spec = {
            "kind": "ui",
            "expect": {"state_change": {"route_change": "/next"}},
        }
        result = assert_behavior(actual, spec)
        assert result["matched"] is False
        assert result["diffs"][0]["field"] == "state_change.route_change"
        assert result["diffs"][0]["actual"] is None


class TestAssertBehaviorRule:

    def test_rule_match(self):
        actual = {"latency_ms": 42, "cpu_pct": 60}
        spec = {
            "kind": "rule",
            "expect": {"rules": [
                {"field": "latency_ms", "expected": 42},
                {"field": "cpu_pct", "expected": 60},
            ]},
        }
        result = assert_behavior(actual, spec)
        assert result["matched"] is True

    def test_rule_mismatch(self):
        actual = {"latency_ms": 2000}
        spec = {
            "kind": "rule",
            "expect": {"rules": [{"field": "latency_ms", "expected": 500}]},
        }
        result = assert_behavior(actual, spec)
        assert result["matched"] is False
        assert result["diffs"][0] == {"field": "latency_ms", "expected": 500, "actual": 2000}


class TestEdgeCases:

    def test_empty_spec_expect(self):
        """空 expect，无规则 → matched=True"""
        actual = {"status_code": 200, "body": {}}
        spec = {"kind": "api", "expect": {}}
        result = assert_behavior(actual, spec)
        assert result["matched"] is True
        assert result["diffs"] == []
        assert result["silent_failure"] is False

    def test_empty_actual(self):
        actual = {}
        spec = {"kind": "api", "expect": {"status": 200}}
        result = assert_behavior(actual, spec)
        assert result["matched"] is False
        assert result["diffs"][0]["field"] == "status_code"
        assert result["diffs"][0]["actual"] is None

    def test_trace_id_passthrough(self):
        actual = {"status_code": 200, "body": {}}
        spec = {"kind": "api", "expect": {"status": 200}, "trace_id": "trace-abc"}
        result = assert_behavior(actual, spec)
        assert result["trace_id"] == "trace-abc"

    def test_multiple_diffs(self):
        actual = {"status_code": 201, "body": {"name": "Old", "version": 1}}
        spec = {
            "kind": "api",
            "expect": {
                "status": 200,
                "body_rules": {"name": "New", "version": 2},
            },
        }
        result = assert_behavior(actual, spec)
        assert result["matched"] is False
        assert len(result["diffs"]) == 3
        fields = {d["field"] for d in result["diffs"]}
        assert fields == {"status_code", "body.name", "body.version"}


# ---------------------------------------------------------------------------
# FIX: R7-V1/V2 —— 断言引擎类型边界
# ---------------------------------------------------------------------------


class TestBoolIntBoundary:
    """R7-V1 回归：bool 与 int 数值相等但类型不同，不得判相等。

    旧实现 ``if a == b: return True`` 首行短路（True == 1）→ 期望 True 实际 1
    被判相等 → 漏报（恰是断言引擎要检测的 silent failure 被放过）。
    """

    def test_values_equal_true_vs_int_one_is_false(self):
        from app.runtime.verifier.assert_engine import _values_equal

        assert _values_equal(True, 1) is False
        assert _values_equal(1, True) is False
        assert _values_equal(False, 0) is False
        assert _values_equal(0, False) is False

    def test_values_equal_same_bool_types_still_equal(self):
        from app.runtime.verifier.assert_engine import _values_equal

        assert _values_equal(True, True) is True
        assert _values_equal(False, False) is True

    def test_values_equal_numeric_normalization_preserved(self):
        """既有 P2 归一语义不受影响："1"(str) vs 1(int) 仍相等。"""
        from app.runtime.verifier.assert_engine import _values_equal

        assert _values_equal("1", 1) is True
        assert _values_equal(1, "1") is True
        assert _values_equal("200", 200) is True

    def test_api_expect_true_actual_one_reports_diff(self):
        """端到端：body 布尔期望 True 实际 1 必须产生 diff（修复前漏报）。"""
        actual = {"status_code": 200, "body": {"enabled": 1}}
        spec = {"id": "s1", "kind": "api", "expect": {"body_rules": {"enabled": True}}}
        result = assert_behavior(actual, spec)
        assert result["matched"] is False
        assert result["silent_failure"] is True  # 2xx + 不匹配 → 静默失败（不再漏报）


class TestStringStatusBoundary:
    """R7-V2 回归：str status（JSON 反序列化 "500"）不得误判静默失败。"""

    def test_string_5xx_not_silent_failure(self):
        actual = {"status_code": "500", "body": {"ok": False}}
        spec = {"id": "s1", "kind": "api", "expect": {"status": 200}}
        result = assert_behavior(actual, spec)
        assert result["matched"] is False
        # 5xx 是显式失败而非静默失败（旧实现把 "500" 判成静默失败）
        assert result["silent_failure"] is False

    def test_string_2xx_still_silent_failure(self):
        actual = {"status_code": "200", "body": {"ok": False}}
        spec = {"id": "s1", "kind": "api", "expect": {"status": 404}}
        result = assert_behavior(actual, spec)
        assert result["matched"] is False
        assert result["silent_failure"] is True

    def test_non_numeric_status_falls_back_to_silent(self):
        from app.runtime.verifier.assert_engine import _judge_silent_failure

        assert _judge_silent_failure(False, {"status_code": "abc"}) is True
        assert _judge_silent_failure(False, {"status_code": True}) is True
        assert _judge_silent_failure(False, {"status_code": 503}) is False
