"""Coordinator —— Agent 执行编排器。

Phase 1：单 Agent 串行（RepairAgent only）。
Phase 2 预留：self._agents 注册 GitAgent / TestAgent / SecurityAgent，
按 plan: list[AgentStep] DAG 调度（接口已通过 BaseAgent 多态预留）。

静默降级：RepairAgent 失败 → repair_plan=None + agent_trace[FAILED]，不抛异常。
"""

from __future__ import annotations

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
from app.agent.repair_agent import RepairAgent

logger = logging.getLogger("ai-debug-mcp.agent.coordinator")


class Coordinator:
    """编排 Agent 执行流程。

    Phase 1 单 Agent 串行；Phase 2 接入多 Agent DAG 时扩展 run() 内的调度逻辑，
    BaseAgent 多态接口无需变更。
    """

    def __init__(self) -> None:
        self._assembler = RepairContextAssembler()
        # Phase 2：注册多个 agent，按 plan 调度
        self._agents: dict[str, BaseAgent] = {"repair": RepairAgent()}

    async def run(
        self, debug_context: dict[str, Any], model: Optional[str] = None
    ) -> dict[str, Any]:
        """主入口：装配上下文 → 调度 RepairAgent → 组装最终输出。

        返回:
            {
              "repair_plan": {...} | None,
              "sources": {"vector_recall": [...], "git_context": [...], "knowledge_base_hit": bool},
              "agent_trace": [AgentTrace.to_dict(), ...]
            }

        静默降级：任何 Agent 失败 → 对应 trace 标 FAILED，repair_plan=None，不抛异常。
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

        # Step 3: 调度 RepairAgent（Phase 1 单 Agent 串行）
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
            # 三重兜底：agent 内部异常 → coordinator 异常 → 上层 RepairQueue 静默降级
            # RepairAgent.run 内部已 try/except，此处仅防御性兜底
            logger.exception("Coordinator: RepairAgent unexpected error")
            agent_trace.append(
                AgentTrace(
                    agent_name="repair",
                    status=AgentStatus.FAILED,
                    duration_s=0.0,
                    error=str(e),
                ).to_dict()
            )

        return {
            "repair_plan": repair_plan,
            "sources": sources,
            "agent_trace": agent_trace,
        }
