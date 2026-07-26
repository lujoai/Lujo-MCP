"""AI Debug Agent 基础框架 —— 多 Agent 协同的抽象契约。

定义 BaseAgent 抽象基类与统一的上下文/结果/审计数据结构。
Phase 1 仅有 RepairAgent 实现；Phase 2 GitAgent / TestAgent / SecurityAgent
继承本类，Coordinator 通过 BaseAgent 多态调度。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class AgentStatus(str, Enum):
    """Agent 执行状态。SKIPPED 用于静默降级（如依赖不可用时跳过该 Agent）。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(slots=True)
class AgentContext:
    """Agent 执行输入：原始调试上下文 + 装配后的修复上下文。"""

    debug_context: dict[str, Any]
    repair_context: dict[str, Any]  # 由 RepairContextAssembler 装配
    model: Optional[str] = None
    trace_id: Optional[str] = None


@dataclass(slots=True)
class AgentResult:
    """Agent 执行输出统一契约。output 字段由各 Agent 自定义。"""

    agent_name: str
    status: AgentStatus
    output: dict[str, Any]
    error: Optional[str] = None
    started_at: float = 0.0
    finished_at: float = 0.0
    usage: dict[str, int] = field(
        default_factory=lambda: {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
    )


@dataclass(slots=True)
class AgentTrace:
    """单次 Agent 执行的审计记录，进入 agent_trace[] 数组。"""

    agent_name: str
    status: AgentStatus
    duration_s: float
    error: Optional[str] = None
    usage: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "status": self.status.value if isinstance(self.status, AgentStatus) else self.status,
            "duration_s": self.duration_s,
            "error": self.error,
            "usage": self.usage,
        }


class BaseAgent(ABC):
    """多 Agent 协同框架的抽象基类。

    子类需实现 ``run(ctx) -> AgentResult``。Coordinator 通过本类多态调度，
    Phase 2 接入多 Agent DAG 时无需改 Coordinator 编排逻辑。
    """

    name: str

    @abstractmethod
    async def run(self, ctx: AgentContext) -> AgentResult:
        """执行 Agent 逻辑，返回统一 AgentResult。"""

    @staticmethod
    def _trace(result: AgentResult) -> AgentTrace:
        """从 AgentResult 派生审计记录。"""
        return AgentTrace(
            agent_name=result.agent_name,
            status=result.status,
            duration_s=round(result.finished_at - result.started_at, 3),
            error=result.error,
            usage=result.usage,
        )

    @staticmethod
    def _now() -> float:
        """统一的时间戳获取（便于测试 mock）。"""
        return time.time()
