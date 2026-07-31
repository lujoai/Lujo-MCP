"""AI Debug Agent 基础框架 —— 多 Agent 协同的抽象契约。

定义 BaseAgent 抽象基类与统一的上下文/结果/审计数据结构。
Phase 1 仅有 RepairAgent 实现；Phase 2 GitAgent / TestAgent / SecurityAgent
继承本类，Coordinator 通过 BaseAgent 多态调度。
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from openai import APIError, APITimeoutError, AsyncOpenAI, RateLimitError

logger = logging.getLogger("ai-debug-mcp.agent.base")


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

    @classmethod
    def _skipped(cls, started_at: float, reason: str) -> AgentResult:
        """构造 SKIPPED 状态的 AgentResult（依赖不可用时跳过）。"""
        return AgentResult(
            agent_name=cls.name,
            status=AgentStatus.SKIPPED,
            output={},
            error=reason,
            started_at=started_at,
            finished_at=cls._now(),
        )

    async def _call_llm(
        self,
        client: AsyncOpenAI,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_retries: int,
        validate_fn: Callable[[str], dict[str, Any]],
        fallback_model: str = "",
    ) -> dict[str, Any]:
        """带重试/fallback 的异步 LLM 调用基类方法。

        重试 RateLimitError / APITimeoutError / APIError；
        耗尽后尝试 fallback_model（若配置）；仍失败抛 RuntimeError。
        validate_fn 由各 Agent 传入自己的 _validate_* 函数。
        """
        last_error: Optional[Exception] = None

        for attempt in range(max_retries + 1):
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    response_format={"type": "json_object"},
                )
                choice = response.choices[0]
                content = choice.message.content or "{}"
                analysis = validate_fn(content)
                usage = {}
                if response.usage:
                    usage = {
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                        "total_tokens": response.usage.total_tokens,
                    }
                return {"analysis": analysis, "usage": usage}
            except (RateLimitError, APITimeoutError, APIError) as e:
                last_error = e
                if attempt < max_retries:
                    wait = 2 ** attempt
                    logger.warning(
                        "%s LLM 调用失败 (attempt %d/%d): %s, 等待 %ds 重试",
                        self.name, attempt + 1, max_retries + 1, e, wait,
                    )
                    await asyncio.sleep(wait)

        # 重试耗尽，尝试 fallback 模型
        if fallback_model and fallback_model != model:
            logger.warning(
                "%s 主模型 %s 不可用，切换 fallback: %s",
                self.name, model, fallback_model,
            )
            response = await client.chat.completions.create(
                model=fallback_model,
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"},
            )
            choice = response.choices[0]
            content = choice.message.content or "{}"
            analysis = validate_fn(content)
            usage = {}
            if response.usage:
                usage = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                }
            return {"analysis": analysis, "usage": usage}

        raise RuntimeError(
            f"{self.name} LLM 调用失败（已重试 {max_retries} 次）: {last_error}"
        )

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
