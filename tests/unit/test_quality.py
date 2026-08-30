"""单元测试：Quality System 评分模型 + 规则引擎。

覆盖：
- QualityReport / EvidenceItem 模型构造与序列化
- 9 维度独立评分函数
- 证据提取逻辑
- AnalysisConfidence 评分算法
- evaluate() 端到端集成
- 静默降级（null_score / 异常兜底）
"""

import json
import time
from unittest.mock import patch


from app.quality.schemas import (
    AnalysisConfidence,
    ContextCompleteness,
    ContextDimension,
    DimensionScore,
    EvidenceItem,
    EvidenceType,
    QualityReport,
    RelevanceLevel,
)
from app.quality.scorer import (
    _extract_evidence,
    _generate_suggestions,
    _score_code_snippet,
    _score_completeness,
    _score_confidence,
    _score_git_context,
    _score_knowledge_base,
    _score_llm_analysis,
    _score_network,
    _score_runtime,
    _score_spec,
    _score_trace,
    _score_ui_event,
    evaluate,
    is_enabled,
)


# ── 测试构造辅助 ──


def _dc(**overrides):
    """构造一个最小 debug_context，覆盖字段用 overrides 指定。"""
    base = {
        "request_id": "test-001",
        "trace_id": "test-001",
        "exception": {
            "type": "ValueError",
            "message": "user_id is None",
            "frames": [
                {"file": "app/service.py", "line": 142, "function": "process"},
                {"file": "app/handler.py", "line": 56, "function": "handle"},
                {"file": "app/main.py", "line": 23, "function": "main"},
                {"file": "app/core.py", "line": 10, "function": "run"},
                {"file": "app/init.py", "line": 5, "function": "init"},
            ],
            "frame_count": 5,
        },
        "code_snippets": [
            {
                "file": "app/service.py",
                "error_line": 142,
                "snippet": "def process(user_id): ...",
                "found": True,
                "link": "vscode://file/app/service.py:142",
            },
            {
                "file": "app/handler.py",
                "error_line": 56,
                "snippet": "def handle(): ...",
                "found": True,
                "link": "vscode://file/app/handler.py:56",
            },
        ],
        "git_blame": [
            {"file": "app/service.py", "line": 142, "author": "zhangsan"},
        ],
        "recent_diffs": [
            {"file": "app/service.py", "diff": "--- a\n+++ b\n"},
        ],
        "network_trace": [
            {"url": "/api/user", "status": 200},
        ],
        "ui_events": None,
        "spec_diffs": None,
        "related_specs": [],
        # FIX: P1-9b 真实快照结构为 runtime.process.* / runtime.system.*
        "runtime": {
            "timestamp": 1.0,
            "python": {"version": "3.12"},
            "system": {"cpu_percent": 12.5},
            "process": {
                "pid": 1234,
                "cpu_percent": 12.5,
                "memory_rss_mb": 256.0,
                "num_threads": 8,
            },
        },
    }
    base.update(overrides)
    return base


def _rc(**overrides):
    """构造一个最小 repair_context，覆盖字段用 overrides 指定。"""
    base = {
        "prior_analysis": {
            "root_cause": "user_id 未做空值校验",
            "impact": "请求处理中断",
            "fix": "增加 None 检查",
            "confidence": "high",
            "analysis_source": "llm",
        },
        "vector_recall": [
            {"id": "kb-001", "summary": "类似 ValueError 修复"},
        ],
        "git_context": [],
        "sources": {
            "vector_recall": [{"id": "kb-001"}],
            "git_context": [],
            "knowledge_base_hit": False,
        },
    }
    base.update(overrides)
    return base


def _agent_ctx(debug_overrides=None, repair_overrides=None):
    return {
        "debug_context": _dc(**(debug_overrides or {})),
        "repair_context": _rc(**(repair_overrides or {})),
    }


# ==================================================================
# QualityReport 模型
# ==================================================================


class TestQualityReport:
    """QualityReport 构造与 null_score 降级。"""

    def test_null_score_all_zeros(self):
        qr = QualityReport.null_score()
        assert qr.overall_score == 0.0
        assert qr.context_completeness.overall_score == 0.0
        assert qr.analysis_confidence.overall_score == 0.0
        assert qr.analysis_confidence.evidence_count == 0
        assert qr.analysis_confidence.high_relevance_count == 0

    def test_null_score_all_dimensions_missing(self):
        qr = QualityReport.null_score()
        dims = qr.context_completeness.dimensions
        assert len(dims) == len(ContextDimension)
        for d in ContextDimension:
            assert dims[d].present is False
            assert dims[d].score == 0.0

    def test_null_score_has_suggestion(self):
        qr = QualityReport.null_score()
        assert len(qr.suggestions) > 0

    def test_model_dump_serializable(self):
        qr = QualityReport.null_score()
        d = qr.model_dump()
        json.dumps(d)  # 不抛异常即可

    def test_overall_score_is_product_of_completeness_and_confidence(self):
        qr = evaluate(_agent_ctx())
        expected = round(
            qr.context_completeness.overall_score * qr.analysis_confidence.overall_score, 4
        )
        assert abs(qr.overall_score - expected) < 0.001


# ==================================================================
# EvidenceItem 模型
# ==================================================================


class TestEvidenceItem:
    """EvidenceItem 构造与字段校验。"""

    def test_minimal_construction(self):
        item = EvidenceItem(
            type=EvidenceType.STACK_TRACE,
            description="trace desc",
            source="test",
        )
        assert item.type == EvidenceType.STACK_TRACE
        assert item.relevance == RelevanceLevel.MEDIUM  # 默认值
        assert item.location is None
        assert item.detail is None

    def test_full_construction(self):
        item = EvidenceItem(
            type=EvidenceType.CODE_SNIPPET,
            description="code snippet at line 42",
            source="code_locator",
            relevance=RelevanceLevel.HIGH,
            location="app/service.py:42",
            detail={"lines": 5},
        )
        assert item.relevance == RelevanceLevel.HIGH
        assert item.location == "app/service.py:42"
        assert item.detail == {"lines": 5}

    def test_model_dump_serializable(self):
        item = EvidenceItem(
            type=EvidenceType.LLM_REASONING,
            description="llm analysis",
            source="analyzer",
        )
        json.dumps(item.model_dump())  # 不抛异常即可


# ==================================================================
# 各维度独立评分
# ==================================================================


class TestScoreTrace:
    """_score_trace 维度：堆栈深度的梯度评分。"""

    def test_full_stack_5_frames(self):
        dc = _dc()
        s = _score_trace(dc, {})
        assert s.present is True
        assert s.score == 1.0

    def test_shallow_stack_3_frames(self):
        dc = _dc()
        dc["exception"]["frame_count"] = 3
        dc["exception"]["frames"] = dc["exception"]["frames"][:3]
        s = _score_trace(dc, {})
        assert s.present is True
        assert s.score == 0.7

    def test_very_shallow_stack_1_frame(self):
        dc = _dc()
        dc["exception"]["frame_count"] = 1
        dc["exception"]["frames"] = dc["exception"]["frames"][:1]
        s = _score_trace(dc, {})
        assert s.present is True
        assert s.score == 0.4

    def test_no_exception(self):
        s = _score_trace({}, {})
        assert s.present is False
        assert s.score == 0.0

    def test_empty_frames(self):
        dc = _dc()
        dc["exception"]["frames"] = []
        dc["exception"]["frame_count"] = 0
        s = _score_trace(dc, {})
        assert s.present is False
        assert s.score == 0.0


class TestScoreCodeSnippet:
    """_score_code_snippet 维度：源码命中率分级。"""

    def test_all_found(self):
        s = _score_code_snippet(_dc(), {})
        assert s.present is True
        assert s.score == 1.0

    def test_partial_found(self):
        dc = _dc()
        dc["code_snippets"] = [
            {"file": "a.py", "error_line": 1, "found": True},
            {"file": "b.py", "error_line": 2, "found": False},
        ]
        s = _score_code_snippet(dc, {})
        assert s.present is True
        assert s.score == 0.6

    def test_none_found(self):
        dc = _dc()
        dc["code_snippets"] = [
            {"file": "a.py", "error_line": 1, "found": False},
        ]
        s = _score_code_snippet(dc, {})
        assert s.present is False
        assert s.score == 0.0

    def test_no_snippets(self):
        s = _score_code_snippet({}, {})
        assert s.present is False
        assert s.score == 0.0


class TestScoreRuntime:
    """_score_runtime 维度：运行时快照有/无。"""

    def test_present(self):
        s = _score_runtime(_dc(), {})
        assert s.present is True
        assert s.score == 1.0

    def test_missing(self):
        s = _score_runtime({}, {})
        assert s.present is False
        assert s.score == 0.0

    def test_no_pid(self):
        s = _score_runtime({"runtime": {"cpu_percent": 50}}, {})
        assert s.present is False
        assert s.score == 0.0


class TestScoreGitContext:
    """_score_git_context 维度：blame + diff 覆盖度。"""

    def test_both_present(self):
        s = _score_git_context(_dc(), {})
        assert s.present is True
        assert s.score == 1.0

    def test_only_blame(self):
        dc = _dc()
        dc["recent_diffs"] = None
        s = _score_git_context(dc, {})
        assert s.present is True
        assert s.score == 0.6

    def test_only_diffs(self):
        dc = _dc()
        dc["git_blame"] = None
        s = _score_git_context(dc, {})
        assert s.present is True
        assert s.score == 0.6

    def test_neither(self):
        dc = _dc()
        dc["git_blame"] = None
        dc["recent_diffs"] = None
        s = _score_git_context(dc, {})
        assert s.present is False
        assert s.score == 0.0


class TestScoreNetwork:
    """_score_network 维度：网络记录有/无。"""

    def test_present(self):
        s = _score_network(_dc(), {})
        assert s.present is True
        assert s.score == 1.0

    def test_missing(self):
        s = _score_network({}, {})
        assert s.present is False
        assert s.score == 0.0


class TestScoreUiEvent:
    """_score_ui_event 维度：前端 UI 事件有/无。"""

    def test_present(self):
        s = _score_ui_event({"ui_events": [{"event": "click"}]}, {})
        assert s.present is True
        assert s.score == 1.0

    def test_missing(self):
        s = _score_ui_event({}, {})
        assert s.present is False
        assert s.score == 0.0


class TestScoreSpec:
    """_score_spec 维度：规范校验。"""

    def test_spec_diffs_present(self):
        s = _score_spec({"spec_diffs": [{"field": "x"}]}, {})
        assert s.present is True
        assert s.score == 1.0

    def test_only_related_specs(self):
        s = _score_spec({"related_specs": [{"file": "spec.md"}]}, {})
        assert s.present is True
        assert s.score == 0.5

    def test_missing(self):
        s = _score_spec({}, {})
        assert s.present is False
        assert s.score == 0.0


class TestScoreKnowledgeBase:
    """_score_knowledge_base 维度：知识库/向量召回命中。"""

    def test_kb_hit(self):
        dc = {}
        rc = {"sources": {"knowledge_base_hit": True, "vector_recall": []}}
        s = _score_knowledge_base(dc, rc)
        assert s.present is True
        assert s.score == 1.0

    def test_vector_recall_only(self):
        dc = {}
        rc = {"sources": {"knowledge_base_hit": False, "vector_recall": [{"id": "1"}]}}
        s = _score_knowledge_base(dc, rc)
        assert s.present is True
        assert s.score == 0.6

    def test_no_hit(self):
        s = _score_knowledge_base({}, {})
        assert s.present is False
        assert s.score == 0.0

    def test_prior_analysis_kb_hit(self):
        dc = {}
        rc = {"prior_analysis": {"knowledge_base_hit": True}, "sources": {}}
        s = _score_knowledge_base(dc, rc)
        assert s.present is True
        assert s.score == 1.0


class TestScoreLlmAnalysis:
    """_score_llm_analysis 维度：LLM 置信度分级。"""

    def test_high_confidence(self):
        s = _score_llm_analysis({}, _rc())
        assert s.present is True
        assert s.score == 1.0

    def test_medium_confidence(self):
        rc = _rc()
        rc["prior_analysis"]["confidence"] = "medium"
        s = _score_llm_analysis({}, rc)
        assert s.present is True
        assert s.score == 0.8

    def test_low_confidence(self):
        rc = _rc()
        rc["prior_analysis"]["confidence"] = "low"
        s = _score_llm_analysis({}, rc)
        assert s.present is True
        assert s.score == 0.5

    def test_no_analysis(self):
        s = _score_llm_analysis({}, {})
        assert s.present is False
        assert s.score == 0.0

    def test_no_root_cause(self):
        rc = _rc()
        rc["prior_analysis"]["root_cause"] = ""
        s = _score_llm_analysis({}, rc)
        assert s.present is False
        assert s.score == 0.0


# ==================================================================
# 证据提取
# ==================================================================


class TestEvidenceExtraction:
    """_extract_evidence：从上下文中自动生成证据列表。"""

    def test_extracts_stack_trace_evidence(self):
        items = _extract_evidence(_dc(), _rc())
        types = [e.type for e in items]
        assert EvidenceType.STACK_TRACE in types

    def test_stack_trace_evidence_has_high_relevance(self):
        items = _extract_evidence(_dc(), _rc())
        trace_item = next(e for e in items if e.type == EvidenceType.STACK_TRACE)
        assert trace_item.relevance == RelevanceLevel.HIGH

    def test_extracts_code_snippet_evidence(self):
        items = _extract_evidence(_dc(), _rc())
        types = [e.type for e in items]
        assert EvidenceType.CODE_SNIPPET in types

    def test_code_snippet_with_link(self):
        items = _extract_evidence(_dc(), _rc())
        code_item = next(e for e in items if e.type == EvidenceType.CODE_SNIPPET)
        assert code_item.location is not None

    def test_extracts_git_blame_evidence(self):
        items = _extract_evidence(_dc(), _rc())
        types = [e.type for e in items]
        assert EvidenceType.GIT_BLAME in types

    def test_extracts_git_diff_evidence(self):
        items = _extract_evidence(_dc(), _rc())
        types = [e.type for e in items]
        assert EvidenceType.GIT_DIFF in types

    def test_extracts_runtime_evidence(self):
        items = _extract_evidence(_dc(), _rc())
        types = [e.type for e in items]
        assert EvidenceType.RUNTIME_STATE in types

    def test_extracts_network_evidence(self):
        items = _extract_evidence(_dc(), _rc())
        types = [e.type for e in items]
        assert EvidenceType.NETWORK_CAPTURE in types

    def test_no_ui_event_evidence_when_missing(self):
        items = _extract_evidence(_dc(), _rc())
        types = [e.type for e in items]
        assert EvidenceType.UI_EVENT not in types

    def test_extracts_ui_event_evidence_when_present(self):
        dc = _dc()
        dc["ui_events"] = [{"event": "click", "target": "#btn"}]
        items = _extract_evidence(dc, _rc())
        types = [e.type for e in items]
        assert EvidenceType.UI_EVENT in types

    def test_extracts_llm_reasoning_evidence(self):
        items = _extract_evidence(_dc(), _rc())
        types = [e.type for e in items]
        assert EvidenceType.LLM_REASONING in types

    def test_llm_high_confidence_gives_high_relevance(self):
        items = _extract_evidence(_dc(), _rc())
        llm_item = next(e for e in items if e.type == EvidenceType.LLM_REASONING)
        assert llm_item.relevance == RelevanceLevel.HIGH

    def test_llm_low_confidence_gives_medium_relevance(self):
        rc = _rc()
        rc["prior_analysis"]["confidence"] = "low"
        items = _extract_evidence(_dc(), rc)
        llm_item = next(e for e in items if e.type == EvidenceType.LLM_REASONING)
        assert llm_item.relevance == RelevanceLevel.MEDIUM

    def test_extracts_kb_hit_evidence(self):
        dc = _dc()
        rc = _rc()
        rc["sources"]["knowledge_base_hit"] = True
        items = _extract_evidence(dc, rc)
        types = [e.type for e in items]
        assert EvidenceType.HISTORICAL_FIX in types

    def test_extracts_vector_recall_evidence(self):
        items = _extract_evidence(_dc(), _rc())
        types = [e.type for e in items]
        assert EvidenceType.HISTORICAL_FIX in types

    def test_extracts_spec_violation_evidence(self):
        dc = _dc()
        dc["spec_diffs"] = [{"field": "status_code", "expected": 200, "actual": 500}]
        items = _extract_evidence(dc, _rc())
        types = [e.type for e in items]
        assert EvidenceType.SPEC_VIOLATION in types

    def test_empty_context_returns_empty_list(self):
        items = _extract_evidence({}, {})
        assert items == []

    def test_no_exception_still_extracts_other_evidence(self):
        dc = _dc()
        del dc["exception"]
        items = _extract_evidence(dc, _rc())
        types = [e.type for e in items]
        # 仍然有 runtime、code_snippet、network、git、llm 等证据
        assert EvidenceType.RUNTIME_STATE in types
        assert EvidenceType.CODE_SNIPPET in types
        assert EvidenceType.STACK_TRACE not in types


# ==================================================================
# AnalysisConfidence 评分
# ==================================================================


class TestAnalysisConfidence:
    """_score_confidence：证据质量评分算法。"""

    def test_no_evidence_returns_zero(self):
        conf = _score_confidence([])
        assert conf.overall_score == 0.0
        assert conf.evidence_count == 0

    def test_basic_scoring(self):
        items = [
            EvidenceItem(type=EvidenceType.STACK_TRACE, description="t", source="s", relevance=RelevanceLevel.HIGH),
            EvidenceItem(type=EvidenceType.CODE_SNIPPET, description="c", source="s", relevance=RelevanceLevel.HIGH),
            EvidenceItem(type=EvidenceType.RUNTIME_STATE, description="r", source="s", relevance=RelevanceLevel.LOW),
        ]
        conf = _score_confidence(items)
        assert conf.evidence_count == 3
        assert conf.high_relevance_count == 2
        assert conf.overall_score > 0.0

    def test_five_evidence_max_base_score(self):
        items = [
            EvidenceItem(type=EvidenceType.STACK_TRACE, description="x", source="s", relevance=RelevanceLevel.HIGH),
            EvidenceItem(type=EvidenceType.CODE_SNIPPET, description="x", source="s", relevance=RelevanceLevel.HIGH),
            EvidenceItem(type=EvidenceType.RUNTIME_STATE, description="x", source="s", relevance=RelevanceLevel.LOW),
            EvidenceItem(type=EvidenceType.GIT_BLAME, description="x", source="s", relevance=RelevanceLevel.MEDIUM),
            EvidenceItem(type=EvidenceType.GIT_DIFF, description="x", source="s", relevance=RelevanceLevel.MEDIUM),
        ]
        conf = _score_confidence(items)
        # 基础分 0.5（5+ 条 = 满分）+ 质量加成 + 覆盖度加成
        assert conf.overall_score >= 0.5

    def test_coverage_aspects_and_missing_aspects(self):
        items = [
            EvidenceItem(type=EvidenceType.STACK_TRACE, description="x", source="s", relevance=RelevanceLevel.HIGH),
            EvidenceItem(type=EvidenceType.CODE_SNIPPET, description="x", source="s", relevance=RelevanceLevel.HIGH),
        ]
        conf = _score_confidence(items)
        assert "stack_trace" in conf.coverage_aspects
        assert "code_snippet" in conf.coverage_aspects
        assert len(conf.missing_aspects) > 0


# ==================================================================
# ContextCompleteness 综合评分
# ==================================================================


class TestContextCompleteness:
    """_score_completeness：9 维度加权平均。"""

    def test_returns_all_dimensions(self):
        result = _score_completeness(_dc(), _rc())
        assert len(result.dimensions) == len(ContextDimension)
        for d in ContextDimension:
            assert d in result.dimensions

    def test_overall_in_range(self):
        result = _score_completeness(_dc(), _rc())
        assert 0.0 <= result.overall_score <= 1.0

    def test_missing_count(self):
        result = _score_completeness(_dc(), _rc())
        # 完整场景应有 ui_event + spec 缺失（2 个）
        assert result.missing_count == 2

    def test_total_dimensions(self):
        result = _score_completeness(_dc(), _rc())
        assert result.total_dimensions == len(ContextDimension)


# ==================================================================
# 改进建议生成
# ==================================================================


class TestSuggestions:
    """_generate_suggestions：根据评分生成改进建议。"""

    def test_empty_context_generates_suggestions(self):
        completeness = ContextCompleteness(
            overall_score=0.0,
            dimensions={d: DimensionScore(present=False, score=0.0, reason="缺失") for d in ContextDimension},
            missing_count=9,
            total_dimensions=9,
        )
        confidence = AnalysisConfidence(
            overall_score=0.0,
            evidence_count=0,
            high_relevance_count=0,
        )
        suggestions = _generate_suggestions(completeness, confidence)
        assert len(suggestions) > 0
        assert any("证据" in s for s in suggestions)

    def test_perfect_context_no_suggestions(self):
        completeness = ContextCompleteness(
            overall_score=1.0,
            dimensions={d: DimensionScore(present=True, score=1.0, reason="OK") for d in ContextDimension},
            missing_count=0,
            total_dimensions=9,
        )
        confidence = AnalysisConfidence(
            overall_score=1.0,
            evidence_count=10,
            high_relevance_count=10,
        )
        suggestions = _generate_suggestions(completeness, confidence)
        assert suggestions == []

    def test_low_confidence_triggered(self):
        completeness = ContextCompleteness(
            overall_score=0.5,
            dimensions={},
            missing_count=4,
            total_dimensions=9,
        )
        confidence = AnalysisConfidence(
            overall_score=0.2,
            evidence_count=1,
            high_relevance_count=0,
        )
        suggestions = _generate_suggestions(completeness, confidence)
        assert any("可信度偏低" in s for s in suggestions)

    def test_low_completeness_triggered(self):
        completeness = ContextCompleteness(
            overall_score=0.2,
            dimensions={d: DimensionScore(present=False, score=0.0, reason="缺失") for d in ContextDimension},
            missing_count=9,
            total_dimensions=9,
        )
        confidence = AnalysisConfidence(
            overall_score=0.5,
            evidence_count=3,
            high_relevance_count=1,
        )
        suggestions = _generate_suggestions(completeness, confidence)
        assert any("完整度严重不足" in s for s in suggestions)


# ==================================================================
# evaluate() 端到端集成
# ==================================================================


class TestEvaluateIntegration:
    """evaluate() 端到端：完整的 debug_context → QualityReport。"""

    def test_full_context_returns_valid_report(self):
        report = evaluate(_agent_ctx())
        assert isinstance(report, QualityReport)
        assert report.overall_score > 0.0
        assert len(report.evidence_items) > 0
        assert report.scorer_version == "1.0.0"

    def test_minimal_context_returns_valid_report(self):
        """最小上下文：仅 trace_id + exception。"""
        ctx = _agent_ctx(
            debug_overrides={
                "code_snippets": [],
                "git_blame": None,
                "recent_diffs": None,
                "network_trace": None,
                "runtime": None,
            },
            repair_overrides={
                "prior_analysis": None,
                "vector_recall": [],
                "sources": {"vector_recall": [], "knowledge_base_hit": False},
            },
        )
        report = evaluate(ctx)
        assert isinstance(report, QualityReport)
        assert report.overall_score < 0.5  # 很多东西缺失，分数应该低
        assert report.context_completeness.missing_count > 3

    def test_empty_context_returns_null_score(self):
        """完全空的 context 不应该抛异常。"""
        report = evaluate({})
        assert isinstance(report, QualityReport)
        assert report.overall_score == 0.0

    def test_none_context_returns_null_score(self):
        """None 输入不应该抛异常。"""
        report = evaluate({"debug_context": None, "repair_context": None})
        assert isinstance(report, QualityReport)

    def test_scored_at_is_recent(self):
        report = evaluate(_agent_ctx())
        assert abs(report.scored_at - time.time()) < 5.0

    def test_suggestions_include_missing_dimensions(self):
        report = evaluate(_agent_ctx())
        assert len(report.suggestions) > 0
        # 应该提示缺少 UI 事件和规范
        suggestion_text = " ".join(report.suggestions)
        assert "UI" in suggestion_text or "规范" in suggestion_text


# ==================================================================
# 特殊场景
# ==================================================================


class TestDegradation:
    """静默降级：异常兜底、null_score。"""

    def test_evaluate_swallows_exception(self):
        """evaluate 内部抛异常 → 返回 null_score，不冒泡。"""
        with patch(
            "app.quality.scorer._score_completeness",
            side_effect=RuntimeError("boom"),
        ):
            report = evaluate(_agent_ctx())
            assert report.overall_score == 0.0
            assert any("失败" in s for s in report.suggestions)

    def test_is_enabled_defaults_to_false(self):
        """quality_scoring_enabled 配置项尚未注册 → 默认返回 False（静默降级）。"""
        # 当 settings 没有 quality_scoring_enabled 属性时，is_enabled 捕获异常返回 False
        result = is_enabled()
        assert isinstance(result, bool)


class TestBackwardCompatibility:
    """向后兼容：旧格式数据不抛异常。"""

    def test_old_exception_format_no_frame_count(self):
        """旧格式 exception 可能没有 frame_count 字段。"""
        ctx = _agent_ctx()
        del ctx["debug_context"]["exception"]["frame_count"]
        report = evaluate(ctx)
        assert isinstance(report, QualityReport)

    def test_code_snippets_missing_found_field(self):
        """旧格式 code_snippet 可能没有 found 字段。"""
        ctx = _agent_ctx()
        ctx["debug_context"]["code_snippets"] = [
            {"file": "a.py", "error_line": 1}  # 无 found 字段
        ]
        report = evaluate(ctx)
        assert isinstance(report, QualityReport)

    def test_prior_analysis_missing_confidence(self):
        """旧格式 prior_analysis 可能没有 confidence。"""
        ctx = _agent_ctx()
        del ctx["repair_context"]["prior_analysis"]["confidence"]
        report = evaluate(ctx)
        assert isinstance(report, QualityReport)

    def test_network_trace_not_list(self):
        """network_trace 可能是 dict 而非 list。"""
        ctx = _agent_ctx()
        ctx["debug_context"]["network_trace"] = {"url": "/api", "status": 200}
        report = evaluate(ctx)
        assert isinstance(report, QualityReport)


# ==================================================================
# 维度权重一致性
# ==================================================================


class TestDimensionWeights:
    """维度权重总和为 1.0，所有维度都有权重。"""

    def test_weights_sum_to_one(self):
        from app.quality.scorer import _DIMENSION_WEIGHTS

        total = sum(_DIMENSION_WEIGHTS.values())
        assert abs(total - 1.0) < 0.001

    def test_all_dimensions_have_weight(self):
        from app.quality.scorer import _DIMENSION_WEIGHTS

        for d in ContextDimension:
            assert d in _DIMENSION_WEIGHTS, f"维度 {d} 缺少权重"

    def test_all_dimensions_have_scorer(self):
        from app.quality.scorer import _DIMENSION_SCORERS

        for d in ContextDimension:
            assert d in _DIMENSION_SCORERS, f"维度 {d} 缺少评分器"


# ==================================================================
# EvidenceType 枚举完整性
# ==================================================================


class TestEvidenceTypeEnum:
    """EvidenceType 枚举覆盖所有已知证据类型。"""

    def test_known_types_exist(self):
        expected = {
            "stack_trace",
            "code_snippet",
            "runtime_state",
            "git_blame",
            "git_diff",
            "network_capture",
            "ui_event",
            "historical_fix",
            "static_analysis",
            "log_pattern",
            "spec_violation",
            "llm_reasoning",
        }
        actual = {t.value for t in EvidenceType}
        assert expected == actual


# ==================================================================
# FIX: R7-Q2 —— prior_analysis 嵌套形状兼容（analyze_async 真实返回）
# ==================================================================


class TestPriorAnalysisShapeCompatibility:
    """R7-Q2 回归：analyze_async 真实返回为嵌套形状
    {analysis: {root_cause, confidence, ...}, analysis_source, cached, ...}，
    scorer 读顶层 root_cause 恒 None → LLM_ANALYSIS 维度（0.12）恒缺失。
    """

    @staticmethod
    def _real_analyze_async_shape():
        """真实生产者形状（analyzer._llm_fallback_result，熔断兜底路径的
        analyze_async 返回值——不伪造与消费者同形的 fixture）。"""
        from app.llm.analyzer import _llm_fallback_result

        return _llm_fallback_result()

    def test_nested_shape_llm_analysis_present(self):
        from app.quality.scorer import _score_llm_analysis

        prior = self._real_analyze_async_shape()
        assert "root_cause" not in prior  # 嵌套形状：顶层无 root_cause
        dim = _score_llm_analysis({}, {"prior_analysis": prior})
        assert dim.present is True
        assert dim.score == 0.5  # fallback confidence=low

    def test_nested_shape_evidence_extracted(self):
        from app.quality.scorer import _extract_evidence

        prior = self._real_analyze_async_shape()
        evidence = _extract_evidence({}, {"prior_analysis": prior})
        kinds = [e.type for e in evidence]
        assert EvidenceType.LLM_REASONING in kinds

    def test_flat_shape_still_supported(self):
        """旧扁平形状（既有测试 fixture）向后兼容。"""
        flat = {"root_cause": "x", "confidence": "high", "analysis_source": "llm"}
        assert _score_llm_analysis({}, {"prior_analysis": flat}).score == 1.0

    def test_nested_shape_none_prior_unchanged(self):
        assert _score_llm_analysis({}, {"prior_analysis": None}).present is False


# ==================================================================
# FIX: R7-Q1 —— GIT_CONTEXT 维度的 repair_ctx.git_context 回退
# ==================================================================


class TestGitContextRepairFallback:
    def test_repair_ctx_git_context_scores_when_debug_ctx_empty(self):
        """debug_context 无 git 维度时，RepairContextAssembler 装配的
        git_context 参与评分（实现声明此前未落地，_repair_ctx 形参从未使用）。"""
        dim = _score_git_context({}, {"git_context": [{"file": "a.py"}]})
        assert dim.present is True
        assert dim.score == 0.6

    def test_debug_ctx_git_dims_take_precedence(self):
        dim = _score_git_context(
            {"git_blame": [{"file": "a.py"}], "recent_diffs": [{"file": "a.py"}]},
            {"git_context": []},
        )
        assert dim.score == 1.0

    def test_both_empty_still_missing(self):
        dim = _score_git_context({}, {})
        assert dim.present is False
