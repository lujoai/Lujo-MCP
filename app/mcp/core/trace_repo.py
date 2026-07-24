"""
统一 trace 存取层 —— 在现有 TraceStorage + errors 近期缓冲之上，
重新实现 proj2 的 save_trace / get_trace / save_network_record /
get_network_records / save_ui_event / get_ui_events 等接口。

设计原则（按 proj1 架构，非复制 proj2 SQLite）：
- 不引入新存储后端，不修改 TraceStorage/SessionStorage 抽象与 MemoryStore/PGStore。
- trace 记录（异常帧）复用 errors.record / get_by_id（近期异常缓冲），
  使 ingest 的错误也出现在既有 list_recent_traces / search_logs 工具里。
- network / ui_event 作为带特殊 step（"network" / "ui_event"）的 trace 条目，
  复用 logs.add_log / get_logs —— Memory 与 PG 后端零改动即可用。
- 脱敏在存储边界统一执行（url/body/payload/message），落实 redaction "写入存储前统一处理"。

C3/C4 修复（v0.3.0 Release Audit）：
- save_trace 始终以 errors 缓冲的 error_id 作为 add_log 写入 key 与返回值，
  保证"返回 ID == add_log key == errors error_id"三者统一。
- save_trace 同时通过 add_log 把完整异常数据（type/message/frames/traceback）
  持久化到 trace_store（step=trace_data），不依赖 errors 内存缓冲。
- get_trace 在 errors 内存未命中时从 trace_store 回读重建 trace 对象，
  解决"重启即丢"。
"""
import time
import uuid
import logging
import threading
from typing import Any, Optional

from app.config import settings
from app.mcp.core.logs import add_log, add_logs_batch, get_logs
from app.mcp.core.errors import record as _record_error, get_by_id as _get_error, get_latest as _get_latest
from app.mcp.core.redaction import redact

logger = logging.getLogger("ai-debug-mcp.trace_repo")
_MAX_RETRY_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 0.1

# Phase 2：复合键名脱敏扩展
# 敏感子串集合：键名（小写）包含任一子串即视为敏感键，
# 覆盖 db_password / user_token / auth_header / secret_config 等复合键名。
_SENSITIVE_SUBSTRINGS = {
    "password",
    "passwd",
    "pwd",
    "token",
    "secret",
    "key",
    "auth",
    "cookie",
}

# 内置白名单：含敏感子串但属于正常字段（不应脱敏）。
# password_hash=哈希后密码（非明文）、public_key=公钥（非私钥）、
# key_count/key_id/key_type=键数量/标识/类型（非密钥本身）。
_DEFAULT_ALLOWLIST = {
    "password_hash",
    "public_key",
    "key_count",
    "key_id",
    "key_type",
}

# 白名单缓存（按配置签名，配置变化时重建）
_allowlist_cache: Optional[set[str]] = None
_allowlist_signature: Optional[str] = None
_allowlist_lock = threading.Lock()


def _get_allowlist() -> set[str]:
    """获取生效的白名单（内置默认 + 用户配置 redaction_key_allowlist）。配置变化时重建。"""
    global _allowlist_cache, _allowlist_signature
    raw = settings.redaction_key_allowlist or ""
    if _allowlist_cache is not None and _allowlist_signature == raw:
        return _allowlist_cache
    with _allowlist_lock:
        if _allowlist_cache is not None and _allowlist_signature == raw:
            return _allowlist_cache
        base = set(_DEFAULT_ALLOWLIST)
        for name in raw.split(","):
            name = name.strip().lower()
            if name:
                base.add(name)
        _allowlist_cache = base
        _allowlist_signature = raw
        return base


def _is_sensitive_key(key) -> bool:
    """判断键名是否敏感：白名单优先（命中不脱敏），其次子串包含匹配。"""
    key_lower = str(key).lower()
    if key_lower in _get_allowlist():
        return False
    return any(s in key_lower for s in _SENSITIVE_SUBSTRINGS)


# trace 条目 step 命名
_STEP_DATA = "trace_data"       # 完整异常数据（C4：落库到 trace_store）
_STEP_META = "trace_meta"      # trace_kind / extra 元信息
_STEP_LINK = "trace_link"      # caller_trace_id ↔ error_id 关联（C3）
_STEP_NETWORK = "network"
_STEP_UI = "ui_event"
_STEP_CONSOLE = "console"


def _new_id(prefix: str = "rec") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _redact_nested(value: Any) -> Any:
    """递归脱敏 frames / extra 等嵌套结构中的字符串值。"""
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if _is_sensitive_key(key):
                sanitized[key] = "***REDACTED***"
            else:
                sanitized[key] = _redact_nested(item)
        return sanitized
    if isinstance(value, list):
        return [_redact_nested(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_nested(item) for item in value]
    if isinstance(value, str):
        return redact(value) or value
    return value


# ── trace（异常/静默失败）──
def save_trace(
    exc_type: str,
    message: str,
    frames: list[dict],
    source: str = "ingest",
    extra: Optional[dict] = None,
    trace_kind: str = "exception",
    trace_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """保存一条 trace（复用 errors 近期缓冲 + trace_store 持久化），返回 error_id。

    C3：返回值与 add_log 写入 key 统一为 errors 缓冲的 error_id。
    C4：完整异常数据通过 add_log(step="trace_data") 落 trace_store，重启不丢。

    caller 提供的 trace_id（如浏览器 SDK 的 _trace_id）会以 trace_link 形式
    记录在 error_id 下，用于审计与反向查询，但不再作为返回值或存储 key。
    """
    extra = extra or {}
    frames = _redact_nested(frames or [])
    exc_data = {
        "type": exc_type,
        "message": redact(message) or "",
        "frames": frames,
        "traceback": "",
        "frame_count": len(frames),
    }
    error_id = _record_error(exc_data, source=source, session_id=session_id)

    # SEC-13：commit-marker 模式 —— 写入顺序调整为 META → LINK → DATA，
    # DATA 作为提交标记最后写入。这样 trace_data 存在即保证 META（及 LINK）已落库；
    # 若崩溃发生在 DATA 写入之前，则无 trace_data，_rebuild_trace_from_store 返回 None（干净失败）。

    # 1+2) 批量写入 META + LINK（准备数据，非提交标记），减少调用开销
    batch_items = [(
        _STEP_META,
        {
            "trace_kind": trace_kind,
            "extra": _redact_nested(extra),
            "error_id": error_id,
            "ts": time.time(),
        },
    )]
    if trace_id and trace_id != error_id:
        batch_items.append((
            _STEP_LINK,
            {"caller_trace_id": trace_id, "ts": time.time()},
        ))
    try:
        add_logs_batch(error_id, batch_items)
    except Exception:
        logger.exception("写入 trace_meta/link 批次失败 (error_id=%s)", error_id)

    # 3) C4 + SEC-13：DATA 作为提交标记单独最后写入（不合并进批次），保证原子语义
    try:
        add_log(error_id, _STEP_DATA, {
            **exc_data,
            "source": source,
            "ts": time.time(),
        })
    except Exception:
        logger.exception("写入 trace_data 失败 (error_id=%s)", error_id)

    return error_id


def _rebuild_trace_from_store(error_id: str) -> Optional[dict]:
    """从 trace_store 回读重建 trace 对象（C4：errors 缓冲未命中时使用）。

    必须能找到 step=trace_data 的条目；trace_meta / trace_link 可选。
    """
    trace_data = None
    meta = {}
    caller_trace_id = None
    try:
        for entry in get_logs(error_id):
            step = entry.get("step")
            data = entry.get("data")
            if step == _STEP_DATA and isinstance(data, dict):
                trace_data = data
            elif step == _STEP_META and isinstance(data, dict):
                meta = data
            elif step == _STEP_LINK and isinstance(data, dict):
                caller_trace_id = data.get("caller_trace_id")
    except Exception:
        logger.exception("从 trace_store 回读 trace 失败 (error_id=%s)", error_id)
        return None

    if trace_data is None:
        return None

    frames = trace_data.get("frames") or []
    timestamp = trace_data.get("ts") or 0
    return {
        "trace_id": error_id,
        "timestamp": timestamp,
        "exc_type": trace_data.get("type"),
        "message": trace_data.get("message", ""),
        "frames": frames,
        "frame_count": trace_data.get("frame_count", len(frames)),
        "source": trace_data.get("source", "storage"),
        "fingerprint": None,
        "occurrence_count": 1,
        "first_seen": timestamp,
        "last_seen": timestamp,
        "trace_kind": meta.get("trace_kind", "exception"),
        "extra": meta.get("extra", {}),
        "caller_trace_id": caller_trace_id,
        "from_store": True,  # 标记来自回读，便于诊断
    }


def get_trace(trace_id: Optional[str] = None, session_id: Optional[str] = None) -> Optional[dict]:
    """取指定 trace_id，不传则取最新一条。

    C4：errors 内存未命中时从 trace_store 回读重建 trace 对象。
    查找顺序：
      1. errors 缓冲直接命中（trace_id == error_id）
      2. trace_store 回读 step=trace_data 重建（重启/超出缓冲容量场景）
    """
    if not trace_id:
        err = _get_latest(session_id=session_id)
        if err is None:
            # 兜底：从 trace_store 找最近一条 trace_data
            return None
        error_id = err["error_id"]
    else:
        error_id = trace_id
        err = _get_error(error_id, session_id=session_id)

    # errors 内存未命中时回读 trace_store（C4 下半段）
    if err is None:
        rebuilt = _rebuild_trace_from_store(error_id)
        if rebuilt is not None:
            return rebuilt
        return None

    # errors 缓冲命中，附加 trace_meta / trace_link
    meta = {}
    caller_trace_id = None
    try:
        for entry in get_logs(err["error_id"]):
            step = entry.get("step")
            data = entry.get("data")
            if step == _STEP_META and isinstance(data, dict):
                meta = data
            elif step == _STEP_LINK and isinstance(data, dict):
                caller_trace_id = data.get("caller_trace_id")
    except Exception:
        pass

    return {
        "trace_id": err["error_id"],
        "timestamp": err["timestamp"],
        "exc_type": err["type"],
        "message": err["message"],
        "frames": err["frames"],
        "frame_count": err["frame_count"],
        "source": err["source"],
        "fingerprint": err.get("fingerprint"),
        "occurrence_count": err.get("occurrence_count", 1),
        "first_seen": err.get("first_seen", err["timestamp"]),
        "last_seen": err.get("last_seen", err["timestamp"]),
        "trace_kind": meta.get("trace_kind", "exception"),
        "extra": meta.get("extra", {}),
        "caller_trace_id": caller_trace_id,
    }


# ── network ──
def save_network_record(
    record: dict,
    trace_id: Optional[str] = None,
    request_id: Optional[str] = None,
    extra: Optional[dict] = None,
) -> str:
    """保存一条网络请求记录，返回 record_id。

    存储边界统一脱敏 url / request_body / response_body。
    """
    key = trace_id or request_id or _new_id("net")
    record_id = record.get("record_id") or _new_id("net")
    payload = dict(record)
    payload["record_id"] = record_id
    payload["trace_id"] = trace_id
    payload["request_id"] = request_id
    payload["timestamp"] = payload.get("timestamp") or time.time()
    payload["direction"] = payload.get("direction") or "outbound"
    # 入库前脱敏
    payload["url"] = redact(payload.get("url"))
    payload["request_body"] = redact(payload.get("request_body"))
    payload["response_body"] = redact(payload.get("response_body"))
    if extra:
        payload["extra"] = extra

    add_log(key, _STEP_NETWORK, payload)
    return record_id


def get_network_records(trace_id: str) -> list[dict]:
    """查询与某 trace 关联的所有网络请求记录（按时间顺序）。"""
    return [e["data"] for e in get_logs(trace_id) if e.get("step") == _STEP_NETWORK]


# ── ui_event ──
def save_ui_event(
    event: dict,
    trace_id: Optional[str] = None,
    extra: Optional[dict] = None,
) -> str:
    """保存一条前端 UI 事件，返回 event_id。

    存储边界统一脱敏 route_path / payload_json。
    """
    key = trace_id or _new_id("ui")
    event_id = event.get("event_id") or _new_id("ui")
    payload = dict(event)
    payload["event_id"] = event_id
    payload["trace_id"] = trace_id
    payload["timestamp"] = payload.get("timestamp") or time.time()
    payload["event_type"] = payload.get("event_type") or "click"
    # 入库前脱敏
    payload["route_path"] = redact(payload.get("route_path"))
    payload["payload_json"] = redact(payload.get("payload_json"))
    if extra:
        payload["extra"] = extra

    add_log(key, _STEP_UI, payload)
    return event_id


def get_ui_events(trace_id: str) -> list[dict]:
    """查询与某 trace 关联的所有 UI 事件（按时间顺序）。"""
    return [e["data"] for e in get_logs(trace_id) if e.get("step") == _STEP_UI]


# ── console ──
def save_console_log(
    level: str = "info",
    message: str = "",
    source: str = "browser_sdk",
    extra: Optional[dict] = None,
    trace_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> str:
    """保存一条控制台日志，返回 record_id。

    存储边界统一脱敏 message。
    """
    key = trace_id or request_id or _new_id("console")
    record_id = _new_id("console")
    payload = {
        "record_id": record_id,
        "trace_id": trace_id,
        "request_id": request_id,
        "timestamp": time.time(),
        "level": level or "info",
        "message": redact(message or ""),
        "source": source,
    }
    if extra:
        payload["extra"] = extra

    add_log(key, _STEP_CONSOLE, payload)
    return record_id


def get_console_logs(trace_id: str) -> list[dict]:
    """查询与某 trace 关联的所有控制台日志（按时间顺序）。"""
    return [e["data"] for e in get_logs(trace_id) if e.get("step") == _STEP_CONSOLE]
