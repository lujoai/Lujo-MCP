"""
MCP 工具：ingest_network / get_network_trace。

- ingest_network：单条网络请求上报（浏览器 SDK / 中间件 / 外部服务调用）。
- get_network_trace：查询与某 trace 关联的所有网络请求记录。

复用 trace_repo 存取层，脱敏在存储边界统一执行。
"""
from app.mcp.protocol.tool_errors import ToolExecutionError
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
            "session_id": {"type": "string", "description": "会话 ID"},
        },
        "required": ["record"],
    },
}

NETWORK_TRACE_DEF = {
    "name": "get_network_trace",
    "description": (
        "查询与某条 trace_id 关联的所有网络请求记录（请求体/响应体/耗时/状态码）。"
        "需要 trace_id：先调用 diagnose_issue 拿到 trace_id；"
        "适合排查接口超时、响应异常、前端请求链路等问题；纯代码问题不要调用。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "trace_id": {"type": "string", "description": "追踪 ID"},
            "session_id": {"type": "string", "description": "会话 ID"},
        },
        "required": ["trace_id"],
    },
}


def tool_ingest_network(
    record: dict,
    trace_id: str | None = None,
    request_id: str | None = None,
    session_id: str | None = None,
) -> dict:
    """解析并保存一条网络记录，返回 record_id 和关联的 trace_id。"""
    parsed = parse_network_record(record)
    record_id = save_network_record(
        parsed, trace_id=trace_id, request_id=request_id, session_id=session_id
    )
    return {"record_id": record_id, "trace_id": trace_id, "saved": True}


def tool_get_network_trace(trace_id: str, session_id: str | None = None) -> dict:
    """查询指定 trace_id 关联的所有网络请求记录。

    无数据时补 scope + next_step（对齐 diagnose_issue 的无数据契约）：
    宿主 AI 需要知道查询范围与如何产生数据（网络记录必须先经 ingest_network
    上报，且传 trace_id 参数才会挂到该 trace 下），而不是拿到空结果自己猜。
    """
    records = get_network_records(trace_id, session_id=session_id)
    if not records:
        return {
            "found": False,
            "count": 0,
            "records": [],
            "scope": {"trace_id": trace_id, "session_id": session_id},
            "next_step": (
                "该查询范围内暂无网络请求记录：数据需先经 ingest_network 上报，"
                "且上报时必须传 trace_id 参数，记录才会关联到本 trace"
                "（浏览器端由 Browser SDK 经 HTTP 自动上报，stdio 模式不接收）。"
                "完成上报后用同一 trace_id 重查本工具。"
            ),
        }
    return {
        "found": True,
        "count": len(records),
        "records": records,
    }


# ── MCP handler（接收 arguments dict，供 register_tool 使用）──
def ingest_network_handler(arguments: dict) -> dict:
    from app.mcp.tools.param_contract import require_text_in_record

    record = arguments.get("record")
    if not isinstance(record, dict) or not record:
        raise ToolExecutionError(
            "record 为必填对象（网络记录字典，含 url/status_code 等字段），收到的是 "
            f"{record!r}——注意不是把字段平铺在参数顶层"
        )
    require_text_in_record(record, "url")
    return tool_ingest_network(
        record=record,
        trace_id=arguments.get("trace_id"),
        request_id=arguments.get("request_id"),
        session_id=arguments.get("session_id"),
    )


def get_network_trace_handler(arguments: dict) -> dict:
    return tool_get_network_trace(
        arguments["trace_id"], session_id=arguments.get("session_id")
    )
