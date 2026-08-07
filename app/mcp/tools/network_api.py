"""
MCP 工具：ingest_network / get_network_trace。

- ingest_network：单条网络请求上报（浏览器 SDK / 中间件 / 外部服务调用）。
- get_network_trace：查询与某 trace 关联的所有网络请求记录。

复用 trace_repo 存取层，脱敏在存储边界统一执行。
"""
from app.runtime.collectors.network import parse_network_record
from app.runtime.core.trace_repo import save_network_record, get_network_records


# ── HTTP 侧注册用 TOOL_DEF（M8 注册）──
NETWORK_INGEST_DEF = {
    "name": "ingest_network",
    "description": "单条上报网络请求记录，通常由浏览器 SDK 或中间件调用。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "record": {"type": "object", "description": "网络请求记录"},
            "trace_id": {"type": "string", "description": "关联的 trace_id"},
            "request_id": {"type": "string", "description": "关联的 request_id"},
        },
        "required": ["record"],
    },
}

NETWORK_TRACE_DEF = {
    "name": "get_network_trace",
    "description": "查询与某条 trace 关联的所有网络请求记录。",
    "inputSchema": {
        "type": "object",
        "properties": {"trace_id": {"type": "string", "description": "追踪 ID"}},
        "required": ["trace_id"],
    },
}


def tool_ingest_network(record: dict, trace_id: str | None = None, request_id: str | None = None) -> dict:
    """解析并保存一条网络记录，返回 record_id 和关联的 trace_id。"""
    parsed = parse_network_record(record)
    record_id = save_network_record(parsed, trace_id=trace_id, request_id=request_id)
    return {"record_id": record_id, "trace_id": trace_id, "saved": True}


def tool_get_network_trace(trace_id: str) -> dict:
    """查询指定 trace_id 关联的所有网络请求记录。"""
    records = get_network_records(trace_id)
    return {
        "found": bool(records),
        "count": len(records),
        "records": records,
    }


# ── MCP handler（接收 arguments dict，供 register_tool 使用）──
def ingest_network_handler(arguments: dict) -> dict:
    return tool_ingest_network(
        record=arguments.get("record", {}),
        trace_id=arguments.get("trace_id"),
        request_id=arguments.get("request_id"),
    )


def get_network_trace_handler(arguments: dict) -> dict:
    return tool_get_network_trace(arguments["trace_id"])
