"""MCP 追踪工具 —— 获取原始追踪日志 / 检索近期捕获的异常"""

from app.runtime.core.logs import get_logs, list_request_ids
from app.runtime.core.errors import list_recent, search as search_errors

TOOL_DEF = {
    "name": "trace",
    "description": "获取请求的完整原始追踪日志（时间、步骤、数据的时序列表）",
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
    return {
        "request_id": request_id,
        "trace": trace,
        "step_count": len(trace),
    }


def invoke(body) -> dict:
    return handler({"request_id": body.request_id})


def _top_frame(frames: list) -> str | None:
    if not frames:
        return None
    f = frames[0]
    return f"{f.get('file', '?')}:{f.get('line', 0)} in {f.get('function', '?')}"


def _extract_trace_summary(request_id: str) -> dict | None:
    """从存储中提取 trace 摘要信息"""
    entries = get_logs(request_id)
    if not entries:
        return None

    summary = {
        "trace_id": request_id,
        "error_id": request_id,
        "fingerprint": "",
        "type": "debug",
        "message": "",
        "source": "storage",
        "occurrence_count": 1,
        # FIX(v0.7.1-b1-8): first_seen 用 None 做哨兵：此前用 0 哨兵，遇到真实的
        # 0 时间戳条目后 `first_seen == 0` 恒成立，后续任意 ts（含更大值）都会
        # 覆盖，first_seen 退化为「最后一条时间」。entries 非空保证循环后必为数值。
        "first_seen": None,
        "last_seen": 0,
        "timestamp": 0,
        "top_frame": None,
    }

    for entry in entries:
        ts = entry.get("timestamp", 0)
        if ts > summary["timestamp"]:
            summary["timestamp"] = ts
            summary["last_seen"] = ts
        if summary["first_seen"] is None or ts < summary["first_seen"]:
            summary["first_seen"] = ts

        step = entry.get("step", "")
        data = entry.get("data")

        if isinstance(data, str):
            if step == "error":
                summary["type"] = "ERROR"
                summary["message"] = data[:200]
            elif step == "request_start":
                summary["message"] = data[:200]
            elif step == "response_ready":
                summary["type"] = "RESPONSE"
            continue

        if not isinstance(data, dict):
            continue

        if step == "request_start":
            summary["type"] = data.get("method", "REQUEST")
            summary["message"] = data.get("url", "")[:200]
        elif step == "response_ready":
            status = data.get("status", 0)
            summary["type"] = f"RESPONSE {status}"
            if status >= 400:
                summary["type"] = "ERROR"
        elif step == "error":
            summary["type"] = data.get("error_type", "ERROR")
            summary["message"] = (data.get("message", "") or "")[:200]
            summary["top_frame"] = _top_frame(data.get("frames", []))
        elif step == "exception":
            summary["type"] = data.get("type", "Exception")
            summary["message"] = (data.get("message", "") or "")[:200]
            summary["top_frame"] = _top_frame(data.get("frames", []))

    return summary


def list_recent_traces(limit: int = 10, session_id: str | None = None) -> list:
    """列出最近被自动捕获的异常摘要（含指纹/发生次数/首末时间，不含完整堆栈）。"""
    results = []
    memory_items = list_recent(limit, session_id=session_id)
    for e in memory_items:
        results.append({
            "trace_id": e["error_id"],
            "error_id": e["error_id"],
            "fingerprint": e["fingerprint"],
            "type": e["type"],
            "message": e["message"],
            "source": e["source"],
            "occurrence_count": e["occurrence_count"],
            "first_seen": e["first_seen"],
            "last_seen": e["last_seen"],
            "timestamp": e["timestamp"],
            "top_frame": _top_frame(e["frames"]),
        })

    storage_ids = list_request_ids(limit=limit)
    seen_ids = {item["trace_id"] for item in results}
    for rid in storage_ids:
        if rid not in seen_ids:
            summary = _extract_trace_summary(rid)
            if summary:
                results.append(summary)
                seen_ids.add(rid)

    results.sort(key=lambda e: e.get("last_seen", e.get("timestamp", 0)), reverse=True)
    return results[:limit]


def search_logs(keyword: str, since_minutes: int = 30, session_id: str | None = None) -> list:
    """按关键字 + 时间窗搜索近期捕获的异常（含指纹/发生次数）。"""
    keyword = (keyword or "").lower()
    cutoff = 0
    if since_minutes > 0:
        import time
        cutoff = time.time() - since_minutes * 60

    results = []
    memory_items = search_errors(keyword, since_minutes, session_id=session_id)
    for e in memory_items:
        last_seen = e.get("last_seen", e.get("timestamp", 0))
        if last_seen >= cutoff:
            results.append({
                "trace_id": e["error_id"],
                "error_id": e["error_id"],
                "fingerprint": e["fingerprint"],
                "type": e["type"],
                "message": e["message"],
                "source": e["source"],
                "occurrence_count": e["occurrence_count"],
                "last_seen": last_seen,
                "top_frame": _top_frame(e["frames"]),
            })

    storage_ids = list_request_ids(limit=50)
    seen_ids = {item["trace_id"] for item in results}
    for rid in storage_ids:
        if rid not in seen_ids:
            summary = _extract_trace_summary(rid)
            if summary:
                ts = summary.get("last_seen", summary.get("timestamp", 0))
                if ts >= cutoff:
                    msg_lower = (summary.get("message") or "").lower()
                    type_lower = (summary.get("type") or "").lower()
                    if keyword in msg_lower or keyword in type_lower:
                        results.append(summary)
                        seen_ids.add(rid)

    results.sort(key=lambda e: e.get("last_seen", 0), reverse=True)
    return results
