"""SSE 广播中心 —— 服务端主动向客户端推送消息（notifications）

⚠️ 当前状态（v0.3.0）：notifications 推送未实现，仅响应通道可用。
   - SSEHub 已被 mcp_routes.py 接线（GET /mcp SSE 流 + POST SSE 响应），
     但 hub.publish() 无业务调用方，server→client 主动推送尚未接入。
   - 现有 SSE 流仅用于单次 JSON-RPC 响应的 event-stream 通道。
   - notifications 主动推送为规划中功能。
"""

import json
import asyncio
import logging
from typing import Dict, List

logger = logging.getLogger("ai-debug-mcp.mcp.sse")


class SSEHub:
    """按 session 维护 asyncio.Queue 列表，支持跨线程发布"""

    def __init__(self):
        self._queues: Dict[str, List[asyncio.Queue]] = {}

    def subscribe(self, session_id: str) -> asyncio.Queue:
        """订阅 SSE 流。

        SEC-04 会话隔离：session_id 必须为已注册的有效会话，否则拒绝订阅。
        """
        # SEC-04: 防御性校验 —— session_id 必须与已注册会话绑定
        from app.mcp.transports.session import registry
        if not registry.get(session_id):
            logger.warning("SSE 订阅被拒：session_id=%s 未注册", session_id)
            raise PermissionError(f"无效或未注册的 session_id: {session_id}")

        q: asyncio.Queue = asyncio.Queue()
        self._queues.setdefault(session_id, []).append(q)
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue) -> None:
        qs = self._queues.get(session_id)
        if qs and q in qs:
            qs.remove(q)

    def publish(self, session_id: str, message: dict) -> None:
        """从任意线程发布 server→client 消息（用于未来的 notifications）"""
        qs = self._queues.get(session_id)
        if not qs:
            return
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        for q in list(qs):
            loop.call_soon_threadsafe(q.put_nowait, message)

    @staticmethod
    def format_event(message: dict) -> str:
        return f"event: message\ndata: {json.dumps(message, ensure_ascii=False)}\n\n"


# 全局单例
hub = SSEHub()
