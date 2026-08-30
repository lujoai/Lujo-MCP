"""FaultLocalizer（方案 A V1）单元测试。

覆盖：
- 栈位置排序（无其他信号时栈顶优先）
- suspicious_inputs 命中帧显著加分并排前
- 项目代码帧优先成为 likely_cause_candidate
- 复杂度提示参与加分
- 全三方/特殊帧兜底
- 空 frames / 畸形帧降级
- 结果确定性 + reasons 可解释
- 纯计算约束（无 DB/IO/LLM/RAG/Agent 依赖）→ 模块 import 不触发任何上层
"""

from unittest.mock import patch


from app.runtime.context.fault_localizer import (
    FaultLocalizationResult,
    ScoreContribution,
    SuspiciousFrame,
    _is_project_code,
    localize,
    to_payload,
)
from app.runtime.collectors.static_analyzer import FaultLocation, FunctionInfo


# ── helpers ──


def _frame(file: str, line: int, function: str, code_context: str | None = None) -> dict:
    f = {"file": file, "line": line, "function": function}
    if code_context:
        f["code_context"] = code_context
    return f


def _fault(
    file: str,
    function: str,
    line: int,
    suspicious_inputs: list[dict] | None = None,
    complexity_hints: list[str] | None = None,
    call_chain: list[str] | None = None,
) -> FaultLocation:
    return FaultLocation(
        file=file,
        function=function,
        line_number=line,
        function_info=FunctionInfo(
            name=function,
            file=file,
            line_start=line,
            line_end=line + 1,
            complexity_hints=complexity_hints or [],
        ),
        call_chain=call_chain or [],
        suspicious_inputs=suspicious_inputs or [],
    )


# ── 1. 栈位置排序 ──


class TestStackPosition:
    def test_top_frame_first_when_no_other_signal(self):
        """无静态信号时，栈顶帧分最高。"""
        frames = [
            _frame("app/x.py", 1, "outer"),
            _frame("app/x.py", 2, "middle"),
            _frame("app/x.py", 3, "inner"),
        ]
        with patch(
            "app.runtime.collectors.static_analyzer.analyze", return_value=[]
        ):
            result = localize(frames)
        assert result.suspicious_frames[0].function == "outer"
        assert result.suspicious_frames[0].score > result.suspicious_frames[1].score
        assert result.suspicious_frames[1].score > result.suspicious_frames[2].score

    def test_scores_decrease_with_depth(self):
        """分数随栈深单调递减（同信号条件）。"""
        frames = [
            _frame("app/x.py", 1, f"f{i}") for i in range(5)
        ]
        with patch(
            "app.runtime.collectors.static_analyzer.analyze", return_value=[]
        ):
            result = localize(frames)
        scores = [f.score for f in result.suspicious_frames]
        assert scores == sorted(scores, reverse=True)


# ── 2. suspicious_inputs 加分排序 ──


class TestSuspiciousInput:
    def test_suspicious_input_frame_ranks_above_plain_top_frame(self):
        """命中可疑输入的深层帧应超过无信号栈顶帧。"""
        frames = [
            _frame("app/x.py", 1, "outer"),
            _frame("app/x.py", 2, "handler", "user = db.get(id)"),
        ]
        # static_analyzer 按帧顺序返回：outer 无信号，handler 命中可疑输入
        with patch(
            "app.runtime.collectors.static_analyzer.analyze",
            return_value=[
                _fault("app/x.py", "outer", 1),
                _fault("app/x.py", "handler", 2, suspicious_inputs=[
                    {"variable": "user", "reason": "从 db.get() 获取后未校验 None 直接使用"}
                ]),
            ],
        ):
            result = localize(frames)

        ranked = result.suspicious_frames
        assert ranked[0].function == "handler", "可疑输入帧应排第一"
        assert "suspicious" in " ".join(ranked[0].reasons).lower()
        # 贡献列表可解释
        assert any(c.rule == "suspicious_input" for c in ranked[0].contributions)

    def test_suspicious_reasons_are_explainable(self):
        """每个命中规则都要有 points + reason。"""
        frames = [_frame("app/x.py", 1, "handler")]
        with patch(
            "app.runtime.collectors.static_analyzer.analyze",
            return_value=[
                _fault("app/x.py", "handler", 1, suspicious_inputs=[
                    {"variable": "v", "reason": "reason-a"}
                ])
            ],
        ):
            result = localize(frames)
        frame = result.suspicious_frames[0]
        assert frame.contributions, "必须有贡献记录"
        for c in frame.contributions:
            assert isinstance(c, ScoreContribution)
            assert c.points > 0
            assert c.reason


# ── 3. 项目代码优先 ──


class TestProjectCodePriority:
    def test_project_frame_becomes_likely_cause_candidate(self):
        """项目帧成为 likely_cause_candidate。"""
        frames = [
            _frame("C:/Python312/lib/site-packages/dep/pkg.py", 5, "dep_fn"),
            _frame("app/services/order.py", 42, "create_order"),
        ]
        with patch(
            "app.runtime.collectors.static_analyzer.analyze", return_value=[]
        ):
            result = localize(frames)

        top = result.suspicious_frames[0]
        assert top.function == "create_order"
        assert top.is_project_code is True
        assert top.is_likely_cause is True
        assert result.likely_cause_candidate is not None
        assert "order.py" in result.likely_cause_candidate

    def test_stdlib_path_detection(self):
        """识别 stdlib / venv / site-packages 路径为非项目代码。"""
        assert _is_project_code("app/main.py") is True
        assert _is_project_code("C:/Python312/lib/site-packages/x.py") is False
        assert _is_project_code("app/.venv/lib/y.py") is False
        assert _is_project_code("/usr/local/lib/python3.11/site-packages/z.py") is False

    def test_all_vendor_frames_fallback(self):
        """全三方/特殊帧时仍返回最高分帧兜底，不抛异常。"""
        frames = [
            _frame("<frozen>runpy.py", 1, "<module>"),
            _frame("C:/Python312/lib/site-packages/libx/impl.py", 10, "impl"),
        ]
        with patch(
            "app.runtime.collectors.static_analyzer.analyze", return_value=[]
        ):
            result = localize(frames)
        assert result.suspicious_frames, "必须返回候选"
        assert any(f.is_likely_cause for f in result.suspicious_frames)


# ── 4. 复杂度 ──


class TestComplexity:
    def test_complexity_hint_adds_points(self):
        """复杂度提示参与加分并写入贡献。"""
        frames = [_frame("app/x.py", 1, "busy")]
        with patch(
            "app.runtime.collectors.static_analyzer.analyze",
            return_value=[
                _fault("app/x.py", "busy", 1, complexity_hints=[
                    "high_nesting(5层)", "long_function(120行)"
                ])
            ],
        ):
            result = localize(frames)
        frame = result.suspicious_frames[0]
        assert any(c.rule == "complexity" for c in frame.contributions)


# ── 5. 空/畸形输入降级 ──


class TestDegradation:
    def test_empty_frames_returns_empty_result(self):
        """空 frames → 空结果，不抛异常。"""
        result = localize([])
        assert isinstance(result, FaultLocalizationResult)
        assert result.suspicious_frames == []
        assert result.likely_cause_candidate is None

    def test_malformed_frames_no_exception(self):
        """缺字段/畸形帧 → 不抛异常。"""
        frames = [
            {"file": None, "line": None, "function": None},
            {},
            {"file": "app/x.py"},  # 缺 line/function
        ]
        result = localize(frames)
        assert result.suspicious_frames  # 至少正常降级出候选

    def test_non_numeric_line_degrades_to_zero(self):
        """FIX(v0.7.1-b2-5) 回归：非数值行号帧行号置 0，不炸整个 localize。

        旧实现 int("12x") 抛 ValueError 且帧循环无 try，单帧畸形即让
        localize 整体失败（builder 兜底后整条 trace 的 fault_localization
        置 None，全部候选丢失）。
        """
        frames = [
            {"file": "app/x.py", "line": "12x", "function": "bad"},
            {"file": "app/y.py", "line": 8, "function": "good"},
        ]
        with patch(
            "app.runtime.collectors.static_analyzer.analyze", return_value=[]
        ):
            result = localize(frames)
        assert len(result.suspicious_frames) == 2  # 两帧都完成评分
        by_fn = {f.function: f for f in result.suspicious_frames}
        assert by_fn["bad"].line == 0  # 畸形行号降级为 0
        assert by_fn["good"].line == 8  # 正常帧不受影响

    def test_static_analyzer_failure_degrades_to_position_only(self):
        """静态分析抛异常 → 降级为仅栈位置评分。"""
        frames = [
            _frame("app/x.py", 1, "a"),
            _frame("app/x.py", 2, "b"),
        ]
        with patch(
            "app.runtime.collectors.static_analyzer.analyze",
            side_effect=RuntimeError("boom"),
        ):
            result = localize(frames)
        assert len(result.suspicious_frames) == 2
        assert result.suspicious_frames[0].function == "a"


# ── 6. 结果确定性与 payload ──


class TestPayloadAndDeterminism:
    def test_result_is_deterministic(self):
        """相同输入两次调用结果一致。"""
        frames = [
            _frame("app/a.py", 1, "a"),
            _frame("app/b.py", 2, "b"),
        ]
        with patch(
            "app.runtime.collectors.static_analyzer.analyze", return_value=[]
        ):
            r1 = localize(frames)
            r2 = localize(frames)
        assert [(f.file, f.function, f.score) for f in r1.suspicious_frames] == [
            (f.file, f.function, f.score) for f in r2.suspicious_frames
        ]

    def test_to_payload_shape(self):
        """to_payload 输出结构符合注入约定。"""
        result = FaultLocalizationResult(
            suspicious_frames=[
                SuspiciousFrame(
                    file="app/x.py", function="f", line=1, score=55.0,
                    is_project_code=True, is_likely_cause=True,
                    reasons=["stack_position(+30): ..."],
                    contributions=[ScoreContribution("stack_position", 30, "reason")],
                )
            ],
            likely_cause_candidate="app/x.py:1 in f (score=55)",
        )
        payload = to_payload(result)
        assert payload["method"] == "heuristic_stack_score"
        assert payload["likely_cause_candidate"] == "app/x.py:1 in f (score=55)"
        assert payload["suspicious_frames"][0]["file"] == "app/x.py"
        assert payload["suspicious_frames"][0]["contributions"][0]["rule"] == "stack_position"

    def test_module_import_does_not_touch_upper_layers(self):
        """纯计算约束：在干净进程中 import fault_localizer 不触发 mcp/agent/llm/rag。

        用子进程隔离验证：主测试进程中可能已被其他测试模块（如 test_debug_context）
        加载 app.llm 等上层模块，直接检查 sys.modules 会误报。
        """
        import subprocess
        import sys

        code = (
            "import sys;"
            "import app.runtime.context.fault_localizer;"
            "bad=[m for m in ('app.llm','app.agent','app.rag','app.mcp') if m in sys.modules];"
            "print('loaded:', bad);"
            "sys.exit(1 if bad else 0)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        assert proc.returncode == 0, (
            f"干净进程下 import fault_localizer 不应加载上层模块: {proc.stdout} {proc.stderr}"
        )
