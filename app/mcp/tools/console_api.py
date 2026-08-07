"""
MCP 工具：ingest_console —— 供浏览器 SDK 上报控制台日志。
"""
import logging

from app.runtime.core.trace_repo import save_console_log

logger = logging.getLogger("ai-debug-mcp.tools.console")

INGEST_CONSOLE_DEF = {
    "name": "ingest_console",
    "description": (
        "供浏览器 SDK 上报控制台日志（console.error / console.warn）。"
        "上报后会自动脱敏并进入统一调试上下文。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "level": {"type": "string", "default": "info", "description": "日志级别：error / warn / info"},
            "message": {"type": "string", "description": "日志消息文本"},
            "source": {"type": "string", "default": "browser_sdk", "description": "日志来源标识"},
            "extra": {"type": "object", "default": {}, "description": "额外上下文"},
            "trace_id": {"type": "string", "description": "关联的 trace ID"},
            "request_id": {"type": "string", "description": "关联的 request ID"},
        },
        "required": ["message"],
    },
}


def tool_ingest_console(
    level: str = "info",
    message: str = "",
    source: str = "browser_sdk",
    extra: dict | None = None,
    trace_id: str | None = None,
    request_id: str | None = None,
) -> dict:
    """接收浏览器控制台日志上报，落库后返回 record_id。"""
    record_id = save_console_log(
        level=level,
        message=message,
        source=source,
        extra=extra,
        trace_id=trace_id,
        request_id=request_id,
    )
    return {"record_id": record_id, "trace_id": trace_id, "saved": True}


def ingest_console_handler(arguments: dict) -> dict:
    return tool_ingest_console(
        level=arguments.get("level", "info"),
        message=arguments.get("message", ""),
        source=arguments.get("source", "browser_sdk"),
        extra=arguments.get("extra"),
        trace_id=arguments.get("trace_id"),
        request_id=arguments.get("request_id"),
    )
