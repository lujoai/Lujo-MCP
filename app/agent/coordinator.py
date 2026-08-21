"""Coordinator —— Agent 执行编排器。

Phase 1：单 Agent 串行（RepairAgent only）。
Phase 2：多 Agent DAG 调度 —— RepairAgent 先行 → GitAgent / TestAgent / SecurityAgent
并行审查（依赖 repair_plan）。通过 `agent_multi_agent_enabled` 开关切换。

静默降级：
- RepairAgent 失败 → repair_plan=None + 下游 Agent 自动 SKIPPED + agent_trace[FAILED]
- 下游 Agent 失败 → 对应 trace 标 FAILED/SKIPPED，不阻断其他 Agent 与最终聚合
- 任一 Agent 异常 → coordinator 防御性兜底，不抛异常穿透到 RepairQueue

零侵入约束：BaseAgent 多态接口无需变更，新增 Agent 只需继承 BaseAgent + 在 dag.py 注册。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from app.agent.base import (
    AgentContext,
    AgentResult,
    AgentStatus,
    AgentTrace,
    BaseAgent,
)
from app.agent.context_assembler import RepairContextAssembler
from app.agent.dag import (
    PHASE2_PARALLEL_NODES,
    build_phase2_agents,
)
from app.agent.repair_agent import RepairAgent
from app.config import AgentMode, settings

logger = logging.getLogger("lujo-mcp.agent.coordinator")


class Coordinator:
    """编排 Agent 执行流程。

    Phase 1 单 Agent 串行；Phase 2 多 Agent DAG（RepairAgent 先行 + 三 Agent 并行审查）。
    通过 ``settings.agent_multi_agent_enabled`` 切换模式。
    """

    def __init__(self) -> None:
        self._assembler = RepairContextAssembler()
        self._agents: dict[str, BaseAgent] = {"repair": RepairAgent()}
        # Phase 2：注册多 Agent DAG 节点（惰性，仅在启用时生效）
        self._phase2_agents: dict[str, BaseAgent] = build_phase2_agents()

    async def run(
        self, debug_context: dict[str, Any], model: Optional[str] = None
    ) -> dict[str, Any]:
        """主入口：装配上下文 → 调度 Agent DAG → 组装最终输出。

        返回:
            {
              "repair_plan": {...} | None,
              "sources": {"vector_recall": [...], "git_context": [...], "knowledge_base_hit": bool},
              "agent_trace": [AgentTrace.to_dict(), ...],
              "git_attribution": {...} | None,        # Phase 2 新增
              "test_plan": {...} | None,              # Phase 2 新增
              "security_review": {...} | None,        # Phase 2 新增
              "multi_agent_mode": bool                # Phase 2 新增（标识 DAG 是否启用）
            }

        静默降级：任何 Agent 失败 → 对应 trace 标 FAILED/SKIPPED，不抛异常。
        """
        trace_id = debug_context.get("request_id") or debug_context.get("trace_id")

        # Step 1: 装配修复上下文（内含三个并发子装配，各自 fail-safe）
        repair_context = await self._assembler.assemble(debug_context)
        sources = repair_context.get("sources", {})

        # Step 2: 构造 AgentContext
        ctx = AgentContext(
            debug_context=debug_context,
            repair_context=repair_context,
            model=model,
            trace_id=trace_id,
        )

        # Step 3: 根据 AgentMode 调度 Agent DAG 或串行执行
        mode = settings.get_agent_mode()
        if mode == AgentMode.VERIFY_LOOP:
            from app.agent.verify_loop import run_verify_loop

            return await run_verify_loop(self._run_dag, ctx, sources)
        if mode == AgentMode.DAG:
            return await self._run_dag(ctx, sources)

        # 默认回退 / SINGLE / OFF 模式：单 Agent 串行（保持向后兼容）
        return await self._run_phase1(ctx, sources)

    async def _run_phase1(
        self, ctx: AgentContext, sources: dict[str, Any]
    ) -> dict[str, Any]:
        """Phase 1：单 RepairAgent 串行（原逻辑，保持向后兼容）。"""
        agent_trace: list[dict[str, Any]] = []
        repair_plan: Optional[dict[str, Any]] = None

        repair_agent = self._agents["repair"]
        try:
            result: AgentResult = await repair_agent.run(ctx)
            if result.status == AgentStatus.SUCCESS:
                repair_plan = result.output.get("repair_plan")
            trace = BaseAgent._trace(result)
            agent_trace.append(trace.to_dict())
        except Exception as e:
            logger.exception("Coordinator: RepairAgent unexpected error")
            agent_trace.append(
                AgentTrace(
                    agent_name="repair",
                    status=AgentStatus.FAILED,
                    duration_s=0.0,
                    error=str(e),
                ).to_dict()
            )

        repair_ctx = ctx.repair_context or {}
        return {
            "repair_plan": repair_plan,
            "sources": sources,
            "agent_trace": agent_trace,
            "quality_report": repair_ctx.get("quality_report"),
            "debug_experience": repair_ctx.get("debug_experience"),
            "multi_agent_mode": False,
        }

    async def _run_dag(
        self, ctx: AgentContext, sources: dict[str, Any]
    ) -> dict[str, Any]:
        """Phase 2：多 Agent DAG 调度。

        流程：
        1. RepairAgent 先行执行，产出 repair_plan
        2. 将 repair_plan 注入 ctx.repair_context，供下游 Agent 读取
        3. GitAgent / TestAgent / SecurityAgent 并行执行（asyncio.gather）
        4. 聚合所有 Agent 结果与 trace，静默降级处理失败节点
        """
        agent_trace: list[dict[str, Any]] = []
        repair_plan: Optional[dict[str, Any]] = None

        # Layer 1: RepairAgent 先行
        repair_agent = self._phase2_agents.get("repair") or self._agents["repair"]
        try:
            repair_result = await repair_agent.run(ctx)
            if repair_result.status == AgentStatus.SUCCESS:
                repair_plan = repair_result.output.get("repair_plan")
            agent_trace.append(BaseAgent._trace(repair_result).to_dict())
        except Exception as e:
            logger.exception("Coordinator DAG: RepairAgent unexpected error")
            agent_trace.append(
                AgentTrace(
                    agent_name="repair",
                    status=AgentStatus.FAILED,
                    duration_s=0.0,
                    error=str(e),
                ).to_dict()
            )
            repair_result = None

        # 将 repair_plan 注入 repair_context，供下游 Agent 读取
        if repair_plan is not None:
            ctx.repair_context["repair_plan"] = repair_plan

        # Layer 2: GitAgent / TestAgent / SecurityAgent 并行审查
        parallel_results = await self._run_parallel_agents(ctx)

        # 聚合并行 Agent 的 trace（保持固定顺序：git → test → security）
        git_output: Optional[dict[str, Any]] = None
        test_output: Optional[dict[str, Any]] = None
        security_output: Optional[dict[str, Any]] = None
        parallel_failures = 0

        for node_name in PHASE2_PARALLEL_NODES:
            result = parallel_results.get(node_name)
            if result is None:
                continue
            agent_trace.append(BaseAgent._trace(result).to_dict())
            if result.status == AgentStatus.SUCCESS:
                if node_name == "git":
                    git_output = result.output
                elif node_name == "test":
                    test_output = result.output.get("test_plan")
                elif node_name == "security":
                    security_output = result.output.get("security_review")
            elif result.status == AgentStatus.FAILED:
                parallel_failures += 1

        # DAG 降级信号：repair 层失败 或 并行节点失败数达阈值时标记（FIX: P1-9g，
        # 修复前 repair 失败不计入，整个 DAG 失效却被报告健康）。
        repair_failed = (
            repair_result is None
            or repair_result.status != AgentStatus.SUCCESS
        )
        dag_degraded = (
            repair_failed
            or parallel_failures >= settings.agent_dag_failure_threshold
        )

        if dag_degraded:
            # FIX: P1-9g degraded 必须告警（此前无任何消费方/日志，静默健康）
            skipped = sum(
                1
                for r in parallel_results.values()
                if isinstance(r, AgentResult) and r.status == AgentStatus.SKIPPED
            )
            logger.warning(
                "Coordinator DAG degraded: repair_failed=%s, "
                "parallel_failures=%d/%d, skipped=%d",
                repair_failed,
                parallel_failures,
                len(PHASE2_PARALLEL_NODES),
                skipped,
            )

        repair_ctx = ctx.repair_context or {}
        return {
            "repair_plan": repair_plan,
            "sources": sources,
            "agent_trace": agent_trace,
            "git_attribution": git_output,
            "test_plan": test_output,
            "security_review": security_output,
            "quality_report": repair_ctx.get("quality_report"),
            "debug_experience": repair_ctx.get("debug_experience"),
            "multi_agent_mode": True,
            "dag_degraded": dag_degraded,
        }

    async def _run_parallel_agents(
        self, ctx: AgentContext
    ) -> dict[str, AgentResult]:
        """并行执行 GitAgent / TestAgent / SecurityAgent，各失败独立降级。

        使用 return_exceptions=True 确保单节点异常不影响其他节点。
        """
        tasks: list[tuple[str, Any]] = []
        for node_name in PHASE2_PARALLEL_NODES:
            agent = self._phase2_agents.get(node_name)
            if agent is None:
                continue
            tasks.append((node_name, agent.run(ctx)))

        if not tasks:
            return {}

        # FIX: P2 agent_dag_parallel_timeout 接入 —— 此前并行 Agent 无超时，
        # 卡死的 Agent 无限拖住整个 DAG；0 表示继承 agent_timeout（向后兼容）
        timeout = settings.agent_dag_parallel_timeout or settings.agent_timeout
        try:
            raw_results = await asyncio.wait_for(
                asyncio.gather(*[t[1] for t in tasks], return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Coordinator DAG: parallel agents timeout after %ss, "
                "marking all nodes FAILED",
                timeout,
            )
            return {
                name: AgentResult(
                    agent_name=name,
                    status=AgentStatus.FAILED,
                    output={},
                    error=f"DAG parallel timeout after {timeout}s",
                    started_at=0.0,
                    finished_at=BaseAgent._now(),
                )
                for name, _ in tasks
            }

        results: dict[str, AgentResult] = {}
        for (node_name, _), raw in zip(tasks, raw_results):
            if isinstance(raw, Exception):
                # 防御性兜底：Agent 内部已 try/except，此处仅防御
                logger.exception(
                    "Coordinator DAG: %s unexpected error", node_name
                )
                results[node_name] = AgentResult(
                    agent_name=node_name,
                    status=AgentStatus.FAILED,
                    output={},
                    error=str(raw),
                    started_at=0.0,
                    finished_at=BaseAgent._now(),
                )
            elif isinstance(raw, AgentResult):
                results[node_name] = raw
        return results
