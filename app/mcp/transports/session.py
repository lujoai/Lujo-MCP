"""MCP 会话注册表 —— 管理 Streamable HTTP 传输的会话生命周期。

安全设计：
- 会话数上限 ``_MAX_SESSIONS``，防止攻击者高频创建会话撑爆内存。
- 满时先尝试驱逐最旧（``last_active`` 最小）的会话；若全活跃则抛
  ``SessionLimitExceeded``，由调用方返回 503。
"""

import uuid
import time
import threading
import copy
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("lujo-mcp.session")

# 会话表容量上限。超过时驱逐已过期会话；全活跃则拒绝新建。
# 每个会话对象很小（~200B），10000 条 ≈ 2MB，不会造成内存压力。
_MAX_SESSIONS = 10_000

# 会话空闲过期阈值（秒），与 cleanup() 默认 TTL 一致。
# FIX P3-3: 达上限时仅驱逐已过期会话，避免把活跃会话挤下线（驱逐 DoS）。
_SESSION_TTL_SECONDS = 1800


class SessionLimitExceeded(Exception):
    """会话数已达上限且无可驱逐的过期会话。"""


@dataclass
class MCPSession:
    session_id: str
    initialized: bool = False
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)


class SessionRegistry:
    """线程安全的 MCP 会话表"""

    def __init__(self, max_sessions: int = _MAX_SESSIONS):
        self._sessions: dict[str, MCPSession] = {}
        self._lock = threading.Lock()
        self._max_sessions = max_sessions

    def create(self) -> MCPSession:
        sid = str(uuid.uuid4())
        s = MCPSession(session_id=sid)
        with self._lock:
            if len(self._sessions) >= self._max_sessions:
                # FIX P3-3: 仅驱逐已过期会话（与 cleanup 同一 TTL）；
                # 全活跃时抛 SessionLimitExceeded（调用方返回 503），
                # 防止攻击者高频建会话把正常活跃会话挤下线。
                now = time.time()
                expired = [
                    k for k, sess in self._sessions.items()
                    if now - sess.last_active > _SESSION_TTL_SECONDS
                ]
                if not expired:
                    raise SessionLimitExceeded(
                        f"会话数已达上限({self._max_sessions})且全部活跃，拒绝新建"
                    )
                for k in expired:
                    del self._sessions[k]
                logger.warning(
                    "会话数达上限(%d)，已驱逐 %d 个过期会话",
                    self._max_sessions, len(expired),
                )
            self._sessions[sid] = s
        return s

    def get(self, sid: str) -> Optional[MCPSession]:
        with self._lock:
            s = self._sessions.get(sid)
            if s:
                s.last_active = time.time()
                return copy.copy(s)  # 返回副本，避免外部并发修改
            return None

    def mark_initialized(self, sid: str) -> None:
        with self._lock:
            s = self._sessions.get(sid)
            if s:
                s.initialized = True

    def delete(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

    def cleanup(self, ttl_seconds: int = 1800) -> list[str]:
        """删除过期会话，返回被清理的 session_id 列表。

        FIX P3-14: 此前返回 int 数量，调用方无法据此通知 SSE hub 关闭悬挂流。
        改为返回被清理的 sid 列表，供 periodic_cleanup 逐个 hub.close_session()。
        """
        now = time.time()
        with self._lock:
            stale = [sid for sid, s in self._sessions.items() if now - s.last_active > ttl_seconds]
            for sid in stale:
                del self._sessions[sid]
        return stale


# 全局单例
registry = SessionRegistry()
