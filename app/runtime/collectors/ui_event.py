"""
前端 UI 事件采集器 —— 解析浏览器 SDK 上报的原始 dict，规范化字段。

职责：解析 / 截断 / 校验。脱敏在 trace_repo 存储边界统一执行（route_path / payload_json）。
按 proj1 架构重写（非复制 proj2）：dict in / dict out，与 trace_repo 保持一致。
"""
import time
import logging

logger = logging.getLogger("lujo-mcp.collectors.ui_event")

_MAX_PAYLOAD_CHARS = 10 * 1024  # 10KB


def _truncate_payload(text):
    """超长 payload 截断，None/空原样返回。"""
    if not text:
        return text
    if len(text) > _MAX_PAYLOAD_CHARS:
        return text[:_MAX_PAYLOAD_CHARS] + "\n...（已截断）"
    return text


def parse_ui_event(raw: dict) -> dict:
    """把原始 dict 规范化为 UI 事件。非法输入抛 ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError("ui event 必须是对象")

    return {
        "event_id": raw.get("event_id"),
        "timestamp": raw.get("timestamp") or time.time(),
        "event_type": raw.get("event_type") or "click",
        "target_selector": raw.get("target_selector"),
        "component_name": raw.get("component_name"),
        "route_path": raw.get("route_path"),
        "payload_json": _truncate_payload(raw.get("payload_json")),
    }


def parse_ui_events(raw_list) -> list[dict]:
    """批量解析，单条异常跳过不阻断整体。"""
    events: list[dict] = []
    for raw in raw_list or []:
        try:
            events.append(parse_ui_event(raw))
        except Exception:
            logger.warning("跳过格式异常的 ui event: %r", raw)
            continue
    return events
