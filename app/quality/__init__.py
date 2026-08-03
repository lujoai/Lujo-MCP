"""Quality System —— 调试上下文质量量化评分。

模块结构：
- schemas.py: 数据模型（ContextCompleteness / AnalysisConfidence / QualityReport / EvidenceItem）
- scorer.py:  QualityScorer 规则引擎（纯函数，无副作用）

feature flag:
- settings.quality_scoring_enabled（默认 True），关闭时 scorer 不执行，零行为变更
"""

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
from app.quality.scorer import evaluate, is_enabled

__all__ = [
    "AnalysisConfidence",
    "ContextCompleteness",
    "ContextDimension",
    "DimensionScore",
    "EvidenceItem",
    "EvidenceType",
    "QualityReport",
    "RelevanceLevel",
    "evaluate",
    "is_enabled",
]