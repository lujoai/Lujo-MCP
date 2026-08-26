"""追踪日志模块 —— 封装存储层的便捷 API"""

import time
import uuid
from typing import Any

from app.runtime.core.redaction import redact_nested
from app.runtime.core.storage.factory import get_trace_store


def create_request_id() -> str:
    return str(uuid.uuid4())


def add_log(request_id: str, step: str, data=None) -> None:
    store = get_trace_store()
    store.save_entry(request_id, {
        "timestamp": time.time(),
        "step": step,
        # FIX: A2 —— data 可能是调用方透传的原始用户 payload（如 POST /debug
        # 的 request body），入库前必须脱敏；此前仅 trace_repo 的 save_* 系列
        # 脱敏，本直写路径绕过了"存储边界统一脱敏"承诺（重复脱敏幂等无害）
        "data": redact_nested(data),
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
        {"timestamp": now, "step": step, "data": redact_nested(data)}
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
