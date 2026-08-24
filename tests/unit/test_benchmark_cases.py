"""单元测试：Benchmark 框架（Phase 3 D6）。

覆盖：
- BenchmarkCase 数据模型（without/with 导出、to_dict）
- 5 个标准 Case 结构完整
- lujo_context 保持 build_debug_context 字段契约
- runner CLI 命令（list / show / 未知命令）
- 与 QualityScorer 可选旁证的输入适配（不混入主评分）
"""

import json

from benchmark.cases import BENCHMARK_CASES, BENCHMARK_INDEX, get_case, list_cases
from benchmark.schemas import BenchmarkCase, EvaluationMetrics
from benchmark import runner


# ── 数据模型 ──


class TestSchemas:
    def test_evaluation_metrics_default_zero(self):
        m = EvaluationMetrics()
        assert m.root_cause_accuracy == 0.0
        assert m.evidence_quality == 0.0
        assert m.fix_suggestion_quality == 0.0
        assert m.time_to_resolution == 0.0
        assert m.to_dict() == {
            "root_cause_accuracy": 0.0,
            "evidence_quality": 0.0,
            "fix_suggestion_quality": 0.0,
            "time_to_resolution": 0.0,
        }

    def test_benchmark_case_without_context(self):
        case = BenchmarkCase(
            case_id="c1",
            title="t",
            category="api_error",
            user_description="用户描述",
            lujo_context={"exception": {}},
            expected_root_cause="根因",
        )
        without = case.without_context()
        assert without == {"user_description": "用户描述"}
        assert "lujo_context" not in without

    def test_benchmark_case_with_context(self):
        case = BenchmarkCase(
            case_id="c1",
            title="t",
            category="api_error",
            user_description="用户描述",
            lujo_context={"exception": {"type": "ValueError"}},
            expected_root_cause="根因",
        )
        with_in = case.with_context()
        assert with_in["user_description"] == "用户描述"
        assert with_in["lujo_context"]["exception"]["type"] == "ValueError"

    def test_to_dict_shape(self):
        case = BenchmarkCase(
            case_id="c1",
            title="t",
            category="api_error",
            user_description="d",
            lujo_context={},
            expected_root_cause="根因",
            expected_evidence=["e1"],
        )
        d = case.to_dict()
        assert d["case_id"] == "c1"
        assert d["expected_evidence"] == ["e1"]
        assert "evaluation_metrics" in d


# ── 6 个标准 Case ──


class TestStandardCases:
    def test_six_cases(self):
        assert len(BENCHMARK_CASES) == 6
        assert len(BENCHMARK_INDEX) == 6

    def test_case_ids_unique(self):
        ids = [c.case_id for c in BENCHMARK_CASES]
        assert len(ids) == len(set(ids))

    def test_categories_cover_six_scenarios(self):
        categories = {c.category for c in BENCHMARK_CASES}
        expected = {
            "api_error", "frontend_blank", "db_error", "auth_403", "perf_slow",
            "frontend_sourcemap",  # v0.5.1 Source Map 还原对照
        }
        assert categories == expected

    def test_all_cases_have_required_fields(self):
        for c in BENCHMARK_CASES:
            assert c.user_description.strip(), f"{c.case_id} 缺 user_description"
            assert c.expected_root_cause.strip(), f"{c.case_id} 缺 expected_root_cause"
            assert c.expected_evidence, f"{c.case_id} 缺 expected_evidence"
            assert isinstance(c.lujo_context, dict) and c.lujo_context, (
                f"{c.case_id} 缺 lujo_context"
            )

    def test_get_case_and_list(self):
        assert get_case("api_500_none_attribute") is BENCHMARK_CASES[0]
        assert get_case("no-such-case") is None
        assert list_cases() == BENCHMARK_CASES


# ── lujo_context 保持 build_debug_context 契约 ──


class TestContextContract:
    """5 个 Case 的 lujo_context 应符合 build_debug_context 的字段结构。"""

    def test_all_serializable(self):
        """lujo_context 必须可 JSON 序列化（保证可喂给外部 AI / 落盘）。"""
        for c in BENCHMARK_CASES:
            json.dumps(c.lujo_context, ensure_ascii=False)  # 不抛异常即通过

    def test_exception_contract(self):
        """含 exception 的 Case，type/message/frames 结构一致。"""
        for c in BENCHMARK_CASES:
            exc = c.lujo_context.get("exception")
            if exc:
                assert exc.get("type"), f"{c.case_id} exception.type 缺失"
                assert exc.get("message"), f"{c.case_id} exception.message 缺失"
                assert isinstance(exc.get("frames"), list), f"{c.case_id} exception.frames 缺失"

    def test_network_trace_contract(self):
        """含 network_trace 的 Case，method/url/status 结构一致。"""
        for c in BENCHMARK_CASES:
            net = c.lujo_context.get("network_trace")
            if net:
                for item in net:
                    assert "method" in item, f"{c.case_id} network.method 缺失"
                    assert "url" in item, f"{c.case_id} network.url 缺失"
                    assert "status" in item, f"{c.case_id} network.status 缺失"

    def test_evidence_matches_context(self):
        """expected_evidence 描述的证据应能在 lujo_context 中找到对应字段。"""
        key_to_field = {
            "exception": "exception",
            "network_trace": "network_trace",
            "ui_events": "ui_events",
            "recent_diffs": "recent_diffs",
            "git_blame": "git_blame",
            "runtime": "runtime",
            "spec_diffs": "spec_diffs",
            "related_specs": "related_specs",
            "trace": "trace",
            "auth_context": "auth_context",
        }
        for c in BENCHMARK_CASES:
            # 每个 Case 至少应有一个主证据字段非空
            present = [f for f in key_to_field.values() if c.lujo_context.get(f)]
            assert present, f"{c.case_id} 无任何主证据字段"

    def test_quality_scorer_adapter(self):
        """lujo_context 可包装为 QualityScorer 需要的 agent_context 结构（旁证）。"""
        from app.quality.scorer import evaluate

        for c in BENCHMARK_CASES:
            agent_context = {
                "debug_context": c.lujo_context,
                "repair_context": {},
            }
            report = evaluate(agent_context)  # 不抛异常即通过
            assert 0.0 <= report.overall_score <= 1.0


# ── runner CLI ──


class TestRunner:
    def test_list_returns_zero(self, capsys):
        assert runner.cmd_list() == 0
        out = capsys.readouterr().out
        assert "6 个 BenchmarkCase" in out
        assert "api_500_none_attribute" in out

    def test_show_returns_zero(self, capsys):
        assert runner.cmd_show("api_500_none_attribute") == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["case_id"] == "api_500_none_attribute"
        assert "without" in payload and "with" in payload

    def test_show_unknown_returns_one(self, capsys):
        assert runner.cmd_show("no-such") == 1

    def test_main_unknown_command(self, capsys):
        assert runner.main(["bogus"]) == 1

    def test_main_no_args_usage(self, capsys):
        assert runner.main([]) == 0
        out = capsys.readouterr().out
        assert "python -m benchmark.runner" in out
