"""MCP 上下文工具 —— 获取请求的调试上下文"""

from app.runtime.core.logs import get_logs
from app.runtime.context.builder import build_context
from app.runtime.collectors.code_locator import get_snippets_for_frames

TOOL_DEF = {
    "name": "context",
    "description": "根据请求 ID 获取结构化的调试上下文，包含执行流程、输入输出、错误信息",
    "inputSchema": {
        "type": "object",
        "properties": {
            "request_id": {"type": "string", "description": "请求 ID"},
        },
        "required": ["request_id"],
    },
}


def handler(arguments: dict) -> dict:
    """MCP 工具 handler"""
    request_id = arguments.get("request_id", "")
    trace = get_logs(request_id)

    if not trace:
        return {
            "error": f"找不到请求 {request_id} 的追踪记录",
            "request_id": request_id,
        }

    context = build_context(request_id, trace)

    # FR11：从 errors 中提取含堆栈帧的异常，附加源码片段
    frames = []
    for err in context.get("errors", []):
        if isinstance(err, dict) and err.get("frames"):
            frames.extend(err["frames"])
    if frames:
        context["code_snippets"] = [
            s.model_dump() for s in get_snippets_for_frames(frames)
        ]

    return context


def invoke(body) -> dict:
    return handler({"request_id": body.request_id})
