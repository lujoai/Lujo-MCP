"""多 Agent DAG 定义（Phase 2）。

DAG 结构：
    ┌─────────────┐
    │ RepairAgent │ (先行，产出 repair_plan)
    └──────┬──────┘
           │ repair_plan 注入到下游 Agent 的 repair_context
           ├──────────────┬──────────────┐
           ▼              ▼              ▼
     ┌──────────┐  ┌──────────┐  ┌──────────────┐
     │ GitAgent │  │TestAgent │  │SecurityAgent │  (并行审查)
     └──────────┘  └──────────┘  └──────────────┘

说明：
- GitAgent 不依赖 repair_plan（纯 git 归因），可与 RepairAgent 并行；
  但为简化 DAG 拓扑与 trace 顺序，统一在 RepairAgent 之后并行执行。
- TestAgent / SecurityAgent 依赖 repair_plan；RepairAgent 失败时二者返回 SKIPPED。
- 任一下游 Agent 失败静默降级，不阻断其他 Agent 与最终输出聚合。

本模块仅定义 DAG 拓扑与节点注册，编排执行逻辑在 Coordinator._run_dag()。
"""

from __future__ import annotations

from app.agent.base import BaseAgent
from app.agent.git_agent import GitAgent
from app.agent.repair_agent import RepairAgent
from app.agent.security_agent import SecurityAgent
from app.agent.test_agent import TestAgent

# Phase 2 DAG 节点注册表：name → BaseAgent 实例
# Coordinator 通过本注册表多态调度，新增 Agent 只需在此注册
PHASE2_AGENTS: dict[str, BaseAgent] = {
    "repair": RepairAgent(),
    "git": GitAgent(),
    "test": TestAgent(),
    "security": SecurityAgent(),
}

# DAG 拓扑：
# - 先行节点（串行执行，产出 repair_plan）
# - 并行节点（基于 repair_plan 并行审查）
# 新增 Agent 时按依赖关系归类到对应层。
PHASE2_FIRST_NODES: list[str] = ["repair"]
PHASE2_PARALLEL_NODES: list[str] = ["git", "test", "security"]


def build_phase2_agents() -> dict[str, BaseAgent]:
    """构造 Phase 2 DAG 节点注册表（每次调用返回新实例，避免共享状态）。"""
    return {
        "repair": RepairAgent(),
        "git": GitAgent(),
        "test": TestAgent(),
        "security": SecurityAgent(),
    }


def get_phase2_agent_names() -> list[str]:
    """返回 Phase 2 DAG 中所有 Agent 名称（用于文档与配置校验）。"""
    return list(PHASE2_AGENTS.keys())
