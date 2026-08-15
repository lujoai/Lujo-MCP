"""
MCP 工具：ingest_silent_failure —— 接收浏览器 SDK 上报的前端静默失败。

静默失败 = 用户期望的行为（如点击后跳转、请求后更新状态）未在预期内发生，
且没有显式异常。本工具把 UI 事件链 + 网络请求链 + 期望行为作为一条
trace_kind="silent_failure" 的记录关联入库，供 get_debug_context 取回。

编排 M3(network) / M4(ui_event) 采集器 + M2(trace_repo)，按 proj1 架构重写。
"""
import logging

from app.runtime.collectors.network import parse_network_records
from app.runtime.collectors.ui_event import parse_ui_events
from app.runtime.core.trace_repo import save_trace, save_ui_event, save_network_record

logger = logging.getLogger("lujo-mcp.tools.silent_failure")

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
            "observed": {
                "type": "string",
                "description": "用户对现象的文字描述，如'点击后无反应'。SDK reportSilentFailure(payload) 的 payload.observed 透传。",
            },
            "observed_events": {
                "type": "array",
                "description": (
                    "最近 N 条事件链（SDK 自动附加，N 默认 20）。元素结构 {kind:'network'|'ui', data:{...}}。"
                    "kind='network' 的 data 形如 {method,url,status_code,duration_ms,timestamp,request_body_preview,error}；"
                    "kind='ui' 的 data 形如 {event_type,target_selector,target_text,timestamp,route_path}。"
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["network", "ui"]},
                        "data": {"type": "object"},
                    },
                    "required": ["kind", "data"],
                },
            },
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


def _split_observed_events(observed_events: list | None) -> tuple[list[dict], list[dict], list[dict]]:
    """把 observed_events 按 kind 分流为 (network_records, ui_events, unknown)。

    - kind='network' 的 data 视为 network 记录原始 dict
    - kind='ui' 的 data 视为 UI 事件原始 dict
    - kind 缺失或非 network/ui 的，原样保留到 unknown（不丢弃，约束 2）

    返回的三组都未经过 parse_* 校验，由调用方按需 parse。
    """
    network_raw: list[dict] = []
    ui_raw: list[dict] = []
    unknown: list[dict] = []
    for item in observed_events or []:
        if not isinstance(item, dict):
            unknown.append({"raw": item})
            continue
        kind = item.get("kind")
        data = item.get("data")
        if kind == "network" and isinstance(data, dict):
            network_raw.append(data)
        elif kind == "ui" and isinstance(data, dict):
            ui_raw.append(data)
        else:
            unknown.append(item)
    return network_raw, ui_raw, unknown


def tool_ingest_silent_failure(
    message: str,
    frames: list | None = None,
    ui_events: list | None = None,
    network_records: list | None = None,
    expectation: dict | None = None,
    observed: str | None = None,
    observed_events: list | None = None,
    source: str = "browser_sdk",
    extra: dict | None = None,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> dict:
    """保存一条前端静默失败 trace，同时关联 UI 事件与网络请求。

    observed_events 中的事件会按 kind 分流，与外部传入的 network_records/ui_events
    合并入库；无法识别 kind 的事件保留到 extra['observed_events_unknown']，不丢弃。
    """
    frames = _parse_frames(frames)
    ui_events = parse_ui_events(ui_events)
    network_records = parse_network_records(network_records)

    # observed_events 按 kind 分流（约束 2：unknown 不丢弃）
    obs_network_raw, obs_ui_raw, obs_unknown = _split_observed_events(observed_events)
    obs_network = parse_network_records(obs_network_raw)
    obs_ui = parse_ui_events(obs_ui_raw)
    observed_event_count = len(observed_events or [])
    observed_event_merged_count = len(obs_network) + len(obs_ui)
    observed_event_unknown_count = len(obs_unknown)

    if obs_unknown:
        logger.warning(
            "observed_events 含 %d 条无法识别 kind 的事件，已保留到 extra.observed_events_unknown",
            observed_event_unknown_count,
        )

    # 合并外部传入与 observed_events 分流出的事件
    all_ui_events = list(ui_events) + list(obs_ui)
    all_network_records = list(network_records) + list(obs_network)

    expectation = expectation or {}
    extra = extra or {}
    if observed is not None:
        extra["observed"] = observed
    extra["expectation"] = expectation
    extra["ui_event_count"] = len(all_ui_events)
    extra["network_record_count"] = len(all_network_records)
    extra["observed_event_count"] = observed_event_count
    extra["observed_event_merged_count"] = observed_event_merged_count
    extra["observed_event_unknown_count"] = observed_event_unknown_count
    if obs_unknown:
        extra["observed_events_unknown"] = obs_unknown

    result_trace_id = save_trace(
        exc_type="SilentFailure",
        message=message,
        frames=frames,
        source=source,
        extra=extra,
        trace_kind="silent_failure",
        trace_id=trace_id,
        session_id=session_id,
    )

    # 关联入库（单条失败不阻断整体）
    for event in all_ui_events:
        try:
            save_ui_event(event, trace_id=result_trace_id)
        except Exception:
            logger.warning("保存 ui_event 失败 (trace_id=%s)", result_trace_id)
    for record in all_network_records:
        try:
            save_network_record(record, trace_id=result_trace_id)
        except Exception:
            logger.warning("保存 network_record 失败 (trace_id=%s)", result_trace_id)

    return {
        "trace_id": result_trace_id,
        "saved": True,
        "ui_events": len(all_ui_events),
        "network_records": len(all_network_records),
        "observed_event_count": observed_event_count,
        "observed_event_merged_count": observed_event_merged_count,
        "observed_event_unknown_count": observed_event_unknown_count,
    }


def silent_failure_handler(arguments: dict) -> dict:
    return tool_ingest_silent_failure(
        message=arguments.get("message", ""),
        frames=arguments.get("frames"),
        ui_events=arguments.get("ui_events"),
        network_records=arguments.get("network_records"),
        expectation=arguments.get("expectation"),
        observed=arguments.get("observed"),
        observed_events=arguments.get("observed_events"),
        source=arguments.get("source", "browser_sdk"),
        extra=arguments.get("extra"),
        trace_id=arguments.get("trace_id"),
        session_id=arguments.get("session_id"),
    )
