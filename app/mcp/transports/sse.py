"""SSE 广播中心 —— 服务端主动向客户端推送消息（notifications）"""

import json
import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, List

logger = logging.getLogger("ai-debug-mcp.mcp.sse")
_CLOSE_EVENT = {"_sse_control": "close"}


@dataclass
class _Subscription:
    queue: asyncio.Queue
    loop: asyncio.AbstractEventLoop


class SSEHub:
    """按 session 维护 asyncio.Queue 列表，支持跨线程发布"""

    def __init__(self):
        self._queues: Dict[str, List[_Subscription]] = {}

    def subscribe(self, session_id: str) -> asyncio.Queue:
        """订阅 SSE 流。

        SEC-04 会话隔离：session_id 必须为已注册的有效会话，否则拒绝订阅。
        """
        # SEC-04: 防御性校验 —— session_id 必须与已注册会话绑定
        from app.mcp.transports.session import registry
        if not registry.get(session_id):
            logger.warning("SSE 订阅被拒：session_id=%s 未注册", session_id)
            raise PermissionError(f"无效或未注册的 session_id: {session_id}")

        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        self._queues.setdefault(session_id, []).append(_Subscription(queue=q, loop=loop))
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue) -> None:
        qs = self._queues.get(session_id)
        if not qs:
            return

        for sub in list(qs):
            if sub.queue is q:
                qs.remove(sub)
                break
        if not qs:
            self._queues.pop(session_id, None)

    def publish(self, session_id: str, message: dict) -> bool:
        """从任意线程发布 server→client 消息。"""
        qs = self._queues.get(session_id)
        if not qs:
            return False

        delivered = False
        for sub in list(qs):
            try:
                sub.loop.call_soon_threadsafe(sub.queue.put_nowait, message)
                delivered = True
            except RuntimeError:
                logger.warning("SSE 发布失败：事件循环不可用，session_id=%s", session_id)
        return delivered

    def publish_notification(self, session_id: str, method: str, params: dict | None = None) -> bool:
        """发布 JSON-RPC notification 到指定 session。"""
        return self.publish(
            session_id,
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
            },
        )

    def subscriber_count(self, session_id: str) -> int:
        return len(self._queues.get(session_id, []))

    def close_session(self, session_id: str) -> int:
        qs = self._queues.pop(session_id, [])
        for sub in list(qs):
            try:
                sub.loop.call_soon_threadsafe(sub.queue.put_nowait, _CLOSE_EVENT)
            except RuntimeError:
                logger.warning("SSE 关闭失败：事件循环不可用，session_id=%s", session_id)
        return len(qs)

    @staticmethod
    def format_event(message: dict) -> str:
        return f"event: message\ndata: {json.dumps(message, ensure_ascii=False)}\n\n"

    @staticmethod
    def is_close_event(message: dict) -> bool:
        return message == _CLOSE_EVENT


# 全局单例
hub = SSEHub()
