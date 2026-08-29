"""AI Debug Agent —— 自动修复 + 多 Agent 协同（Phase 1 MVP + Phase 2 DAG）。

模块结构：
- base.py: BaseAgent ABC + AgentContext/AgentResult/AgentTrace + AgentStatus
- context_assembler.py: 修复上下文装配（复用 analyze_async + retrieve_similar + get_recent_diff）
- repair_agent.py: RepairAgent（复用 analyzer._get_async_client）
- git_agent.py: GitAgent（git blame/diff 归因，Phase 2 DAG 节点）
- test_agent.py: TestAgent（验证策略生成，Phase 2 DAG 节点）
- security_agent.py: SecurityAgent（修复方案安全审查，Phase 2 DAG 节点）
- dag.py: 多 Agent DAG 拓扑定义（Phase 2）
- repair_queue.py: 异步削峰队列 + lifespan helper（对称 analysis_queue.py）
- coordinator.py: Agent 编排器（按 agent_mode 派发：single 串行 / dag 并行 DAG / verify_loop 迭代）

启用开关（settings.get_agent_mode() / settings.is_agent_active）：
- 显式配置 AGENT_MODE（single | dag | verify_loop | off）时以它为准；
- 未显式配置时按历史布尔开关向后兼容派生：agent_verify_loop_enabled /
  agent_iterative_repair_enabled → verify_loop；agent_multi_agent_enabled → dag；
  agent_enabled → single；全关 → off（路由不挂载，零行为变更）。
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
from app.agent.dag import (
    PHASE2_FIRST_NODES,
    PHASE2_PARALLEL_NODES,
    build_phase2_agents,
    get_phase2_agent_names,
)
from app.agent.git_agent import GitAgent
from app.agent.repair_agent import RepairAgent
from app.agent.repair_queue import (
    QueueFullError,
    RepairQueue,
    drain_repair_queue,
    get_repair_queue,
    start_repair_queue,
)
from app.agent.security_agent import SecurityAgent
from app.agent.test_agent import TestAgent

__all__ = [
    "AgentContext",
    "AgentResult",
    "AgentStatus",
    "AgentTrace",
    "BaseAgent",
    "Coordinator",
    "GitAgent",
    "PHASE2_FIRST_NODES",
    "PHASE2_PARALLEL_NODES",
    "QueueFullError",
    "RepairAgent",
    "RepairContextAssembler",
    "RepairQueue",
    "SecurityAgent",
    "TestAgent",
    "build_phase2_agents",
    "drain_repair_queue",
    "get_phase2_agent_names",
    "get_repair_queue",
    "start_repair_queue",
]
