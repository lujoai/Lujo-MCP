"""调试上下文构建器 —— 安全地解析追踪日志"""

import logging

from app.config import settings
from app.runtime.core.errors import compute_fingerprint
from app.schemas import DebugContext
from app.runtime.context.fault_localizer import localize, to_payload

logger = logging.getLogger("lujo-mcp.context")


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


def build_debug_context(trace_id: str | None = None, include_runtime: bool = True) -> DebugContext | None:
    """一次性组装完整调试上下文（M8）：异常帧 + 源码片段 + git 归因 + 网络链 + UI 事件 + 运行时。

    复用 trace_repo(M2) / code_locator / git(M5) / runtime。
    返回 DebugContext（Pydantic model），无 trace 时返回 None。
    各子采集失败降级，不阻断整体构建。
    """
    from app.runtime.core import trace_repo
    from app.runtime.collectors.code_locator import get_snippets_for_frames
    from app.runtime.core.git import get_blame_for_frame, get_recent_diff
    from app.runtime.collectors.runtime import collect_runtime_snapshot
    from app.runtime.collectors.spec import get_related_specs

    trace = trace_repo.get_trace(trace_id)
    if trace is None:
        # fallback: 数据可能通过 add_log 直接写入存储（非 errors 缓冲），
        # 从 TraceStorage 构造最小 trace 对象
        from app.runtime.core.logs import get_logs
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
        error_frames: list = []
        error_fingerprint: "str | None" = None
        flow: list = []
        input_data = None
        output_data = None
        for e in entries:
            data = e.get("data")
            step = e.get("step", "")
            flow.append(step)
            if step == "request_start":
                input_data = data
            elif step == "response_ready":
                output_data = data
            if not isinstance(data, dict):
                continue
            if step == "error":
                exc_type = data.get("error_type", data.get("type", "unknown"))
                message = data.get("message", "")
                # FIX: R7-Q1 —— error 条目携带的完整异常数据（含堆栈帧）必须
                # 进入合成 trace，否则 fallback 路径丢帧，下游源码片段/故障
                # 定位/评分维度全部空转
                error_frames = data.get("frames") or []
                error_fingerprint = data.get("fingerprint")
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
            "frames": error_frames,
            "frame_count": len(error_frames),
            "source": "storage",
            # FIX: R7-P1-2（断点①）—— fallback 合成 trace 不再显式 None：
            # error 条目自带指纹（capture_exception 产出）时透传，
            # 否则用与 errors.record 相同的 compute_fingerprint 现算
            "fingerprint": error_fingerprint or compute_fingerprint(exc_type, error_frames),
            "occurrence_count": 1,
            "first_seen": first_ts,
            "last_seen": last_ts,
            "trace_kind": trace_kind,
            "extra": extra,
            # R7-Q1：请求载荷/执行流程保留给下游（修复链路 prompt 与评分依赖）
            "flow": flow,
            "input": input_data,
            "output": output_data,
        }

    tid = trace["trace_id"]
    frames = trace.get("frames", []) or []

    # v0.5.1 Source Map 还原：前端 minified 帧优先还原为原始源码位置（默认关闭，失败静默）。
    # 还原命中后：code_snippets / fault_localization / git 归因 / 相关规范均改用还原帧。
    resolved_frames = None
    effective_frames = frames
    sm_snippets: list = []
    if frames and settings.sourcemap_enabled:
        try:
            from app.runtime.collectors.sourcemap_store import resolve_frames_auto

            artifact = (trace.get("extra") or {}).get("artifact")
            release = (trace.get("extra") or {}).get("release")
            new_frames, sm_snippets = resolve_frames_auto(frames, artifact=artifact, release=release)
            if any(f.get("resolved") for f in new_frames):
                resolved_frames = new_frames
                effective_frames = new_frames
        except Exception:
            logger.warning("source map 帧还原失败 (trace_id=%s)", tid)

    # 源码片段（还原命中时：已还原帧用还原产物 snippets，未还原帧仍走 code_locator）
    code_snippets: list = []
    if effective_frames:
        try:
            if resolved_frames is not None:
                rest = [f for f in resolved_frames if not f.get("resolved")]
                code_snippets = sm_snippets + [
                    s.model_dump() for s in get_snippets_for_frames(rest)
                ]
            else:
                code_snippets = [s.model_dump() for s in get_snippets_for_frames(frames)]
        except Exception:
            logger.warning("code_snippets 构建失败 (trace_id=%s)", tid)

    # 无堆栈静态分析（M3）：静默失败无异常堆栈时，基于网络请求反查 handler
    static_analysis = None
    if not frames:
        try:
            method, path = _extract_request_target(trace)
            if method and path:
                from app.runtime.collectors.static_analyzer import analyze_handler

                loc = analyze_handler(method, path)
                if loc is not None:
                    static_analysis = {
                        "file": loc.file,
                        "function": loc.function,
                        "params": loc.function_info.params if loc.function_info else [],
                        "complexity_hints": (
                            loc.function_info.complexity_hints if loc.function_info else []
                        ),
                        "suspicious_inputs": loc.suspicious_inputs,
                    }
        except Exception:
            logger.warning("static_analysis 构建失败 (trace_id=%s)", tid)

    # git 归因（前 3 帧）
    git_blame: list = []
    recent_diffs: list = []
    for f in effective_frames[:3]:
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
        from app.runtime.core.logs import get_logs
        spec_diffs = [e["data"] for e in get_logs(tid) if e.get("step") == "verify"] or None
    except Exception:
        spec_diffs = None

    # 相关规范片段（前 3 帧所在文件，按规范文件去重，限总长 ~6000 字符）
    related_specs: list = []
    _seen_spec_files: set = set()
    _total = 0
    for f in effective_frames[:3]:
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
    # FIX: R7-P1-2（断点①）—— 指纹上游（errors.record / capture_exception /
    # trace_repo 回读重算）已算好，此前在 context 构建侧被丢弃，导致
    # context_prep/_get_error_signal 取不到指纹 → KB 三级命中、向量 RAG、
    # 分析回写、verify 写回、经验召回整条学习闭环因"指纹为空"短路。
    # 同时注入 exception/errors[0] 与顶层（context_assembler 经验召回读顶层）。
    fingerprint = trace.get("fingerprint")

    # 故障定位候选（V1）：仅筛选"最值得优先检查的位置"，不声称绝对根因。
    # 失败降级为 None，不影响 Debug Context 其余部分。
    fault_localization = None
    if effective_frames:
        try:
            fault_localization = to_payload(localize(effective_frames, exc_type, message))
        except Exception:
            logger.warning("fault_localization 构建失败 (trace_id=%s)", tid)

    result = {
        "request_id": tid,
        "trace_id": tid,
        "trace_kind": trace.get("trace_kind", "exception"),
        "flow": trace.get("flow") or ["error"],
        "input": trace.get("input"),
        "output": trace.get("output"),
        "errors": [{"type": exc_type, "message": message, "fingerprint": fingerprint}],
        "exception": {
            "type": exc_type,
            "message": message,
            "frames": frames,
            "frame_count": trace.get("frame_count", len(frames)),
            "fingerprint": fingerprint,
        },
        "source": trace.get("source"),
        "extra": trace.get("extra", {}),
        "fingerprint": fingerprint,
        "code_snippets": code_snippets,
        "static_analysis": static_analysis,
        "git_blame": git_blame or None,
        "recent_diffs": recent_diffs or None,
        "related_specs": related_specs or None,
        "network_trace": network_trace,
        "ui_events": ui_events,
        "spec_diffs": spec_diffs,
        "runtime": runtime,
        "fault_localization": fault_localization,
        "resolved_frames": resolved_frames,
    }
    return DebugContext(**result)


def _extract_request_target(trace: dict) -> tuple[str | None, str | None]:
    """从静默失败 trace 中提取 (method, path)，供 handler 反查定位。

    优先从 network_records 解析（含 method + url），其次从 extra 透传字段兜底。
    无法解析时返回 (None, None)，静默降级。
    """
    try:
        from app.runtime.core import trace_repo

        records = trace_repo.get_network_records(trace.get("trace_id", "")) or []
        for rec in records:
            method = rec.get("method")
            url = rec.get("url") or rec.get("path")
            if method and url:
                path = _url_to_path(url)
                if method and path:
                    return method.upper(), path
    except Exception:
        pass
    # extra 兜底：浏览器 SDK 可能直接透传 method/path
    extra = trace.get("extra", {}) or {}
    method = extra.get("method") or (extra.get("request") or {}).get("method")
    path = extra.get("path") or (extra.get("request") or {}).get("path")
    if method and path:
        return str(method).upper(), str(path)
    return None, None


def _url_to_path(url: str) -> str | None:
    """从 URL 中提取 path 部分（去掉 scheme/host/query）。"""
    if not url:
        return None
    # 去掉 query/fragment
    path = url.split("?")[0].split("#")[0]
    # 去掉 scheme + host
    if "://" in path:
        path = path.split("://", 1)[1]
        # 去掉 host 部分
        idx = path.find("/")
        if idx == -1:
            return None
        path = path[idx:]
    if not path.startswith("/"):
        path = "/" + path
    return path
