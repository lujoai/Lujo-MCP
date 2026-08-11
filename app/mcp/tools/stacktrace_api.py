"""MCP 堆栈追踪工具 —— 获取异常调用栈"""

import sys
import time
from app.runtime.collectors.stacktrace import capture_exception, format_trace_for_ai
from app.runtime.collectors.code_locator import get_snippets_for_frames
from app.runtime.core.logs import get_logs
from app.mcp.observability import observe_context, attach_metadata

TOOL_DEF = {
    "name": "stacktrace",
    "description": "捕获当前异常或指定请求关联的错误堆栈信息",
    "inputSchema": {
        "type": "object",
        "properties": {
            "request_id": {"type": "string", "description": "请求 ID（可选，用于关联）"},
        },
    },
}


def handler(arguments: dict) -> dict:
    """MCP 工具 handler"""
    request_id = arguments.get("request_id")

    # 尝试获取当前线程的异常
    exc_info = sys.exc_info()
    if exc_info[1] is not None:
        exc_data = capture_exception(exc_info[1])
    elif request_id:
        trace = get_logs(request_id)
        errors = [item for item in trace if item.get("step") == "error"]
        if errors:
            exc_data = {
                "type": "LoggedError",
                "message": str(errors[-1].get("data", "")),
                "traceback": str(errors),
                "frames": [],
                "frame_count": 0,
            }
        else:
            return {
                "request_id": request_id,
                "message": "当前请求没有捕获到异常",
            }
    else:
        return {"message": "当前上下文中没有异常"}

    # 附加每帧源码片段（FR11：免手动翻找代码文件）
    frames = exc_data.get("frames", [])
    code_snippets = (
        [s.model_dump() for s in get_snippets_for_frames(frames)]
        if frames
        else []
    )

    return {
        "request_id": request_id,
        "exception": exc_data,
        "code_snippets": code_snippets,
        "ai_summary": format_trace_for_ai(exc_data),
    }


def invoke(body) -> dict:
    return handler({"request_id": getattr(body, "request_id", None)})


def get_stacktrace(trace_id: str | None = None) -> dict:
    """返回指定/最近捕获的异常堆栈（含每帧源码片段）。

    供 stdio MCP 工具使用：优先取近期异常存储中的记录，
    否则退而取当前线程未捕获异常。
    """
    from app.runtime.core.errors import get_by_id, get_latest

    _start = time.perf_counter()
    err = get_by_id(trace_id) if trace_id else get_latest()
    if err is None:
        exc_info = sys.exc_info()
        if exc_info[1] is not None:
            err = capture_exception(exc_info[1])
        else:
            return {"message": "当前没有捕获到异常"}

    frames = err.get("frames", [])
    code_snippets = (
        [s.model_dump() for s in get_snippets_for_frames(frames)]
        if frames
        else []
    )
    exception = {
        "type": err.get("type"),
        "message": err.get("message"),
        "traceback": err.get("traceback"),
        "frames": frames,
        "frame_count": err.get("frame_count", len(frames)),
    }
    output = {
        "error_id": err.get("error_id"),
        "exception": exception,
        "code_snippets": code_snippets,
        "ai_summary": format_trace_for_ai(exception),
    }
    # Phase 3 D5：注入可观测 metadata（仅描述 Context，向后兼容）
    trace_obs = observe_context(
        request_id=trace_id or "",
        context=output,
        response_duration=time.perf_counter() - _start,
    )
    return attach_metadata(output, trace_obs)
