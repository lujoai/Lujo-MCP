"""MCP 调试工具 —— 一键运行调试流程 / 运行时快照 / 调试上下文 / LLM 分析"""

import logging

from app.mcp.core.logs import create_request_id, add_log, get_logs
from app.mcp.builders.context import build_context, build_debug_context
from app.mcp.collectors.runtime import collect_runtime_snapshot
from app.llm.analyzer import analyze

logger = logging.getLogger("ai-debug-mcp.debug-tool")

TOOL_DEF = {
    "name": "debug",
    "description": "执行完整调试流程：接收请求数据，记录执行链路，返回结构化调试上下文",
    "inputSchema": {
        "type": "object",
        "properties": {
            "payload": {"type": "object", "description": "要调试的请求数据"},
            "metadata": {"type": "object", "description": "附加元数据"},
        },
    },
}


def handler(arguments: dict) -> dict:
    """MCP 工具 handler（接收 dict 参数，返回 dict 结果）"""
    payload = arguments.get("payload", {})
    metadata = arguments.get("metadata")

    request_id = create_request_id()
    add_log(request_id, "mcp_debug_start", payload)
    add_log(request_id, "mcp_processing", {"metadata": metadata})
    result = {"echo": payload, "status": "success"}
    add_log(request_id, "mcp_response_ready", result)

    trace = get_logs(request_id)
    context = build_context(request_id, trace)

    return {
        "request_id": request_id,
        "result": result,
        "trace": trace,
        "context": context,
    }


# 兼容旧调用方式
def invoke(body) -> dict:
    return handler({"payload": getattr(body, "arguments", {}).get("payload", {})})


def get_runtime_snapshot() -> dict:
    """获取当前进程运行时快照（CPU/内存/线程等）。"""
    return collect_runtime_snapshot()


def get_debug_context(trace_id: str | None = None) -> dict:
    """【核心工具】一次性获取某次错误的完整调试上下文：
    异常堆栈 + 运行时快照 + 源码片段 + git 归因 + 网络链 + UI 事件。
    """
    ctx = build_debug_context(trace_id)
    if ctx is None:
        return {"message": "暂无捕获到的错误上下文"}
    return ctx


def analyze_with_llm(trace_id: str | None = None) -> dict:
    """对指定/最近捕获的异常做 LLM 根因分析。"""
    ctx = build_debug_context(trace_id)
    if ctx is None:
        return {"message": "暂无捕获到的错误上下文"}
    try:
        return analyze(ctx)
    except RuntimeError as e:
        logger.error(str(e), exc_info=True)
        return {"error": "Tool execution failed"}
