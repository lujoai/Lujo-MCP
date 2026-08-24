"""Quality System 数据模型 —— 量化 Debug Context 完整度与分析可信度。

ContextCompleteness: 衡量采集到的调试上下文各维度是否齐全
AnalysisConfidence:  衡量已有证据对根因推断的支持强度
QualityReport:       完整质量报告，由 QualityScorer 规则引擎生成
EvidenceItem:        单条证据，由 LLM 分析增强模块输出

设计原则：
- 所有评分字段使用 0.0~1.0 float，便于计算和展示
- 所有模型向后兼容，新字段可选
- 评分失败时 QualityReport 中 score 为 null，不阻断主流程
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── 枚举 ──


class EvidenceType(str, Enum):
    """证据类型枚举，标识证据来源维度。"""

    STACK_TRACE = "stack_trace"
    CODE_SNIPPET = "code_snippet"
    RUNTIME_STATE = "runtime_state"
    GIT_BLAME = "git_blame"
    GIT_DIFF = "git_diff"
    NETWORK_CAPTURE = "network_capture"
    UI_EVENT = "ui_event"
    HISTORICAL_FIX = "historical_fix"
    STATIC_ANALYSIS = "static_analysis"
    LOG_PATTERN = "log_pattern"
    SPEC_VIOLATION = "spec_violation"
    LLM_REASONING = "llm_reasoning"


class RelevanceLevel(str, Enum):
    """证据相关度等级。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ContextDimension(str, Enum):
    """调试上下文维度枚举，用于 ContextCompleteness 的维度评分。"""

    TRACE = "trace"
    RUNTIME = "runtime"
    CODE_SNIPPET = "code_snippet"
    GIT_CONTEXT = "git_context"
    NETWORK = "network"
    UI_EVENT = "ui_event"
    SPEC = "spec"
    KNOWLEDGE_BASE = "knowledge_base"
    LLM_ANALYSIS = "llm_analysis"


# ── 证据模型 ──


class EvidenceItem(BaseModel):
    """单条证据条目。

    由 LLM 分析增强模块（Task 5, QUA-005）在分析过程中提取，
    或由 QualityScorer 规则引擎（Task 4, QUA-004）从上下文中自动识别。

    每条证据记录：
    - 来源（哪个维度采集到的）
    - 具体内容描述
    - 对根因推断的支持强度
    - 可选的详细信息（如代码行号、文件路径等）
    """

    type: EvidenceType = Field(..., description="证据类型，标识来源维度")
    description: str = Field(..., description="证据内容描述，人类可读")
    source: str = Field(..., description="证据来源标识，如模块名/文件路径/函数名")
    relevance: RelevanceLevel = Field(
        RelevanceLevel.MEDIUM, description="对根因推断的相关度"
    )
    location: Optional[str] = Field(
        None, description="证据关联的代码位置，如 'app/llm/analyzer.py:315'"
    )
    detail: Optional[dict[str, Any]] = Field(
        None, description="证据的额外结构化信息"
    )


# ── 评分模型 ──


class DimensionScore(BaseModel):
    """单个维度的质量评分。"""

    present: bool = Field(..., description="该维度数据是否存在")
    score: float = Field(
        ..., ge=0.0, le=1.0, description="该维度质量评分 0.0~1.0"
    )
    reason: Optional[str] = Field(
        None, description="评分说明，如缺失原因或质量评估"
    )


class ContextCompleteness(BaseModel):
    """上下文完整度评分。

    衡量 Debug Context 各维度采集是否齐全。
    由 QualityScorer 规则引擎基于 AgentContext 中的已有字段判定。
    """

    overall_score: float = Field(
        ..., ge=0.0, le=1.0, description="整体完整度，各维度加权平均"
    )
    dimensions: dict[ContextDimension, DimensionScore] = Field(
        default_factory=dict, description="各维度独立评分"
    )
    missing_count: int = Field(..., ge=0, description="缺失维度数量")
    total_dimensions: int = Field(..., ge=1, description="总维度数")


class AnalysisConfidence(BaseModel):
    """分析可信度评分。

    衡量已有证据对根因推断的支持强度。
    由 QualityScorer 规则引擎基于证据数量、相关度、覆盖度综合评定。
    """

    overall_score: float = Field(
        ..., ge=0.0, le=1.0, description="整体可信度评分"
    )
    evidence_count: int = Field(..., ge=0, description="证据条目总数")
    high_relevance_count: int = Field(
        ..., ge=0, description="高相关度证据数"
    )
    coverage_aspects: list[str] = Field(
        default_factory=list, description="已覆盖的分析维度列表"
    )
    missing_aspects: list[str] = Field(
        default_factory=list, description="缺失的分析维度建议"
    )


class QualityReport(BaseModel):
    """完整质量报告 —— QualityScorer 规则引擎的输出。

    包含上下文完整度 + 分析可信度 + 综合评分 + 改进建议。
    在 build_debug_context() 返回前注入 AgentContext。
    """

    context_completeness: ContextCompleteness = Field(
        ..., description="上下文完整度评分"
    )
    analysis_confidence: AnalysisConfidence = Field(
        ..., description="分析可信度评分"
    )
    overall_score: float = Field(
        ..., ge=0.0, le=1.0, description="综合评分 = 完整度 × 可信度"
    )
    evidence_items: list[EvidenceItem] = Field(
        default_factory=list, description="支持当前分析的所有证据列表"
    )
    suggestions: list[str] = Field(
        default_factory=list, description="改进建议，如缺少哪些维度可提升评分"
    )
    scored_at: float = Field(
        ..., description="评分时间戳 (Unix 时间戳)"
    )
    scorer_version: str = Field(
        "1.0.0", description="评分器版本，用于向前兼容"
    )

    @classmethod
    def null_score(cls) -> "QualityReport":
        """评分失败时的降级报告 —— 全零分 + 兜底说明。

        调用方无需判空，直接读取字段即可。
        """
        null_dim = DimensionScore(present=False, score=0.0, reason="评分失败，降级为 null")
        dims = {d: null_dim for d in ContextDimension}
        return cls(
            context_completeness=ContextCompleteness(
                overall_score=0.0,
                dimensions=dims,
                missing_count=len(ContextDimension),
                total_dimensions=len(ContextDimension),
            ),
            analysis_confidence=AnalysisConfidence(
                overall_score=0.0,
                evidence_count=0,
                high_relevance_count=0,
            ),
            overall_score=0.0,
            suggestions=["QualityScorer 评分失败，请检查日志排查原因"],
            scored_at=time.time(),
        )
