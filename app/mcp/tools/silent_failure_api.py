"""
MCP 工具：ingest_silent_failure —— 接收浏览器 SDK 上报的前端静默失败。

静默失败 = 用户期望的行为（如点击后跳转、请求后更新状态）未在预期内发生，
且没有显式异常。本工具把 UI 事件链 + 网络请求链 + 期望行为作为一条
trace_kind="silent_failure" 的记录关联入库，供 get_debug_context 取回。

编排 M3(network) / M4(ui_event) 采集器 + M2(trace_repo)，按 proj1 架构重写。
"""
import logging

from app.mcp.collectors.network import parse_network_records
from app.mcp.collectors.ui_event import parse_ui_events
from app.mcp.core.trace_repo import save_trace, save_ui_event, save_network_record

logger = logging.getLogger("ai-debug-mcp.tools.silent_failure")

SILENT_FAILURE_DEF = {
    "name": "ingest_silent_failure",
    "description": (
        "上报一条前端静默失败：用户期望发生的行为（如点击后跳转、请求后更新状态）未在指定时间内出现，"
        "且没有显式异常。需要包含 UI 事件链、网络请求链和期望行为描述。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "静默失败描述"},
            "frames": {"type": "array", "items": {"type": "object"}, "description": "相关源码位置"},
            "ui_events": {"type": "array", "items": {"type": "object"}, "description": "UI 事件链"},
            "network_records": {"type": "array", "items": {"type": "object"}, "description": "网络请求链"},
            "expectation": {"type": "object", "description": "用户期望行为"},
            "source": {"type": "string", "default": "browser_sdk"},
            "extra": {"type": "object", "default": {}},
        },
        "required": ["message"],
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


def tool_ingest_silent_failure(
    message: str,
    frames: list | None = None,
    ui_events: list | None = None,
    network_records: list | None = None,
    expectation: dict | None = None,
    source: str = "browser_sdk",
    extra: dict | None = None,
    trace_id: str | None = None,
) -> dict:
    """保存一条前端静默失败 trace，同时关联 UI 事件与网络请求。"""
    frames = _parse_frames(frames)
    ui_events = parse_ui_events(ui_events)
    network_records = parse_network_records(network_records)
    expectation = expectation or {}
    extra = extra or {}
    extra["expectation"] = expectation
    extra["ui_event_count"] = len(ui_events)
    extra["network_record_count"] = len(network_records)

    result_trace_id = save_trace(
        exc_type="SilentFailure",
        message=message,
        frames=frames,
        source=source,
        extra=extra,
        trace_kind="silent_failure",
        trace_id=trace_id,
    )

    # 关联入库（单条失败不阻断整体）
    for event in ui_events:
        try:
            save_ui_event(event, trace_id=result_trace_id)
        except Exception:
            logger.warning("保存 ui_event 失败 (trace_id=%s)", result_trace_id)
    for record in network_records:
        try:
            save_network_record(record, trace_id=result_trace_id)
        except Exception:
            logger.warning("保存 network_record 失败 (trace_id=%s)", result_trace_id)

    return {
        "trace_id": result_trace_id,
        "saved": True,
        "ui_events": len(ui_events),
        "network_records": len(network_records),
    }


def silent_failure_handler(arguments: dict) -> dict:
    return tool_ingest_silent_failure(
        message=arguments.get("message", ""),
        frames=arguments.get("frames"),
        ui_events=arguments.get("ui_events"),
        network_records=arguments.get("network_records"),
        expectation=arguments.get("expectation"),
        source=arguments.get("source", "browser_sdk"),
        extra=arguments.get("extra"),
        trace_id=arguments.get("trace_id"),
    )
