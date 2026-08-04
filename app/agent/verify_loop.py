"""Agent Verify Loop —— 迭代修复闭环（v0.4.0 M4）。

设计：三层开关控制 + 四级判定 + 验证通过后 KB 写回。

三层开关（叠加，逐层收紧）：
1. ``agent_enabled``：Agent 子系统总开关
2. ``agent_multi_agent_enabled``：多 Agent DAG 开关
3. ``agent_verify_loop_enabled``：Verify Loop 迭代开关

四级判定（按综合验证分 score，0~1）：
- ``high_confidence``：score >= high_confidence_pass_threshold（0.85）→ 直接通过，快速收敛
- ``passed``：score >= pass_threshold（0.7）→ 通过
- ``partial``：score >= partial_threshold（0.5）且 < pass_threshold → 部分通过，可继续迭代
- ``failed``：score < partial_threshold 或无法产出验证依据 → 迭代失败

流程：每个迭代轮次执行一次完整 DAG（修复 → 并行审查 → 验证评分），
达到 passed/high_confidence 或达到最大迭代轮数即收敛；通过后写回 KB。

设计原则：
- 纯编排，不侵入各 Agent 实现（依赖 Coordinator 单次 DAG 迭代函数）
- 验证评分用确定性启发式（无额外 LLM 调用），可测试、可复现
- 每轮失败静默降级，最终返回聚合结果 + verify_loop 信息
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from app.config import settings

logger = logging.getLogger("ai-debug-mcp.agent.verify_loop")


class VerifyVerdict(str, Enum):
    """四级验证判定结果。"""

    HIGH_CONFIDENCE = "high_confidence"
    PASSED = "passed"
    PARTIAL = "partial"
    FAILED = "failed"


def compute_verdict(score: float) -> VerifyVerdict:
    """按综合验证分判定四级结果。"""
    if score >= settings.agent_verify_loop_high_confidence_pass_threshold:
        return VerifyVerdict.HIGH_CONFIDENCE
    if score >= settings.agent_verify_loop_pass_threshold:
        return VerifyVerdict.PASSED
    if score >= settings.agent_verify_loop_partial_threshold:
        return VerifyVerdict.PARTIAL
    return VerifyVerdict.FAILED


def compute_verify_score(result: dict[str, Any]) -> float:
    """基于聚合 DAG 输出计算综合验证分（确定性启发式，0~1）。

    评分维度（权重合计 1.0）：
    - repair_plan 存在：+0.4
    - test_plan 存在且含 test_cases：+0.3
    - security_review 存在且无 critical 发现：+0.2
    - git_attribution 存在：+0.1

    任一维度缺失按 0 计，最终分值 = 各项得分之和。
    """
    score = 0.0

    if result.get("repair_plan"):
        score += 0.4

    test_plan = result.get("test_plan") or {}
    if test_plan and test_plan.get("test_cases"):
        score += 0.3

    security_review = result.get("security_review") or {}
    if security_review:
        findings = security_review.get("findings") or []
        critical = any(
            str(f.get("severity", "")).lower() in ("critical", "high")
            for f in findings
        )
        if not critical:
            score += 0.2

    if result.get("git_attribution"):
        score += 0.1

    return round(min(score, 1.0), 3)


async def run_verify_loop(
    iteration_fn: Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]],
    ctx: Any,
    sources: dict[str, Any],
) -> dict[str, Any]:
    """执行 Verify Loop 迭代闭环。

    Args:
        iteration_fn: 单次 DAG 迭代函数（Coordinator._run_dag），
            签名 (ctx, sources) -> 聚合结果 dict。
        ctx: AgentContext（跨轮复用，repair_plan 会注入 ctx.repair_context）。
        sources: 装配后的 sources。

    Returns:
        最后一次迭代的聚合结果 + ``verify_loop`` 信息：
        {
          "verify_loop": {
            "enabled": bool,
            "iterations": [ {iteration, verdict, score, repair_plan} ],  # 每轮判定
            "total_iterations": int,
            "final_verdict": str,
            "kb_writeback": bool | None
          }
        }
    """
    max_iterations = max(1, settings.agent_verify_loop_max_iterations)
    iterations: list[dict[str, Any]] = []
    final_result: dict[str, Any] = {}
    final_verdict: VerifyVerdict = VerifyVerdict.FAILED
    kb_writeback: Optional[bool] = None

    for i in range(1, max_iterations + 1):
        result = await iteration_fn(ctx, sources)
        final_result = result

        score = compute_verify_score(result)
        verdict = compute_verdict(score)
        iterations.append(
            {
                "iteration": i,
                "verdict": verdict.value,
                "score": score,
                "repair_plan": bool(result.get("repair_plan")),
            }
        )

        # 通过 → 收敛并写回 KB
        if verdict in (VerifyVerdict.HIGH_CONFIDENCE, VerifyVerdict.PASSED):
            final_verdict = verdict
            kb_writeback = _writeback_kb(ctx, result, score)
            break

        # 未通过 → 将本轮 repair_plan 注入 ctx，供下一轮修复复用
        if result.get("repair_plan") is not None:
            ctx.repair_context["repair_plan"] = result["repair_plan"]

        final_verdict = verdict

    final_result["verify_loop"] = {
        "enabled": True,
        "iterations": iterations,
        "total_iterations": len(iterations),
        "final_verdict": final_verdict.value,
        "kb_writeback": kb_writeback,
    }
    return final_result


def _writeback_kb(ctx: Any, result: dict[str, Any], score: float) -> Optional[bool]:
    """验证通过后写回 KB：按异常指纹递增 verify_count / 提升 case_confidence。

    由 ``settings.agent_verify_loop_kb_writeback_enabled`` 控制。
    未命中 KB 条目（无指纹或不匹配）时返回 None，不抛异常。
    """
    if not settings.agent_verify_loop_kb_writeback_enabled:
        return None
    try:
        from app.rag.knowledge_base import record_verification

        debug_context = ctx.debug_context or {}
        exception = debug_context.get("exception") or {}
        fingerprint = exception.get("fingerprint")
        if not fingerprint:
            return None
        updated = record_verification(fingerprint, score)
        if updated is None:
            return None
        logger.info(
            "Verify Loop KB 写回成功 fingerprint=%s verify_count=%s confidence=%s",
            fingerprint,
            updated.get("verify_count"),
            updated.get("case_confidence"),
        )
        return True
    except Exception:
        logger.warning("Verify Loop KB 写回失败", exc_info=True)
        return None