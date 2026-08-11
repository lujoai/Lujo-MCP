"""MCP Debug Context 可观察性增强（Phase 3 D5）

核心资产是 Debug Context（而非 MCP 协议 / Agent / LLM 推理），因此命名为
``DebugContextTrace``。

职责边界：
- 只记录「一次 MCP 请求中 Debug Context 的生命周期信息」。
- 只描述 Context，不描述 AI 推理结果，不包含 Agent 状态。
- 不调用 RAG 检索（避免重复检索），只观察已有 Context 中是否已携带经验字段。
- 不修改业务逻辑，所有字段均为可观测元信息。

本模块新增可观测结构，不改变任何层的返回契约；metadata 为可选字段，旧调用方不受影响。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app import __version__
from app.config import settings

# 经验字段在 Context 中的实际键名（仅观察，不主动检索）
_EXPERIENCE_KEYS = ("debug_experience", "experience")


@dataclass(slots=True)
class DebugContextTrace:
    """一次 MCP 请求中 Debug Context 的生命周期观测记录。

    - ``request_id`` / ``trace_id``：请求标识
    - ``runtime_context_available``：Runtime 层是否构建出非空 Context
    - ``runtime_context_size``：序列化后的 Context 字节数
    - ``experience_enabled``：Debug Experience 开关状态（settings.debug_experience_enabled）
    - ``experience_hit_count``：已有 Context 中携带的经验记录数（0 = 未命中/未携带）
    - ``context_build_duration``：Context 构建耗时（秒）
    - ``response_duration``：Tool 响应耗时（秒）
    """

    request_id: str = ""
    trace_id: str = ""
    runtime_context_available: bool = False
    runtime_context_size: int = 0
    experience_enabled: bool = False
    experience_hit_count: int = 0
    context_build_duration: float = 0.0
    response_duration: float = 0.0

    def to_metadata(self) -> dict:
        """转为 tool 输出中的可选 ``metadata`` 字段（仅描述 Context）。"""
        return {
            "version": __version__,
            "runtime_context_available": self.runtime_context_available,
            "runtime_context_size": self.runtime_context_size,
            "experience_enabled": self.experience_enabled,
            "experience_hit_count": self.experience_hit_count,
            "context_build_duration_ms": round(self.context_build_duration * 1000, 2),
            "response_duration_ms": round(self.response_duration * 1000, 2),
        }


def _observe_experience_count(context: dict) -> int:
    """观察已有 Context 中携带的经验记录数（纯观察，不主动检索 / 不触发 RAG）。"""
    for key in _EXPERIENCE_KEYS:
        value = context.get(key)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            return 1
    return 0


def observe_context(
    request_id: str = "",
    trace_id: str = "",
    context: dict | None = None,
    context_build_duration: float = 0.0,
    response_duration: float = 0.0,
) -> DebugContextTrace:
    """组装一条 DebugContextTrace（纯观测，不修改业务逻辑、不调用 RAG）。"""
    available = bool(context) and isinstance(context, dict)
    size = 0
    if available:
        try:
            size = len(json.dumps(context, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            size = 0
    return DebugContextTrace(
        request_id=request_id,
        trace_id=trace_id,
        runtime_context_available=available,
        runtime_context_size=size,
        experience_enabled=settings.debug_experience_enabled,
        experience_hit_count=_observe_experience_count(context) if available else 0,
        context_build_duration=context_build_duration,
        response_duration=response_duration,
    )


def attach_metadata(result: dict, trace: DebugContextTrace) -> dict:
    """向 tool 结果注入可选 ``metadata`` 字段（就地修改并返回，向后兼容）。"""
    result["metadata"] = trace.to_metadata()
    return result


__all__ = [
    "DebugContextTrace",
    "observe_context",
    "attach_metadata",
]