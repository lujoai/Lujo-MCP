"""MCP 调试工具 —— 一键运行调试流程 / 运行时快照 / 调试上下文 / LLM 分析"""

import logging
import time

from app.runtime.core.logs import create_request_id, add_log, get_logs
from app.runtime.context.builder import build_context, build_debug_context
from app.runtime.collectors.runtime import collect_runtime_snapshot
from app.llm.analyzer import analyze
from app.mcp.observability import observe_context, attach_metadata

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
    start = time.perf_counter()
    payload = arguments.get("payload", {})
    metadata = arguments.get("metadata")

    request_id = create_request_id()
    add_log(request_id, "mcp_debug_start", payload)
    add_log(request_id, "mcp_processing", {"metadata": metadata})
    result = {"echo": payload, "status": "success"}
    add_log(request_id, "mcp_response_ready", result)

    trace = get_logs(request_id)
    context = build_context(request_id, trace)
    build_duration = time.perf_counter() - start

    output = {
        "request_id": request_id,
        "result": result,
        "trace": trace,
        "context": context,
    }

    # Phase 3 D5：注入可观测 metadata（仅描述 Context，向后兼容）
    trace_obs = observe_context(
        request_id=request_id,
        context=context,
        context_build_duration=build_duration,
        response_duration=time.perf_counter() - start,
    )
    return attach_metadata(output, trace_obs)


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
    start = time.perf_counter()
    ctx = build_debug_context(trace_id)
    if ctx is None:
        return {"message": "暂无捕获到的错误上下文"}
    build_duration = time.perf_counter() - start

    # Phase 3 D5：注入可观测 metadata（仅描述 Context，向后兼容）
    trace_obs = observe_context(
        trace_id=trace_id or "",
        context=ctx,
        context_build_duration=build_duration,
        response_duration=time.perf_counter() - start,
    )
    return attach_metadata(ctx, trace_obs)


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
