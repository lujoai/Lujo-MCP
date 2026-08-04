"""MCP 工具：repair_async / repair_result —— AI Debug Agent Phase 1。

repair_async：异步触发修复方案生成，返回 job_id。
repair_result：查询修复任务状态/结果。

需 settings.agent_enabled=True，否则返回 error。
与 /api/debug/repair/* REST 端点共享业务逻辑（通过 RepairQueue 单例）。
"""

from __future__ import annotations

import logging
from typing import Any

from app.agent.repair_queue import get_repair_queue
from app.config import settings
from app.mcp.builders.context import build_context
from app.mcp.collectors.runtime import collect_runtime_snapshot
from app.mcp.core.logs import get_logs

logger = logging.getLogger("ai-debug-mcp.mcp.tools.repair")


REPAIR_ASYNC_DEF = {
    "name": "repair_async",
    "description": (
        "异步生成可执行修复方案（AI Debug Agent）：基于调试上下文、历史相似修复、"
        "git 近期改动，由 RepairAgent 生成结构化修复方案"
        "（含 patch / affected_files / validation_strategy / risk_assessment / confidence）。"
        "返回 job_id，客户端轮询 repair_result 取结果。"
        "需 settings.agent_enabled=True。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "request_id": {
                "type": "string",
                "description": "请求 ID 或 trace_id",
            },
            "trace_id": {
                "type": "string",
                "description": "等价于 request_id，二选一",
            },
        },
        "required": ["request_id"],
    },
}


REPAIR_RESULT_DEF = {
    "name": "repair_result",
    "description": (
        "查询 repair_async 异步任务的状态/结果。"
        "返回 {status, result, error, created_at, finished_at}。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "repair_async 返回的 job_id"}
        },
        "required": ["job_id"],
    },
}


async def repair_async_handler(arguments: dict[str, Any]) -> dict[str, Any]:
    """repair_async 工具处理函数。

    与 /api/debug/repair/async 端点共享逻辑：build_debug_context → enqueue。
    """
    if not settings.agent_enabled:
        return {"error": "agent disabled", "_hint": "set AGENT_ENABLED=true"}

    request_id = arguments.get("request_id") or arguments.get("trace_id")
    if not request_id:
        return {"error": "must provide request_id or trace_id"}

    try:
        trace = get_logs(request_id)
    except Exception as e:
        logger.error(str(e), exc_info=True)
        return {"error": "internal error"}

    if not trace:
        return {"error": f"request {request_id} not found"}

    try:
        context = build_context(request_id, trace)
    except Exception as e:
        logger.error(str(e), exc_info=True)
        return {"error": "build context failed"}

    # 与 /analyze 一致：errors 中含堆栈帧则提升到 exception
    for err in context.get("errors", []):
        if isinstance(err, dict) and err.get("frames"):
            context["exception"] = err
            break

    try:
        context["runtime"] = collect_runtime_snapshot()
    except Exception:
        context["runtime"] = {}

    try:
        job_id = await get_repair_queue().enqueue(context, model=None)
    except Exception:
        return {
            "error": "queue_full",
            "queue_size": get_repair_queue().queue_size(),
        }

    return {"job_id": job_id, "status": "queued"}


async def repair_result_handler(arguments: dict[str, Any]) -> dict[str, Any]:
    """repair_result 工具处理函数。"""
    if not settings.agent_enabled:
        return {"error": "agent disabled", "_hint": "set AGENT_ENABLED=true"}

    job_id = arguments.get("job_id")
    if not job_id:
        return {"error": "must provide job_id"}

    job = get_repair_queue().get_job(job_id)
    if job is None:
        return {"error": f"job {job_id} not found"}
    return job
