"""调试上下文构建器 —— 安全地解析追踪日志"""

import logging

logger = logging.getLogger("ai-debug-mcp.context")


def build_context(request_id: str, logs: list) -> dict:
    """
    将原始 logs 转换成 AI 可理解的 debug context。
    对格式异常的日志记录做保护性处理。
    """
    flow = []
    input_data = None
    output_data = None
    errors = []

    for item in logs:
        try:
            step = item.get("step", "unknown")
            data = item.get("data")
            flow.append(step)

            if step == "request_start":
                input_data = data
            elif step == "response_ready":
                output_data = data
            elif step == "error":
                errors.append(data)
        except Exception:
            # 单条日志格式异常不应阻断整个上下文构建
            logger.warning(f"跳过格式异常日志: {item}", exc_info=True)
            flow.append("<malformed>")

    return {
        "request_id": request_id,
        "flow": flow,
        "input": input_data,
        "output": output_data,
        "errors": errors,
    }


def build_debug_context(trace_id: str | None = None, include_runtime: bool = True) -> dict | None:
    """一次性组装完整调试上下文（M8）：异常帧 + 源码片段 + git 归因 + 网络链 + UI 事件 + 运行时。

    复用 trace_repo(M2) / code_locator / git(M5) / runtime。
    返回 dict（兼容 analyzer 的 exception/runtime 字段），无 trace 时返回 None。
    各子采集失败降级，不阻断整体构建。
    """
    from app.mcp.core import trace_repo
    from app.mcp.collectors.code_locator import get_snippets_for_frames
    from app.mcp.core.git import get_blame_for_frame, get_recent_diff
    from app.mcp.collectors.runtime import collect_runtime_snapshot
    from app.mcp.collectors.spec import get_related_specs

    trace = trace_repo.get_trace(trace_id)
    if trace is None:
        # fallback: 数据可能通过 add_log 直接写入存储（非 errors 缓冲），
        # 从 TraceStorage 构造最小 trace 对象
        from app.mcp.core.logs import get_logs
        entries = get_logs(trace_id) if trace_id else []
        if not entries:
            return None
        # 从 entries 提取最早时间戳和摘要
        first_ts = min(e.get("timestamp", 0) for e in entries)
        last_ts = max(e.get("timestamp", 0) for e in entries)
        message = ""
        exc_type = "unknown"
        trace_kind = "debug"
        extra = {}
        for e in entries:
            data = e.get("data")
            if not isinstance(data, dict):
                continue
            step = e.get("step", "")
            if step == "error":
                exc_type = data.get("error_type", data.get("type", "unknown"))
                message = data.get("message", "")
                break
            elif step == "trace_meta":
                trace_kind = data.get("trace_kind", "exception")
                extra = data.get("extra", {})
                # 从 trace_meta 提取 exc_type/message（如果 extra 中有）
                if not message and extra.get("message"):
                    message = extra["message"]
        trace = {
            "trace_id": trace_id,
            "timestamp": last_ts,
            "exc_type": exc_type,
            "message": message,
            "frames": [],
            "frame_count": 0,
            "source": "storage",
            "fingerprint": None,
            "occurrence_count": 1,
            "first_seen": first_ts,
            "last_seen": last_ts,
            "trace_kind": trace_kind,
            "extra": extra,
        }

    tid = trace["trace_id"]
    frames = trace.get("frames", []) or []

    # 源码片段
    code_snippets: list = []
    if frames:
        try:
            code_snippets = [s.model_dump() for s in get_snippets_for_frames(frames)]
        except Exception:
            logger.warning("code_snippets 构建失败 (trace_id=%s)", tid)

    # git 归因（前 3 帧）
    git_blame: list = []
    recent_diffs: list = []
    for f in frames[:3]:
        try:
            blame = get_blame_for_frame(f.get("file", ""), f.get("line", 0))
            if blame:
                git_blame.append(blame)
        except Exception:
            pass
        try:
            diff = get_recent_diff(f.get("file", ""), commits_back=3)
            if diff:
                recent_diffs.append(diff)
        except Exception:
            pass

    # 网络链 / UI 事件（仅当存在）
    try:
        network_trace = trace_repo.get_network_records(tid) or None
    except Exception:
        network_trace = None
    try:
        ui_events = trace_repo.get_ui_events(tid) or None
    except Exception:
        ui_events = None

    # verify 断言结果（spec_diffs，V5 闭环）
    try:
        from app.mcp.core.logs import get_logs
        spec_diffs = [e["data"] for e in get_logs(tid) if e.get("step") == "verify"] or None
    except Exception:
        spec_diffs = None

    # 相关规范片段（前 3 帧所在文件，按规范文件去重，限总长 ~6000 字符）
    related_specs: list = []
    _seen_spec_files: set = set()
    _total = 0
    for f in frames[:3]:
        try:
            for s in get_related_specs(f.get("file", "")):
                if s["file"] in _seen_spec_files:
                    continue
                _seen_spec_files.add(s["file"])
                if _total + len(s["content"]) > 6000:
                    remaining = 6000 - _total
                    if remaining > 100:
                        related_specs.append({**s, "content": s["content"][:remaining] + "\n...（已截断）"})
                    break
                related_specs.append(s)
                _total += len(s["content"])
        except Exception:
            pass
        if _total >= 6000:
            break

    runtime = None
    if include_runtime:
        try:
            runtime = collect_runtime_snapshot()
        except Exception:
            runtime = None

    exc_type = trace.get("exc_type")
    message = trace.get("message")

    return {
        "request_id": tid,
        "trace_id": tid,
        "trace_kind": trace.get("trace_kind", "exception"),
        "flow": ["error"],
        "input": None,
        "output": None,
        "errors": [{"type": exc_type, "message": message}],
        "exception": {
            "type": exc_type,
            "message": message,
            "frames": frames,
            "frame_count": trace.get("frame_count", len(frames)),
        },
        "source": trace.get("source"),
        "extra": trace.get("extra", {}),
        "code_snippets": code_snippets,
        "git_blame": git_blame or None,
        "recent_diffs": recent_diffs or None,
        "related_specs": related_specs or None,
        "network_trace": network_trace,
        "ui_events": ui_events,
        "spec_diffs": spec_diffs,
        "runtime": runtime,
    }
