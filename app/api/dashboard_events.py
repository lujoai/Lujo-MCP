"""Dashboard 实时事件总线 —— SSE 推送通道

与 MCP 传输层 ``SSEHub`` 的职责区分：
- ``SSEHub``（``app/mcp/transports/sse.py``）按 MCP ``session_id`` 隔离（SEC-04），
  服务于 JSON-RPC notifications，订阅需有效已注册会话；
- ``DashboardEventBus`` 面向公开 Web 控制台，**无 session 门槛**，广播式推送，
  供 ``dashboard.html`` 通过 ``EventSource`` 订阅 trace/error 变更信号。

线程安全：``publish`` 可从任意线程调用（trace 写入路径可能位于同步上下文 /
异常钩子线程 / 守护线程），通过 ``call_soon_threadsafe`` 跨线程投递到各订阅者
的事件循环。无订阅者时 ``publish`` 为 no-op（零开销），主链路写入不受影响。

降级语义：与 ``invalidate_cache`` 一致 —— 任何异常静默吞掉，绝不穿透写入主链路。
"""

import asyncio
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger("lujo-mcp.dashboard.sse")

# 关闭信号：close_all 投递给订阅者以促使其退出消费循环
_CLOSE_EVENT = {"_dashboard_control": "close"}


@dataclass
class _DashboardSubscription:
    queue: asyncio.Queue
    loop: asyncio.AbstractEventLoop


class DashboardEventBus:
    """广播式 SSE 事件总线（Dashboard 实时推送）。

    每个订阅者持有独立 ``asyncio.Queue``；``publish`` 把同一事件投递到全部订阅者。
    设计要点：
    - 无订阅者时 ``publish`` 直接返回 0（零开销），写入主链路无感；
    - 队列满（消费过慢）时丢弃最旧事件保最新，**实时性优先于完整性**；
    - 订阅者事件循环关闭时自动清理死订阅，避免泄漏。
    """

    def __init__(self) -> None:
        self._subs: list[_DashboardSubscription] = []

    def subscribe(self) -> asyncio.Queue:
        """订阅事件流，返回专属队列。必须在运行中的事件循环内调用。"""
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subs.append(_DashboardSubscription(queue=q, loop=loop))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        for sub in list(self._subs):
            if sub.queue is q:
                self._subs.remove(sub)
                break

    def publish(self, event: dict) -> int:
        """广播事件到所有订阅者，返回成功投递数（含跨线程）。"""
        delivered = 0
        for sub in list(self._subs):
            try:
                sub.loop.call_soon_threadsafe(self._put_nowait, sub, event)
                delivered += 1
            except RuntimeError:
                # 订阅者事件循环已关闭 —— 清理死订阅
                logger.debug("Dashboard SSE 投递失败：事件循环不可用，清理订阅")
                self._safe_remove(sub)
        return delivered

    @staticmethod
    def _put_nowait(sub: _DashboardSubscription, event: dict) -> None:
        """在订阅者事件循环内执行：投递事件，队列满时丢弃最旧。"""
        try:
            sub.queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                sub.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def _safe_remove(self, sub: _DashboardSubscription) -> None:
        try:
            self._subs.remove(sub)
        except ValueError:
            pass

    def subscriber_count(self) -> int:
        return len(self._subs)

    def close_all(self) -> int:
        """优雅停机：向所有订阅者投递关闭信号并清空注册表。返回受影响订阅数。"""
        n = len(self._subs)
        for sub in list(self._subs):
            try:
                sub.loop.call_soon_threadsafe(self._put_nowait, sub, _CLOSE_EVENT)
            except RuntimeError:
                pass
        self._subs.clear()
        return n

    @staticmethod
    def format_event(event: dict) -> str:
        return f"event: message\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

    @staticmethod
    def is_close_event(event: dict) -> bool:
        return event == _CLOSE_EVENT


# 全局单例
dashboard_hub = DashboardEventBus()


def broadcast_dashboard_event(event: dict) -> None:
    """广播 Dashboard 变更事件（写入主链路调用）。

    降级矩阵：
    - ``dashboard_sse_enabled=False``：直接返回（功能关闭，零开销）；
    - 无订阅者：直接返回（无人监听，零开销）；
    - 投递异常：静默吞掉，绝不影响写入主链路
      （与 ``invalidate_cache`` 的降级语义一致）。
    """
    try:
        from app.config import settings
        if not settings.dashboard_sse_enabled:
            return
        if dashboard_hub.subscriber_count() == 0:
            return
        dashboard_hub.publish(event)
    except Exception:
        logger.debug("Dashboard SSE 广播失败", exc_info=True)
