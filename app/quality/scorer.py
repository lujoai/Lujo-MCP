"""QualityScorer 规则引擎 —— 纯函数，无副作用。

输入：AgentContext（debug_context + repair_context）
输出：QualityReport（包含 ContextCompleteness + AnalysisConfidence + evidence_items）

评分逻辑：
- ContextCompleteness：逐维度检查数据是否存在，加权平均
- AnalysisConfidence：基于证据数量、相关度、覆盖度综合评定
- overall_score = completeness × confidence（乘法，隐含"数据再全，证据不相关也白搭"）

设计约束（v0.4.0）：
- 纯函数，无 I/O、无副作用
- 评分失败返回 QualityReport.null_score()，不抛异常
- 通过 feature flag（quality_scoring_enabled）控制
"""

from __future__ import annotations

import logging
import time
from typing import Any

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

logger = logging.getLogger("ai-debug-mcp.quality.scorer")


# ── 维度权重（总和为 1.0） ──
# TRACE + CODE_SNIPPET 权重最高——报错场景最核心的两个维度
# RUNTIME / GIT_CONTEXT / LLM_ANALYSIS 次之——辅助定位
# NETWORK / UI_EVENT / SPEC / KNOWLEDGE_BASE 辅助——特定场景才存在
_DIMENSION_WEIGHTS: dict[ContextDimension, float] = {
    ContextDimension.TRACE: 0.20,
    ContextDimension.CODE_SNIPPET: 0.20,
    ContextDimension.RUNTIME: 0.10,
    ContextDimension.GIT_CONTEXT: 0.12,
    ContextDimension.LLM_ANALYSIS: 0.12,
    ContextDimension.NETWORK: 0.08,
    ContextDimension.UI_EVENT: 0.08,
    ContextDimension.SPEC: 0.05,
    ContextDimension.KNOWLEDGE_BASE: 0.05,
}


# ── 公共入口 ──


def evaluate(agent_context: dict[str, Any]) -> QualityReport:
    """评估 Agent 上下文的调试质量，返回 QualityReport。

    内部调用各维度评分函数，失败时返回 null_score() 静默降级。
    """
    try:
        debug_ctx = agent_context.get("debug_context") or {}
        repair_ctx = agent_context.get("repair_context") or {}

        completeness = _score_completeness(debug_ctx, repair_ctx)
        evidence_items = _extract_evidence(debug_ctx, repair_ctx)
        confidence = _score_confidence(evidence_items, completeness)
        overall = round(completeness.overall_score * confidence.overall_score, 4)
        suggestions = _generate_suggestions(completeness, confidence)

        return QualityReport(
            context_completeness=completeness,
            analysis_confidence=confidence,
            overall_score=overall,
            evidence_items=evidence_items,
            suggestions=suggestions,
            scored_at=time.time(),
        )
    except Exception:
        logger.warning("QualityScorer 评分失败，降级为 null_score", exc_info=True)
        return QualityReport.null_score()


def is_enabled() -> bool:
    """feature flag 检查：quality_scoring_enabled。"""
    try:
        from app.config import settings

        return settings.quality_scoring_enabled
    except Exception:
        return False


# ── ContextCompleteness 评分 ──


def _score_completeness(
    debug_ctx: dict[str, Any], repair_ctx: dict[str, Any]
) -> ContextCompleteness:
    """逐维度评分，加权平均计算整体完整度。"""
    dimensions: dict[ContextDimension, DimensionScore] = {}
    weighted_sum = 0.0
    missing_count = 0

    for dim in ContextDimension:
        scorer = _DIMENSION_SCORERS.get(dim)
        score = scorer(debug_ctx, repair_ctx) if scorer else _score_none(dim)
        dimensions[dim] = score
        weighted_sum += score.score * _DIMENSION_WEIGHTS.get(dim, 0.0)
        if not score.present:
            missing_count += 1

    return ContextCompleteness(
        overall_score=round(weighted_sum, 4),
        dimensions=dimensions,
        missing_count=missing_count,
        total_dimensions=len(ContextDimension),
    )


# ── 各维度评分函数 ──


def _score_trace(debug_ctx: dict[str, Any], _repair_ctx: dict[str, Any]) -> DimensionScore:
    """TRACE 维度：异常 + 堆栈帧是否存在。"""
    exc = debug_ctx.get("exception")
    if not exc or not isinstance(exc, dict):
        return DimensionScore(present=False, score=0.0, reason="无异常信息")
    frames = exc.get("frames") or []
    frame_count = exc.get("frame_count", len(frames))
    if frame_count == 0:
        return DimensionScore(present=False, score=0.0, reason="异常存在但无堆栈帧")
    if frame_count >= 5:
        return DimensionScore(present=True, score=1.0, reason=f"堆栈完整，{frame_count} 帧")
    if frame_count >= 2:
        return DimensionScore(present=True, score=0.7, reason=f"堆栈较浅，仅 {frame_count} 帧，可能丢失调用链")
    return DimensionScore(present=True, score=0.4, reason=f"堆栈过浅，仅 {frame_count} 帧，定位困难")


def _score_code_snippet(debug_ctx: dict[str, Any], _repair_ctx: dict[str, Any]) -> DimensionScore:
    """CODE_SNIPPET 维度：源码片段是否成功采集。"""
    snippets = debug_ctx.get("code_snippets") or []
    if not snippets:
        return DimensionScore(present=False, score=0.0, reason="无源码片段")
    found = sum(1 for s in snippets if s.get("found"))
    total = len(snippets)
    if found == total:
        return DimensionScore(present=True, score=1.0, reason=f"全部 {total} 帧源码已定位")
    if found > 0:
        return DimensionScore(
            present=True, score=0.6, reason=f"{found}/{total} 帧源码已定位，{total - found} 帧未找到"
        )
    return DimensionScore(present=False, score=0.0, reason=f"全部 {total} 帧源码均未找到，检查路径映射")


def _score_runtime(debug_ctx: dict[str, Any], _repair_ctx: dict[str, Any]) -> DimensionScore:
    """RUNTIME 维度：运行时快照是否存在。"""
    runtime = debug_ctx.get("runtime")
    # FIX: P1-9b 真实快照结构为 runtime.process.pid / runtime.system.*，
    # 原实现读 runtime.get("pid") 恒为空，导致 RUNTIME 维度恒 0 分。
    process = (runtime or {}).get("process") if isinstance(runtime, dict) else None
    if process and isinstance(process, dict) and process.get("pid"):
        return DimensionScore(present=True, score=1.0, reason="运行时快照已采集")
    return DimensionScore(present=False, score=0.0, reason="无运行时快照（可能采集已关闭）")


def _score_git_context(debug_ctx: dict[str, Any], _repair_ctx: dict[str, Any]) -> DimensionScore:
    """GIT_CONTEXT 维度：git blame + recent diffs 是否可用。"""
    blame = debug_ctx.get("git_blame") or []
    diffs = debug_ctx.get("recent_diffs") or []
    # 同时也检查 repair_ctx 中的 git_context（由 RepairContextAssembler 装配）
    has_blame = bool(blame)
    has_diffs = bool(diffs)
    if has_blame and has_diffs:
        return DimensionScore(present=True, score=1.0, reason="git blame + recent diff 均可用")
    if has_blame:
        return DimensionScore(present=True, score=0.6, reason="仅 git blame 可用，缺少 recent diff")
    if has_diffs:
        return DimensionScore(present=True, score=0.6, reason="仅 recent diff 可用，缺少 git blame")
    return DimensionScore(present=False, score=0.0, reason="git 上下文不可用（非 git 仓库或路径不在白名单）")


def _score_network(debug_ctx: dict[str, Any], _repair_ctx: dict[str, Any]) -> DimensionScore:
    """NETWORK 维度：网络请求记录是否存在。"""
    network = debug_ctx.get("network_trace")
    if network:
        count = len(network) if isinstance(network, list) else 1
        return DimensionScore(present=True, score=1.0, reason=f"已采集 {count} 条网络请求")
    return DimensionScore(present=False, score=0.0, reason="无网络请求记录（非 HTTP 场景或采集已关闭）")


def _score_ui_event(debug_ctx: dict[str, Any], _repair_ctx: dict[str, Any]) -> DimensionScore:
    """UI_EVENT 维度：前端 UI 事件是否存在。"""
    ui_events = debug_ctx.get("ui_events")
    if ui_events:
        count = len(ui_events) if isinstance(ui_events, list) else 1
        return DimensionScore(present=True, score=1.0, reason=f"已采集 {count} 条 UI 事件")
    return DimensionScore(present=False, score=0.0, reason="无 UI 事件（非前端场景或 Browser SDK 未启用）")


def _score_spec(debug_ctx: dict[str, Any], _repair_ctx: dict[str, Any]) -> DimensionScore:
    """SPEC 维度：规范校验结果是否存在。"""
    spec_diffs = debug_ctx.get("spec_diffs")
    related_specs = debug_ctx.get("related_specs") or []
    if spec_diffs:
        return DimensionScore(present=True, score=1.0, reason="规范校验已执行，diff 可用")
    if related_specs:
        return DimensionScore(present=True, score=0.5, reason="仅有相关规范引用，未执行校验")
    return DimensionScore(present=False, score=0.0, reason="无规范信息（SDD 闭环未启用）")


def _score_knowledge_base(debug_ctx: dict[str, Any], repair_ctx: dict[str, Any]) -> DimensionScore:
    """KNOWLEDGE_BASE 维度：知识库/向量召回是否命中。"""
    sources = repair_ctx.get("sources") or {}
    prior = repair_ctx.get("prior_analysis") or {}
    vector_recall = sources.get("vector_recall") or []
    kb_hit = sources.get("knowledge_base_hit", False) or prior.get("knowledge_base_hit", False)
    if kb_hit:
        return DimensionScore(present=True, score=1.0, reason="知识库精确命中，复用历史修复结论")
    if vector_recall and len(vector_recall) > 0:
        return DimensionScore(present=True, score=0.6, reason=f"向量召回 {len(vector_recall)} 条相似案例")
    return DimensionScore(present=False, score=0.0, reason="知识库/向量召回均未命中")


def _score_llm_analysis(debug_ctx: dict[str, Any], repair_ctx: dict[str, Any]) -> DimensionScore:
    """LLM_ANALYSIS 维度：先验 LLM 分析是否可用。"""
    prior = repair_ctx.get("prior_analysis") or {}
    if prior and prior.get("root_cause"):
        conf = prior.get("confidence", "low")
        src = prior.get("analysis_source", "unknown")
        if conf == "high":
            return DimensionScore(present=True, score=1.0, reason=f"LLM 分析完成（置信度 high，来源 {src}）")
        if conf == "medium":
            return DimensionScore(present=True, score=0.8, reason=f"LLM 分析完成（置信度 medium，来源 {src}）")
        return DimensionScore(present=True, score=0.5, reason=f"LLM 分析完成（置信度 low，来源 {src}）")
    return DimensionScore(present=False, score=0.0, reason="LLM 先验分析不可用（未启用或调用失败）")


def _score_none(dim: ContextDimension) -> DimensionScore:
    """未注册评分器的维度，兜底。"""
    return DimensionScore(present=False, score=0.0, reason=f"维度 {dim.value} 未配置评分器")


# 维度 → 评分函数 注册表
_DIMENSION_SCORERS: dict[ContextDimension, Any] = {
    ContextDimension.TRACE: _score_trace,
    ContextDimension.CODE_SNIPPET: _score_code_snippet,
    ContextDimension.RUNTIME: _score_runtime,
    ContextDimension.GIT_CONTEXT: _score_git_context,
    ContextDimension.NETWORK: _score_network,
    ContextDimension.UI_EVENT: _score_ui_event,
    ContextDimension.SPEC: _score_spec,
    ContextDimension.KNOWLEDGE_BASE: _score_knowledge_base,
    ContextDimension.LLM_ANALYSIS: _score_llm_analysis,
}


# ── 证据提取 ──


def _extract_evidence(
    debug_ctx: dict[str, Any], repair_ctx: dict[str, Any]
) -> list[EvidenceItem]:
    """从 debug_context + repair_context 中提取证据列表。

    覆盖所有已存在的维度，自动生成 EvidenceItem。
    后续 Task 5 (LLM 分析增强) 会产出 reasoning_chain + evidence_items，
    与本函数合并后注入 QualityReport。
    """
    evidence: list[EvidenceItem] = []

    # 1. 堆栈证据
    exc = debug_ctx.get("exception") or {}
    frames = exc.get("frames") or []
    if frames:
        top_frame = frames[0]
        evidence.append(
            EvidenceItem(
                type=EvidenceType.STACK_TRACE,
                description=f"异常 {exc.get('type', 'Unknown')} 发生在 {top_frame.get('function', '?')}（{top_frame.get('file', '?')}:{top_frame.get('line', '?')}）",
                source="build_debug_context",
                relevance=RelevanceLevel.HIGH,
                location=f"{top_frame.get('file', '')}:{top_frame.get('line', '')}",
                detail={"frame_count": len(frames), "exc_type": exc.get("type")},
            )
        )

    # 2. 源码证据
    snippets = debug_ctx.get("code_snippets") or []
    for s in snippets:
        if s.get("found"):
            evidence.append(
                EvidenceItem(
                    type=EvidenceType.CODE_SNIPPET,
                    description=f"源码片段：{s.get('file', '?')} 第 {s.get('error_line', '?')} 行附近",
                    source="code_locator",
                    relevance=RelevanceLevel.HIGH,
                    location=s.get("link"),
                )
            )

    # 3. git 证据
    git_blame = debug_ctx.get("git_blame") or []
    for b in git_blame:
        evidence.append(
            EvidenceItem(
                type=EvidenceType.GIT_BLAME,
                description=f"git blame: {b.get('file', '?')}:{b.get('line', '?')} 最后修改者 {b.get('author', '?')}",
                source="git",
                relevance=RelevanceLevel.MEDIUM,
                location=f"{b.get('file', '')}:{b.get('line', '')}",
            )
        )
    diffs = debug_ctx.get("recent_diffs") or []
    if diffs:
        evidence.append(
            EvidenceItem(
                type=EvidenceType.GIT_DIFF,
                description=f"recent diff 覆盖 {len(diffs)} 个文件",
                source="git",
                relevance=RelevanceLevel.MEDIUM,
            )
        )

    # 4. 运行时证据
    runtime = debug_ctx.get("runtime")
    if runtime and isinstance(runtime, dict):
        # FIX: P1-9b 对齐真实快照结构 runtime.process.* / runtime.system.*
        process = runtime.get("process") or {}
        system = runtime.get("system") or {}
        if isinstance(process, dict) and isinstance(system, dict):
            evidence.append(
                EvidenceItem(
                    type=EvidenceType.RUNTIME_STATE,
                    description=(
                        f"运行时状态：CPU {system.get('cpu_percent', process.get('cpu_percent', '?'))}%，"
                        f"内存 {process.get('memory_rss_mb', '?')}MB，"
                        f"线程 {process.get('num_threads', '?')} 个"
                    ),
                    source="runtime_collector",
                    relevance=RelevanceLevel.LOW,
                )
            )

    # 5. 网络证据
    network = debug_ctx.get("network_trace")
    if network:
        count = len(network) if isinstance(network, list) else 1
        evidence.append(
            EvidenceItem(
                type=EvidenceType.NETWORK_CAPTURE,
                description=f"已采集 {count} 条网络请求记录",
                source="network_collector",
                relevance=RelevanceLevel.MEDIUM,
            )
        )

    # 6. UI 事件证据
    ui_events = debug_ctx.get("ui_events")
    if ui_events:
        count = len(ui_events) if isinstance(ui_events, list) else 1
        evidence.append(
            EvidenceItem(
                type=EvidenceType.UI_EVENT,
                description=f"已采集 {count} 条前端 UI 事件",
                source="browser_sdk",
                relevance=RelevanceLevel.MEDIUM,
            )
        )

    # 7. 知识库证据
    sources = repair_ctx.get("sources") or {}
    prior = repair_ctx.get("prior_analysis") or {}
    if sources.get("knowledge_base_hit") or prior.get("knowledge_base_hit"):
        evidence.append(
            EvidenceItem(
                type=EvidenceType.HISTORICAL_FIX,
                description="知识库精确命中，复用历史修复结论",
                source="knowledge_base",
                relevance=RelevanceLevel.HIGH,
            )
        )
    vector_recall = sources.get("vector_recall") or []
    for item in vector_recall:
        if isinstance(item, dict):
            evidence.append(
                EvidenceItem(
                    type=EvidenceType.HISTORICAL_FIX,
                    description=f"向量召回相似案例：{item.get('summary', item.get('id', 'unknown'))}",
                    source="vector_store",
                    relevance=RelevanceLevel.MEDIUM,
                )
            )

    # 8. LLM 分析证据
    if prior and prior.get("root_cause"):
        evidence.append(
            EvidenceItem(
                type=EvidenceType.LLM_REASONING,
                description=f"LLM 分析：{prior.get('root_cause', '')[:200]}",
                source="llm_analyzer",
                relevance=RelevanceLevel.HIGH if prior.get("confidence") == "high" else RelevanceLevel.MEDIUM,
                detail={
                    "confidence": prior.get("confidence"),
                    "analysis_source": prior.get("analysis_source"),
                    "cached": prior.get("cached", False),
                },
            )
        )

    # 9. 规范证据
    spec_diffs = debug_ctx.get("spec_diffs")
    if spec_diffs:
        evidence.append(
            EvidenceItem(
                type=EvidenceType.SPEC_VIOLATION,
                description=f"规范校验发现 {len(spec_diffs) if isinstance(spec_diffs, list) else 1} 处偏离",
                source="assert_engine",
                relevance=RelevanceLevel.HIGH,
            )
        )

    return evidence


# ── AnalysisConfidence 评分 ──


def _score_confidence(
    evidence_items: list[EvidenceItem],
    completeness: ContextCompleteness,
) -> AnalysisConfidence:
    """基于证据质量 + 上下文完整度，综合评定分析可信度。

    评分逻辑：
    - 基础分：证据数量（0-5 条=0.0-0.5，5+ 条=0.5-1.0）
    - 质量加成：高相关度证据占比越高，加成越大
    - 覆盖度加成：已覆盖维度越多，加成越大
    - 完整度加成：上下文越完整，可信度越高（底数高）
    """
    total = len(evidence_items)
    high_count = sum(1 for e in evidence_items if e.relevance == RelevanceLevel.HIGH)

    # 基础分（0-0.5）：5 条以上证据即满分
    base = min(total / 5.0, 1.0) * 0.5

    # 质量加成（0-0.3）：高相关度证据占比
    if total > 0:
        quality = (high_count / total) * 0.3
    else:
        quality = 0.0

    # 覆盖度加成（0-0.2）：已覆盖的维度数
    covered = _count_covered_dimensions(evidence_items)
    coverage = min(covered / 5.0, 1.0) * 0.2

    overall = round(base + quality + coverage, 4)

    # 分析覆盖/缺失维度
    covered_types = {e.type.value for e in evidence_items}
    all_types = {t.value for t in EvidenceType}
    missing_aspects = sorted(all_types - covered_types)
    coverage_aspects = sorted(covered_types)

    return AnalysisConfidence(
        overall_score=overall,
        evidence_count=total,
        high_relevance_count=high_count,
        coverage_aspects=coverage_aspects,
        missing_aspects=missing_aspects,
    )


def _count_covered_dimensions(evidence_items: list[EvidenceItem]) -> int:
    """统计证据覆盖了多少个不同的 EvidenceType。"""
    return len({e.type for e in evidence_items})


# ── 改进建议生成 ──


def _generate_suggestions(
    completeness: ContextCompleteness,
    confidence: AnalysisConfidence,
) -> list[str]:
    """根据完整度和可信度评分，生成改进建议。"""
    suggestions: list[str] = []

    # 完整度建议
    for dim, score in completeness.dimensions.items():
        if not score.present:
            label = _DIMENSION_LABELS.get(dim, dim.value)
            suggestions.append(f"缺少 {label}，建议启用对应采集器或检查配置")

    # 可信度建议
    if confidence.evidence_count == 0:
        suggestions.append("无任何证据条目，请检查采集链路是否正常运行")
    elif confidence.high_relevance_count == 0:
        suggestions.append("所有证据相关度均为中/低，建议启用 LLM 分析增强以提升可信度")
    if confidence.overall_score < 0.3:
        suggestions.append("分析可信度偏低（< 0.3），建议优先补充高相关度证据（堆栈、源码、git blame）")

    # 综合建议
    if completeness.overall_score < 0.3:
        suggestions.append("上下文完整度严重不足，采集链路可能存在问题，建议排查")

    return suggestions


_DIMENSION_LABELS: dict[ContextDimension, str] = {
    ContextDimension.TRACE: "异常堆栈",
    ContextDimension.CODE_SNIPPET: "源码片段",
    ContextDimension.RUNTIME: "运行时快照",
    ContextDimension.GIT_CONTEXT: "Git 归因",
    ContextDimension.NETWORK: "网络请求记录",
    ContextDimension.UI_EVENT: "前端 UI 事件",
    ContextDimension.SPEC: "规范校验",
    ContextDimension.KNOWLEDGE_BASE: "知识库/向量召回",
    ContextDimension.LLM_ANALYSIS: "LLM 先验分析",
}