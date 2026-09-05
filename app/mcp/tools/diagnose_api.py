"""MCP 工具：diagnose_issue —— 统一诊断入口（Agent-facing 只读）。

解决的问题：宿主 AI 在新会话中没有 request_id/trace_id，也不知道该先调哪个
工具；而此前的查询类工具（context/trace/get_network_trace）全部要求 ID，
导致 AI 首次调用即死路、之后不再尝试。

本工具把「找错误」收敛为单一入口，优先复用既有能力、不复制存储逻辑：
- errors.get_by_id / get_latest（近期错误缓冲）
- trace_api.search_logs / list_recent_traces（内存 + 存储摘要合并检索）
- build_debug_context（完整调试上下文组装：堆栈/源码片段/git/网络/UI/运行时）

返回结构稳定：found=true 时含 trace_id/summary/debug_context/source；
found=false 时含 message/setup_hint/next_step（绝不返回空对象让 AI 猜）。
"""
import logging

from app.runtime.core import errors
from app.runtime.context.builder import build_debug_context

logger = logging.getLogger("lujo-mcp.tools.diagnose")

DIAGNOSE_DEF = {
    "name": "diagnose_issue",
    "description": (
        "【统一诊断入口，遇到运行问题优先调用】当用户报告运行时问题——"
        "如「刚才页面报错了」「接口返回 500」「点击按钮没有反应」「测试失败了」"
        "「控制台有异常」「登录失败」——应首先调用本工具。"
        "没有 request_id / trace_id 也必须调用：本工具会自动查找最近一次真实错误，"
        "并一次性返回完整调试上下文（异常堆栈+源码片段+网络请求链+UI 事件+git 归因）。"
        "三种用法：①不带参数=取最近一次错误；②query=按关键词匹配近期错误"
        "（如「登录失败」「500」）；③request_id=精确查询指定记录。"
        "拿到 trace_id 后如需更细粒度信息，再按需调用 context / get_network_trace / "
        "get_recent_diff / get_blame_for_frame。"
        "纯代码解释、架构讨论、与运行时现场无关的问题不要调用本工具。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "request_id": {
                "type": "string",
                "description": "错误或请求 ID（可选；提供时精确查询该记录）",
            },
            "query": {
                "type": "string",
                "description": "关键词（可选），如「登录失败」「500」，在近期错误中匹配",
            },
            "since_minutes": {
                "type": "integer",
                "description": "查询时间范围（分钟），默认 30（仅 query 模式生效）",
                "default": 30,
            },
            "session_id": {
                "type": "string",
                "description": "会话 ID（可选，用于会话隔离查询）",
            },
        },
        "required": [],
    },
}


def _summarize_error(err: dict | None) -> dict:
    """把错误记录归一为稳定摘要结构。"""
    if not err:
        return {}
    frames = err.get("frames") or []
    top = frames[0] if isinstance(frames, list) and frames else None
    top_frame = None
    if isinstance(top, dict):
        top_frame = f"{top.get('file', '?')}:{top.get('line', 0)} in {top.get('function', '?')}"
    return {
        "error_id": err.get("error_id") or err.get("trace_id"),
        "type": err.get("type"),
        "message": err.get("message"),
        "last_seen": err.get("last_seen") or err.get("timestamp"),
        "occurrence_count": err.get("occurrence_count", 1),
        "top_frame": top_frame,
        "source": err.get("source"),
    }


def _not_found(message: str, next_step: str | None = None) -> dict:
    """无数据时的稳定引导结构——AI 据此向用户解释并采取下一步。"""
    return {
        "found": False,
        "message": message,
        "setup_hint": (
            "错误数据来源：浏览器端需在页面接入 Browser SDK 并上报到本服务的 "
            "HTTP /ingest 端点；后端异常由全局异常钩子或 ingest_error 自动捕获。"
            "stdio 纯 MCP 接入不接收浏览器 HTTP 上报。"
        ),
        "next_step": next_step or (
            "可直接再次调用本工具（不带参数）获取最近一次错误；"
            "或确认页面已接入 SDK 且服务以 HTTP 模式运行后重试。"
        ),
    }


def _build_context(trace_id: str) -> dict | None:
    """构建调试上下文，失败降级为 None（不阻断摘要返回）。"""
    try:
        ctx = build_debug_context(trace_id)
        return ctx.model_dump() if ctx is not None else None
    except Exception:
        logger.exception("build_debug_context 失败 (trace_id=%s)，降级为仅摘要", trace_id)
        return None


def _finish(trace_id: str, err: dict | None, source: str) -> dict:
    """汇总单条命中结果：摘要 + 完整上下文。"""
    if err is None:
        err = errors.get_by_id(trace_id)
    ctx = _build_context(trace_id)
    if err is None and not ctx:
        return _not_found(
            f"记录 {trace_id} 存在摘要但无法构建调试上下文",
            next_step="可尝试调用 trace 工具查看该 ID 的原始追踪日志。",
        )
    return {
        "found": True,
        "trace_id": trace_id,
        "summary": _summarize_error(err),
        "debug_context": ctx or {},
        "source": source,
    }


def handler(arguments: dict) -> dict:
    """diagnose_issue 工具处理函数。"""
    arguments = arguments or {}
    request_id = arguments.get("request_id")
    query = arguments.get("query")
    session_id = arguments.get("session_id")
    try:
        since_minutes = int(arguments.get("since_minutes") or 30)
    except (TypeError, ValueError):
        since_minutes = 30

    # ① request_id 精确查询
    if request_id:
        err = errors.get_by_id(request_id, session_id=session_id)
        ctx = _build_context(request_id)
        if err is None and not ctx:
            return _not_found(
                f"未找到 {request_id} 对应的错误或追踪记录",
                next_step="可不带参数重新调用本工具，将自动返回最近一次错误；"
                          "或调用 list_recent_traces 浏览近期错误摘要。",
            )
        return {
            "found": True,
            "trace_id": request_id,
            "summary": _summarize_error(err),
            "debug_context": ctx or {},
            "source": "request_id",
        }

    # ② query 关键词匹配（复用 trace_api.search_logs：内存缓冲 + 存储摘要合并）
    if query:
        from app.mcp.tools.trace_api import search_logs as _search_logs

        matches = _search_logs(
            query, since_minutes=since_minutes, session_id=session_id
        )
        if not matches:
            return _not_found(
                f"最近 {since_minutes} 分钟内没有匹配「{query}」的错误",
                next_step="可尝试其他关键词、扩大 since_minutes，"
                          "或调用 list_recent_traces 查看全部近期错误。",
            )
        best = matches[0]
        return _finish(
            best.get("trace_id") or best.get("error_id") or "",
            err=None,
            source="query",
        )

    # ③ 默认：最近一次真实错误（errors 缓冲优先，回退存储摘要）
    latest = errors.get_latest(session_id=session_id)
    if latest:
        return _finish(
            latest.get("error_id") or latest.get("trace_id") or "",
            err=latest,
            source="latest",
        )

    from app.mcp.tools.trace_api import list_recent_traces as _list_recent

    recent = _list_recent(limit=1, session_id=session_id)
    if recent:
        return _finish(recent[0].get("trace_id") or "", err=None, source="recent_traces")

    return _not_found(
        "当前服务没有捕获到任何错误或追踪记录（可能是刚启动或尚无数据上报）",
        next_step="若调试浏览器问题：确认页面已接入 Browser SDK 并以 HTTP 模式运行"
                  "本服务（stdio 模式不接收浏览器上报），复现问题后重新调用本工具。",
    )


def invoke(body) -> dict:
    return handler(getattr(body, "arguments", {}) or {})
