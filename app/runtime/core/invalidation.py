"""写入侧 → 展示侧的缓存失效广播（W14 / P1-ARC-1）。

``app/runtime`` 是下层，**不得**反向 import ``app/api``——这与
``app/runtime/__init__.py`` 声明的「MCP / API 层作为适配器依赖本包；本包不
依赖任何协议层」直接冲突。此前 ``runtime/core/logs.py``（2 处）与
``runtime/core/errors.py``（1 处）各自惰性 ``from app.api.dashboard import
invalidate_cache`` 并包在 ``except Exception: pass`` 里：同一份代码复制三遍，
既构成 runtime→api 的反向依赖，又把缓存失效故障**静默吞掉**（Dashboard 会一直
显示旧数据而无人知晓）。

现改为订阅/广播：展示侧（``app/api/dashboard.py``）在导入时注册监听器，
runtime 写入后只广播，不认识任何具体消费者。降级语义保留（失效失败绝不穿透
写入主链路，与 ``app/api/dashboard_events.py`` 一致），但失败**记 warning**
而不是静默 pass。

本模块只依赖 stdlib，且不得 import 任何 ``app.api`` / ``app.mcp`` 符号。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger("lujo-mcp.runtime.invalidation")

_lock = threading.Lock()
# name -> listener。按名字登记，重复注册同名监听器是覆盖而不是叠加
# （模块可能被多次导入/测试可能重复注册，叠加会让一次写入触发 N 次失效）。
_listeners: dict[str, Callable[[], None]] = {}


def register_invalidation_listener(name: str, listener: Callable[[], None]) -> None:
    """登记一个「运行现场已写入」的监听器（同名覆盖，幂等）。

    ``listener`` 必须是无参可调用。想让它跟随目标模块的后续替换（例如测试用
    ``monkeypatch`` 换掉 ``invalidate_cache``），就注册一个**晚绑定**的包装
    （``lambda: invalidate_cache()``）而不是函数对象本身。
    """
    with _lock:
        _listeners[name] = listener


def unregister_invalidation_listener(name: str) -> None:
    """注销监听器（幂等；未登记过也不报错）。"""
    with _lock:
        _listeners.pop(name, None)


def listener_names() -> list[str]:
    """当前已登记的监听器名（只读快照，供诊断与测试断言）。"""
    with _lock:
        return sorted(_listeners)


def notify_data_written() -> None:
    """广播一次「运行现场已写入」，逐个调用监听器。

    锁内只取快照，调用在锁外（监听器可能再取别的锁，锁内调用会有死锁面）。
    单个监听器抛异常不影响其余监听器，也不向写入主链路传播——但会记 warning，
    因为静默吞掉正是本模块要替换掉的旧行为。
    """
    with _lock:
        snapshot = list(_listeners.items())
    for name, listener in snapshot:
        try:
            listener()
        except Exception:
            logger.warning(
                "缓存失效监听器 %s 执行失败（Dashboard 可能短暂显示旧数据）",
                name,
                exc_info=True,
            )
