"""追踪日志模块 —— 封装存储层的便捷 API"""

import time
import uuid

from app.mcp.core.storage.factory import get_trace_store


def create_request_id() -> str:
    return str(uuid.uuid4())


def add_log(request_id: str, step: str, data=None) -> None:
    store = get_trace_store()
    store.save_entry(request_id, {
        "timestamp": time.time(),
        "step": step,
        "data": data,
    })
    # 持久化新 trace 数据后失效 Dashboard 概览缓存，使新数据立即可见
    # （save_entry 路径：覆盖 save_trace/network/ui/console 等所有写入）。
    # 用惰性 import 打破 core→api 的潜在循环依赖；失败不影响写入主流程。
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
