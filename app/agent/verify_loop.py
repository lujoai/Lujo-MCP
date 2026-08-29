"""Agent Verify Loop —— 迭代修复闭环（v0.4.0 M4）。

设计：迭代判定 + 验证通过后 KB 写回。

启用条件：``settings.get_agent_mode() == AgentMode.VERIFY_LOOP`` ——
显式配置 AGENT_MODE=verify_loop 时生效；未显式配置 agent_mode 时按历史
布尔开关向后兼容派生（agent_verify_loop_enabled / agent_iterative_repair_enabled，
见 config.get_agent_mode）。模式判定与开关体系统一收敛在 is_agent_active / get_agent_mode。

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

import asyncio
import logging
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from app.config import settings
from app.observability import record_kb_writeback

logger = logging.getLogger("lujo-mcp.agent.verify_loop")


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
    - security_review 存在且无 high 风险（risks 与 overall_severity 双检）：+0.2
    - git_attribution 存在：+0.1

    任一维度缺失按 0 计，最终分值 = 各项得分之和。

    SEC: 安全门——当 security_review 缺失（SecurityAgent 跳过/失败）、含 high 风险、
    或 overall_severity 为 high/unknown 时，score 上限钳制为 PARTIAL 阈值（含），
    确保 verdict 不会达到 PASSED/HIGH_CONFIDENCE，
    防止"安全审查缺失即绕过"。仍允许 PARTIAL 以继续迭代补全安全审查。
    """
    score = 0.0
    security_pass = False

    if result.get("repair_plan"):
        score += 0.4

    test_plan = result.get("test_plan") or {}
    if test_plan and test_plan.get("test_cases"):
        score += 0.3

    security_review = result.get("security_review") or {}
    if security_review:
        # FIX: CR-1 —— SecurityAgent 的输出契约是 risks/overall_severity
        # （见 security_agent._validate_security_review），不存在 "findings" 键。
        # 此前误读 findings 恒为空列表，导致含 high 风险的方案也能通过安全门。
        # 形状防御：既无 risks 也无 overall_severity 键（如误传旧 findings 形态）
        # 视为畸形输出，门不通过（fail-safe）。
        if "risks" in security_review or "overall_severity" in security_review:
            risks = security_review.get("risks") or []
            has_high_risk = any(
                str(r.get("severity", "")).lower() in ("critical", "high")
                for r in risks
                if isinstance(r, dict)
            )
            overall = str(security_review.get("overall_severity", "none")).lower()
            # overall_severity 为 unknown（LLM 输出非法值）时同样视为不通过（fail-safe）
            if not has_high_risk and overall not in ("high", "unknown"):
                score += 0.2
                security_pass = True
    # security_review 缺失 / 畸形 / 含 high 风险 / overall 无法判定 → security_pass 保持 False

    if result.get("git_attribution"):
        score += 0.1

    score = round(min(score, 1.0), 3)

    # SEC: 安全门未通过时，钳制分数到 PARTIAL 阈值（含），防止 verdict 越级到 PASSED
    pass_threshold = settings.agent_verify_loop_pass_threshold
    if not security_pass and score >= pass_threshold:
        partial_threshold = settings.agent_verify_loop_partial_threshold
        score = partial_threshold

    return score


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

    # 单轮 DAG 执行超时（秒）：0 表示按单轮内部预算继承（FIX: P1-9f 始终有
    # watchdog，避免"无单轮超时"时最差 ≈ 轮数 × (repair 90s + 3 并行各 90s) 卡死）
    round_timeout = float(getattr(settings, "agent_verify_loop_round_timeout", 0) or 0)
    if round_timeout <= 0:
        # FIX: R7-Q4 —— 轮预算算术。单轮内部预算 = RepairAgent(agent_timeout)
        # + 并行节点(agent_dag_parallel_timeout or agent_timeout)。此前继承
        # agent_timeout 等于把单轮预算收紧一半：RepairAgent 耗时 60-90s 时
        # 并行阶段启动数秒即被 watchdog 取消，整轮返回 {"repair_plan": None}
        # 存根、已成功成果丢弃。
        parallel_budget = float(
            settings.agent_dag_parallel_timeout or settings.agent_timeout
        )
        round_timeout = float(settings.agent_timeout or 0) + parallel_budget
        logger.debug(
            "verify_loop round_timeout 未配置，按单轮预算继承 %.1fs"
            "（agent_timeout %s + 并行预算 %.1fs）",
            round_timeout,
            settings.agent_timeout,
            parallel_budget,
        )

    for i in range(1, max_iterations + 1):
        if round_timeout > 0:
            try:
                result = await asyncio.wait_for(
                    iteration_fn(ctx, sources), timeout=round_timeout
                )
            except asyncio.TimeoutError:
                # 单轮超时：静默降级为 FAILED，避免卡死消耗整个 agent_timeout × N
                logger.warning(
                    "Verify Loop 第 %d 轮超时（>%.1fs），判定为 failed",
                    i,
                    round_timeout,
                )
                result = {"repair_plan": None}
        else:
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
            # FIX: v0.6.6 事件循环阻塞 —— _writeback_kb 走 record_verification
            # 同步 IO（KB 存储），直接调用会卡住整个事件循环，移入线程池执行
            kb_writeback = await asyncio.to_thread(_writeback_kb, ctx, result, score)
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

    v0.7.0: 三种返回路径埋点（skipped=开关关闭 / miss=无指纹或未命中 /
    success=写回成功）——只读埋点，不影响返回值与异常传播。
    """
    if not settings.agent_verify_loop_kb_writeback_enabled:
        record_kb_writeback("verify", "skipped")
        return None
    try:
        from app.rag.knowledge_base import record_verification

        debug_context = ctx.debug_context or {}
        exception = debug_context.get("exception") or {}
        fingerprint = exception.get("fingerprint")
        if not fingerprint:
            record_kb_writeback("verify", "miss")
            return None
        updated = record_verification(fingerprint, score)
        if updated is None:
            record_kb_writeback("verify", "miss")
            return None
        record_kb_writeback("verify", "success")
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
