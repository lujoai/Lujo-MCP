"""Benchmark 数据模型 —— MCP Debug Context Quality Benchmark（Phase 3 D6）。

`BenchmarkCase` 定义一次「对照验证」所需的所有数据：
- 用户描述（Without 输入：普通 AI 只能看到的）
- Lujo Debug Context（With 输入：AI 获得 MCP Debug Context 后）
- 标准答案（expected_root_cause / expected_evidence）

设计约束：
- 纯数据模型（dataclass），无 I/O、无副作用、无 LLM 调用。
- `lujo_context` 保持 `build_debug_context` 的返回字段契约（由单元测试校验）。
- 本模块独立于 app/，不污染任何生产 Layer。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class EvaluationMetrics:
    """Benchmark 主评分指标（各 0-10 分，由评估员填入）。

    主评分只衡量「AI 使用 Lujo Context 后的 Debug 能力」，与 QualityScorer 的
    Context 完整度评分是两个独立体系。
    """

    root_cause_accuracy: float = 0.0
    evidence_quality: float = 0.0
    fix_suggestion_quality: float = 0.0
    time_to_resolution: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return {
            "root_cause_accuracy": self.root_cause_accuracy,
            "evidence_quality": self.evidence_quality,
            "fix_suggestion_quality": self.fix_suggestion_quality,
            "time_to_resolution": self.time_to_resolution,
        }


@dataclass(slots=True)
class BenchmarkCase:
    """一个可复现的对照验证用例。"""

    case_id: str
    title: str
    category: str
    user_description: str
    lujo_context: dict[str, Any]
    expected_root_cause: str
    expected_evidence: list[str] = field(default_factory=list)
    evaluation_metrics: EvaluationMetrics = field(default_factory=EvaluationMetrics)

    def without_context(self) -> dict[str, Any]:
        """导出 Without 输入（只有用户描述，无 Lujo Context）。"""
        return {"user_description": self.user_description}

    def with_context(self) -> dict[str, Any]:
        """导出 With 输入（用户描述 + Lujo Debug Context）。"""
        return {
            "user_description": self.user_description,
            "lujo_context": self.lujo_context,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "title": self.title,
            "category": self.category,
            "expected_root_cause": self.expected_root_cause,
            "expected_evidence": self.expected_evidence,
            "evaluation_metrics": self.evaluation_metrics.to_dict(),
        }


__all__ = ["BenchmarkCase", "EvaluationMetrics"]
