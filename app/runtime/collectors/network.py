"""
网络请求记录采集器 —— 解析外部上报的原始 dict，截断 body，规范化字段。

职责：解析 / 截断 / 校验。脱敏在 trace_repo 存储边界统一执行，本模块不重复脱敏。
按 proj1 架构重写（非复制 proj2）：dict in / dict out，与 trace_repo 保持一致。
"""
import time
import logging

logger = logging.getLogger("lujo-mcp.collectors.network")

_MAX_BODY_CHARS = 10 * 1024  # 10KB，与浏览器 SDK 截断阈值一致


def _truncate_body(text):
    """超长 body 截断，None/空原样返回。"""
    if not text:
        return text
    if len(text) > _MAX_BODY_CHARS:
        return text[:_MAX_BODY_CHARS] + "\n...（已截断）"
    return text


def parse_network_record(raw: dict) -> dict:
    """把原始 dict 规范化为 network 记录。非法输入抛 ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError("network record 必须是对象")

    status_code = raw.get("status_code")
    duration_ms = raw.get("duration_ms")

    return {
        "record_id": raw.get("record_id"),
        "timestamp": raw.get("timestamp") or time.time(),
        "direction": raw.get("direction") or "outbound",
        "method": (raw.get("method") or "GET").upper(),
        "url": raw.get("url"),
        "status_code": int(status_code) if status_code is not None else None,
        "request_body": _truncate_body(raw.get("request_body")),
        "response_body": _truncate_body(raw.get("response_body")),
        "duration_ms": float(duration_ms) if duration_ms is not None else None,
    }


def parse_network_records(raw_list) -> list[dict]:
    """批量解析，单条异常跳过不阻断整体。"""
    records: list[dict] = []
    for raw in raw_list or []:
        try:
            records.append(parse_network_record(raw))
        except Exception:
            logger.warning("跳过格式异常的 network record: %r", raw)
            continue
    return records
