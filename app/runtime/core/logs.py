"""追踪日志模块 —— 封装存储层的便捷 API"""

import json
import time
import uuid
from typing import Any

from app.runtime.core.redaction import redact_nested
from app.runtime.core.storage.factory import get_trace_store


def create_request_id() -> str:
    return str(uuid.uuid4())


def _coerce_structured_payload(data: Any) -> Any:
    """A2 契约：调用方透传的 JSON 字符串解析成结构化值后再入库。

    此前只有 PG 后端"碰巧"满足该契约——``data JSONB`` 列读回来自动是 dict；
    memory 后端把脱敏后的字符串原样存取，同一条用例在两种后端上结果不同
    （test_redaction_integration 的 dict 断言因此随机器状态漂移）。
    仅当字符串以 ``{`` / ``[`` 开头才尝试解析；普通文本（console 消息等）
    或解析失败时保持原样，行为不变。
    """
    if isinstance(data, str) and data[:1] in ("{", "["):
        try:
            return json.loads(data)
        except ValueError:
            return data
    return data


def add_log(request_id: str, step: str, data=None) -> None:
    store = get_trace_store()
    store.save_entry(request_id, {
        "timestamp": time.time(),
        "step": step,
        # FIX: A2 —— data 可能是调用方透传的原始用户 payload（如 POST /debug
        # 的 request body），入库前必须脱敏；此前仅 trace_repo 的 save_* 系列
        # 脱敏，本直写路径绕过了"存储边界统一脱敏"承诺（重复脱敏幂等无害）
        # FIX(R8) —— 字符串 payload 先按 A2 契约归一成结构化值，两种后端一致
        "data": redact_nested(_coerce_structured_payload(data)),
    })
    # 持久化新 trace 数据后失效 Dashboard 概览缓存，使新数据立即可见
    # （save_entry 路径：覆盖 save_trace/network/ui/console 等所有写入）。
    # 用惰性 import 打破 core→api 的潜在循环依赖；失败不影响写入主流程。
    try:
        from app.api.dashboard import invalidate_cache
        invalidate_cache()
    except Exception:
        pass


def add_logs_batch(request_id: str, items: list[tuple[str, Any]]) -> None:
    """批量写入多条日志。items 为 (step, data) 元组列表。

    相比逐条 add_log，减少 get_trace_store() 调用次数与 dashboard 缓存失效次数。
    写入顺序与 items 列表顺序一致（对 SEC-13 commit-marker 语义重要）。
    """
    store = get_trace_store()
    now = time.time()
    entries = [
        # FIX: A2 —— 与 add_log 一致，批量直写路径同样在存储边界脱敏
        # FIX(R8) —— 同口径先归一 JSON 字符串 payload
        {"timestamp": now, "step": step,
         "data": redact_nested(_coerce_structured_payload(data))}
        for step, data in items
    ]
    store.save_entries(request_id, entries)
    # 批量写入后单次失效缓存，替代逐条失效
    try:
        from app.api.dashboard import invalidate_cache
        invalidate_cache()
    except Exception:
        pass


def get_logs(request_id: str) -> list[dict]:
    store = get_trace_store()
    return store.get_entries(request_id)


def delete_logs(request_id: str) -> None:
    store = get_trace_store()
    store.delete(request_id)


def list_request_ids(limit: int = 50) -> list[str]:
    store = get_trace_store()
    return store.list_request_ids(limit)
