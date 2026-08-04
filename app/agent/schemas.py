"""AI Debug Agent 数据模型 —— 请求/方案/任务的 Pydantic 契约。

与 analyzer.py 的 {root_cause, impact, fix, confidence} 不同，
RepairPlan 聚焦"可执行修复方案"：patch + affected_files + validation_strategy。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class RepairRequest(BaseModel):
    """修复请求：与 AnalyzeRequest 对称，复用 request_id 拉取 trace。"""

    request_id: str = Field(..., description="请求 ID 或 trace_id")
    model: Optional[str] = Field(None, description="指定 LLM 模型，留空回退 settings.agent_model")


class RepairPlan(BaseModel):
    """RepairAgent 输出的结构化修复方案。"""

    patch: str = Field(..., description="具体代码修改方案：文件路径、修改位置、修改前/后片段、动作")
    affected_files: list[str] = Field(
        default_factory=list, description="受影响的文件列表"
    )
    validation_strategy: str = Field(..., description="验证策略：单测/集成测/手动验证步骤")
    risk_assessment: str = Field(..., description="风险评估：副作用、回归风险、影响范围")
    confidence: str = Field("low", description="置信度：high/medium/low")
    rationale: str = Field("", description="修复思路的推理过程")


class Sources(BaseModel):
    """修复方案的信息来源追溯。"""

    vector_recall: list[dict[str, Any]] = Field(
        default_factory=list, description="向量召回的历史相似修复"
    )
    git_context: list[dict[str, Any]] = Field(
        default_factory=list, description="git 近期 diff 上下文"
    )
    knowledge_base_hit: bool = Field(False, description="知识库精确指纹是否命中")


class RepairJob(BaseModel):
    """异步修复任务状态。结构对称 AnalysisQueue 的 job。"""

    job_id: str
    status: str  # pending | running | done | failed
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    created_at: float
    finished_at: Optional[float] = None
