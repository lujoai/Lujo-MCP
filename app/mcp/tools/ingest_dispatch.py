"""ingest 事件分发的 tools 层共用模块 + heavy 结果排水入库（v1.0.x 修复）。

背景：``/ingest/*`` 的批量分发逻辑（``_dispatch_single``）原先只活在
``app/api/ingest.py``（HTTP 层）。auto_test 修复工作包需要主进程在收到 heavy
子进程结果后，把子进程截获的 SDK 上报事件经**同一套**校验/脱敏/关联/入库落地
（HTTP /ingest 与 MCP 结果排水不得是两套契约），而 protocol 层直接 import
api 层会违反既有依赖方向（api → tools 单向）。因此把纯函数分发逻辑下沉到
tools 层本模块，``app/api/ingest.py`` 改为从这里导入，行为不变。

``drain_result_ingest_events`` 是主进程侧排水入口：
- heavy 工具（auto_test）在结果 dict 中携带内部保留键 ``_lujo_ingest``
  （``{"path": "/ingest/console", "payload": {...}}`` 列表）；
- 本函数弹出该键并逐条经 ``dispatch_single`` 入库（与 /ingest/batch 端点
  完全同一处理路径），把 ``events_ingested`` / ``ingest_failures`` 计数写回
  ``result["sdk_capture"]``；
- 内部键弹出后不会泄漏进 MCP 响应；无键的结果为 no-op。
"""
import json
import logging

logger = logging.getLogger("lujo-mcp.ingest_dispatch")

# heavy 结果内部保留键：auto_test 等采集型 heavy 工具向主进程回传待入库事件。
# 该键在主进程排水时弹出，属进程间内部契约，不属于任何公共响应字段。
RESULT_INGEST_KEY = "_lujo_ingest"

# 排水单次入库条数上限：与 SDK 批量上报的客户端分片上限同量级，防止
# 异常页面洪水把主进程入库与 MCP 响应撑爆。截断时以真实计数如实上报。
_MAX_DRAIN_EVENTS = 200

# 排水单次入库字节上限（UTF-8 编码后的 JSON 表示逐条计量）：条数上限之外
# 再加一道字节闸门——单条事件可携带长堆栈，200 条可能远超 heavy IPC 帧与
# 主进程内存的合理水位。取值与子进程侧 _MAX_CAPTURE_TOTAL_BYTES 同值
# （8 MiB），超限即停止入库并以 sdk_capture.events_ingest_truncated 如实标记。
_MAX_DRAIN_BYTES = 8 * 1024 * 1024


def extract_session_id(payload: dict) -> str | None:
    """统一上报 envelope 的 session_id 提取（自 app/api/ingest.py 下沉，行为不变）。

    规范位置是顶层 ``session_id``；兼容旧版浏览器 SDK 把会话放在
    ``extra.session_id`` 的上报格式。两者同时存在时顶层优先（显式入参
    可信度更高）。普通 SDK 数据此前因此进入 _global 桶，会话查询无结果、
    相同错误跨页面被错误合并。
    """
    if not isinstance(payload, dict):
        return None
    sid = payload.get("session_id")
    if sid:
        return str(sid)
    extra = payload.get("extra")
    if isinstance(extra, dict):
        sid = extra.get("session_id")
        if sid:
            return str(sid)
    return None


# ingest 分发路径的单一事实源：dispatch_single 的支持集必须与此集合一致
# （tests/unit/test_auto_test_ingest_chain.py 一致性锁定）。auto_test 采集
# 拦截白名单从这里派生，避免两处白名单长期漂移。
DISPATCH_INGEST_PATHS = frozenset({
    "/ingest/error",
    "/ingest/network",
    "/ingest/ui-event",
    "/ingest/console",
    "/ingest/silent-failure",
})


def dispatch_single(path: str, payload: dict) -> dict:
    """将单条批量事件分发到对应的 ingest 处理器（自 app/api/ingest.py 下沉）。

    path 为 SDK 原始上报路径（如 /ingest/error），payload 为该路径对应的完整请求体。
    各路径的参数提取逻辑与独立 ingest 端点保持一致。
    """
    if path == "/ingest/error":
        from app.mcp.tools.ingest_api import tool_ingest_error

        return tool_ingest_error(
            exc_type=payload.get("exc_type", "UnknownError"),
            message=payload.get("message", ""),
            frames=payload.get("frames", []),
            source=payload.get("source", "http_ingest"),
            extra=payload.get("extra"),
            trace_id=payload.get("trace_id"),
            session_id=extract_session_id(payload),
        )
    if path == "/ingest/network":
        from app.mcp.tools.network_api import tool_ingest_network

        return tool_ingest_network(
            record=payload.get("record", {}),
            trace_id=payload.get("trace_id"),
            request_id=payload.get("request_id"),
            session_id=extract_session_id(payload),
        )
    if path == "/ingest/ui-event":
        from app.runtime.core.trace_repo import save_ui_event

        trace_id = payload.get("trace_id")
        event_id = save_ui_event(
            event=payload.get("event", {}) or {},
            trace_id=trace_id,
            extra=payload.get("extra"),
            session_id=extract_session_id(payload),
        )
        return {"event_id": event_id, "trace_id": trace_id, "saved": True}
    if path == "/ingest/console":
        from app.mcp.tools.console_api import tool_ingest_console

        return tool_ingest_console(
            level=payload.get("level", "info"),
            message=payload.get("message", ""),
            source=payload.get("source", "browser_sdk"),
            extra=payload.get("extra"),
            trace_id=payload.get("trace_id"),
            request_id=payload.get("request_id"),
            session_id=extract_session_id(payload),
        )
    if path == "/ingest/silent-failure":
        from app.mcp.tools.silent_failure_api import tool_ingest_silent_failure

        return tool_ingest_silent_failure(
            message=payload.get("message", ""),
            frames=payload.get("frames"),
            ui_events=payload.get("ui_events"),
            network_records=payload.get("network_records"),
            expectation=payload.get("expectation"),
            observed=payload.get("observed"),
            observed_events=payload.get("observed_events"),
            source=payload.get("source", "browser_sdk"),
            extra=payload.get("extra"),
            trace_id=payload.get("trace_id"),
            session_id=extract_session_id(payload),
        )
    raise ValueError(f"Unknown ingest path: {path}")


def drain_result_ingest_events(result: dict) -> None:
    """主进程侧：弹出 heavy 结果中的 ``_lujo_ingest`` 事件并逐条入库。

    - 只接受 dict 结果；无内部键时为 no-op（对既有工具零影响）；
    - 逐条容错：单条失败（未知 path / 载荷非法）不中断其余事件，
      以 ``ingest_failures`` 计数如实暴露；
    - 成功计数写入 ``result["sdk_capture"]["events_ingested"]``
      （无 sdk_capture 字段的结果仅计数入日志，不凭空造字段）；
    - 条数（``_MAX_DRAIN_EVENTS``）与 UTF-8 字节（``_MAX_DRAIN_BYTES``）
      双上限：任一超限即停止入库，并在 sdk_capture 写真实标记
      ``events_ingest_truncated=True``（被丢弃的事件确实没有入库，
      不得把丢弃报告成 complete）。
    """
    if not isinstance(result, dict):
        return
    events = result.pop(RESULT_INGEST_KEY, None)
    if not events:
        return
    if not isinstance(events, list):
        logger.warning("heavy 结果 %s 键类型异常（%s），忽略排水", RESULT_INGEST_KEY, type(events).__name__)
        return

    ok = 0
    failed = 0
    used_bytes = 0
    byte_limited = False
    for event in events[:_MAX_DRAIN_EVENTS]:
        if not isinstance(event, dict):
            failed += 1
            continue
        # UTF-8 字节计量：与子进程侧采集限额同一口径，逐条诚实累加
        try:
            size = len(json.dumps(event, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            size = 0
        if used_bytes + size > _MAX_DRAIN_BYTES:
            byte_limited = True
            logger.warning(
                "heavy 结果事件累计字节 %d 超过排水上限 %d，停止入库",
                used_bytes + size, _MAX_DRAIN_BYTES,
            )
            break
        used_bytes += size
        try:
            dispatch_single(str(event.get("path") or ""), event.get("payload") or {})
            ok += 1
        except Exception:
            failed += 1
            logger.exception(
                "heavy 采集事件入库失败 path=%s", str(event.get("path") or "")[:64]
            )
    truncated = byte_limited or len(events) > _MAX_DRAIN_EVENTS
    if len(events) > _MAX_DRAIN_EVENTS:
        logger.warning(
            "heavy 结果事件数 %d 超过排水上限 %d，已截断入库",
            len(events), _MAX_DRAIN_EVENTS,
        )

    status = result.get("sdk_capture")
    if isinstance(status, dict):
        status["events_ingested"] = ok
        if truncated:
            # 如实标记：被丢弃的事件没有入库，不得把丢弃报告成 complete
            status["events_ingest_truncated"] = True
        if failed:
            status["ingest_failures"] = failed
    else:
        logger.info("heavy 采集事件排水完成：ingested=%d failed=%d", ok, failed)
