"""AI Debug Agent —— 自动修复 + 多 Agent 协同（Phase 1 MVP）。

模块结构：
- base.py: BaseAgent ABC + AgentContext/AgentResult/AgentTrace + AgentStatus
- schemas.py: Pydantic 数据模型（RepairRequest/RepairPlan/RepairJob/Sources）
- context_assembler.py: 修复上下文装配（复用 analyze_async + retrieve_similar + get_recent_diff）
- repair_agent.py: RepairAgent（复用 analyzer._get_async_client）
- repair_queue.py: 异步削峰队列 + lifespan helper（对称 analysis_queue.py）
- coordinator.py: Agent 编排器（Phase 1 单 Agent 串行）

feature flag: settings.agent_enabled（默认 False，关闭时零行为变更）
"""

from app.agent.base import (
    AgentContext,
    AgentResult,
    AgentStatus,
    AgentTrace,
    BaseAgent,
)
from app.agent.coordinator import Coordinator
from app.agent.context_assembler import RepairContextAssembler
from app.agent.repair_agent import RepairAgent
from app.agent.repair_queue import (
    QueueFullError,
    RepairQueue,
    drain_repair_queue,
    get_repair_queue,
    start_repair_queue,
)
from app.agent.schemas import RepairJob, RepairPlan, RepairRequest, Sources

__all__ = [
    "AgentContext",
    "AgentResult",
    "AgentStatus",
    "AgentTrace",
    "BaseAgent",
    "Coordinator",
    "QueueFullError",
    "RepairAgent",
    "RepairContextAssembler",
    "RepairJob",
    "RepairPlan",
    "RepairQueue",
    "RepairRequest",
    "Sources",
    "drain_repair_queue",
    "get_repair_queue",
    "start_repair_queue",
]
