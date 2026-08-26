"""SSE 广播中心 —— 服务端主动向客户端推送消息（notifications）"""

import json
import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Dict, List

logger = logging.getLogger("lujo-mcp.mcp.sse")
_CLOSE_EVENT = {"_sse_control": "close"}


@dataclass
class _Subscription:
    queue: asyncio.Queue
    loop: asyncio.AbstractEventLoop


class SSEHub:
    """按 session 维护 asyncio.Queue 列表，支持跨线程发布"""

    # FIX: P1-10a 每订阅队列有界，防止慢消费客户端导致无界内存增长
    _QUEUE_MAXSIZE = 256
    # FIX: P3-7 每 session 订阅数上限，防止持有效 session_id 打开无限 SSE 长连接
    _MAX_SUBSCRIBERS_PER_SESSION = 5

    def __init__(self):
        self._queues: Dict[str, List[_Subscription]] = {}
        # FIX: P3-11 保护 _queues 字典结构跨线程读改写，publish/close_session 可能从任意线程调用
        self._lock = threading.Lock()

    @staticmethod
    def _is_response(message) -> bool:
        """是否为 JSON-RPC 响应（带 id 无 method）。

        带响应的请求方（mcp_post 已回 202）在等待匹配该 id 的响应——
        丢弃响应 = 客户端该请求永久悬挂。通知（有 method 无 id）则可容忍丢失。
        """
        return isinstance(message, dict) and "id" in message and "method" not in message

    @classmethod
    def _publish_locked(cls, q: asyncio.Queue, message: dict) -> None:
        """在事件循环线程内执行：队列满时的分级丢弃策略。

        FIX: P1-C3 —— 旧策略无条件"丢最旧"会静默丢弃带 id 的 JSON-RPC 响应
        （mcp_post 已对该请求返回 202，客户端将永久悬挂），且无日志无指标。
        现按消息类别分级：
        - close 控制事件：必须送达（否则连接悬挂），无条件丢最旧腾位；
        - 响应类：优先挤掉最旧的**通知类**消息腾位（丢通知可接受）；
          队列全为在途响应（客户端实质失联）才丢弃最旧响应，并记 error；
        - 通知类：优先挤掉最旧通知；队列全为在途响应时直接不投递本条通知
          （宁可丢新通知，不丢任何在途响应），记 warning。
        扫描在事件循环线程内同步完成（无 await），不存在并发消费窗口。
        """
        if not q.full():
            q.put_nowait(message)
            return

        # close 控制事件必须送达（P3-11：丢 close 会让客户端连接悬挂）
        if cls.is_close_event(message):
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            q.put_nowait(message)
            return

        # 挤掉最旧的一条通知类消息（其余相对顺序不变）
        items: list = []
        while True:
            try:
                items.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
        dropped_notification = None
        for old in items:
            if dropped_notification is None and not cls._is_response(old):
                dropped_notification = old
                continue
            q.put_nowait(old)

        if cls._is_response(message):
            if dropped_notification is not None:
                logger.warning("SSE 队列满：丢弃最旧通知为响应腾位（响应不可丢）")
                q.put_nowait(message)
            else:
                # 队列全为在途未消费响应：客户端实质失联
                try:
                    evicted = q.get_nowait()
                except asyncio.QueueEmpty:
                    evicted = None
                if evicted is not None:
                    logger.error(
                        "SSE 队列满且均为未消费响应，丢弃最旧响应 id=%r（该请求将悬挂）",
                        evicted.get("id"),
                    )
                q.put_nowait(message)
        else:
            if dropped_notification is not None:
                logger.warning("SSE 队列满：丢弃最旧通知")
                q.put_nowait(message)
            else:
                logger.warning("SSE 队列满且均为在途响应：丢弃本条通知（保住在途响应）")

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
        # FIX: P1-10a 有界队列（maxsize=256），满时由 _publish_locked 丢最旧
        q: asyncio.Queue = asyncio.Queue(maxsize=self._QUEUE_MAXSIZE)
        with self._lock:
            # FIX: P3-7 锁内检查订阅数上限，防止同一有效 session 开无限长连接
            if len(self._queues.get(session_id, [])) >= self._MAX_SUBSCRIBERS_PER_SESSION:
                raise PermissionError("SSE 订阅数达上限")
            self._queues.setdefault(session_id, []).append(_Subscription(queue=q, loop=loop))
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue) -> None:
        with self._lock:
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
        # FIX: P3-11 锁内复制订阅列表，锁外再跨线程投递，避免持锁调用 call_soon_threadsafe
        with self._lock:
            qs = list(self._queues.get(session_id, []))
        if not qs:
            return False

        delivered = False
        for sub in qs:
            try:
                # FIX: P1-10a 满时丢最旧由 _publish_locked 在 loop 线程内原子处理
                sub.loop.call_soon_threadsafe(
                    SSEHub._publish_locked, sub.queue, message
                )
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
        with self._lock:
            return len(self._queues.get(session_id, []))

    def close_session(self, session_id: str) -> int:
        with self._lock:
            qs = self._queues.pop(session_id, [])
        for sub in qs:
            try:
                # FIX: 满队列时通过 _publish_locked 丢最旧一条保底入队，避免触发 QueueFull 导致客户端连接悬挂
                sub.loop.call_soon_threadsafe(
                    SSEHub._publish_locked, sub.queue, _CLOSE_EVENT
                )
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
