"""
MCP 工具：ingest_error —— 供任意语言/进程主动上报错误。

非 Python 运行时（Node.js / Go / Rust / Java 等）可把自身异常解析后按一致结构上报，
复用同一套存储、脱敏与调试上下文逻辑（trace_repo）。按 proj1 架构重写。
"""
import logging

from app.runtime.core.trace_repo import save_trace

logger = logging.getLogger("lujo-mcp.tools.ingest")

INGEST_ERROR_DEF = {
    "name": "ingest_error",
    "description": (
        "供任意语言/进程主动上报一条错误（不限于 Python）。"
        "字段与 get_stacktrace 返回结构一致，上报后会自动脱敏并进入统一调试上下文。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "exc_type": {"type": "string", "description": "异常类型，如 NullPointerException / ValueError"},
            "message": {"type": "string", "description": "异常消息文本"},
            "frames": {
                "type": "array",
                "items": {"type": "object"},
                "description": "堆栈帧列表，每项含 file/line/function/code_context",
            },
            "source": {"type": "string", "default": "ingest", "description": "错误来源标识"},
            "extra": {"type": "object", "default": {}, "description": "额外上下文"},
        },
        "required": ["exc_type", "message"],
    },
}


def _parse_frames(frames) -> list[dict]:
    """规范化外部上报的堆栈帧，丢弃缺 file/line 的无效项。"""
    out: list[dict] = []
    for f in frames or []:
        if not isinstance(f, dict):
            continue
        file = f.get("file")
        line = f.get("line")
        if not file or line is None:
            continue
        try:
            line_int = int(line)
        except (TypeError, ValueError):
            continue
        out.append({
            "file": str(file),
            "line": line_int,
            "function": f.get("function") or "unknown",
            "code": f.get("code") or f.get("code_context") or "",
        })
    return out


def tool_ingest_error(
    exc_type: str,
    message: str,
    frames: list | None = None,
    source: str = "ingest",
    extra: dict | None = None,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> dict:
    """接收外部上报的错误，落库后返回 trace_id。"""
    frames = _parse_frames(frames)
    result_trace_id = save_trace(
        exc_type=exc_type,
        message=message,
        frames=frames,
        source=source,
        extra=extra or {},
        trace_kind="exception",
        trace_id=trace_id,
        session_id=session_id,
    )
    return {"trace_id": result_trace_id, "saved": True, "frame_count": len(frames)}


def ingest_error_handler(arguments: dict) -> dict:
    return tool_ingest_error(
        exc_type=arguments.get("exc_type", "UnknownError"),
        message=arguments.get("message", ""),
        frames=arguments.get("frames", []),
        source=arguments.get("source", "ingest"),
        extra=arguments.get("extra"),
        trace_id=arguments.get("trace_id"),
        session_id=arguments.get("session_id"),
    )
