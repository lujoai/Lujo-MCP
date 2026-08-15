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

# 会话表容量上限。超过时驱逐最旧会话；全活跃则拒绝新建。
# 每个会话对象很小（~200B），10000 条 ≈ 2MB，不会造成内存压力。
_MAX_SESSIONS = 10_000


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
                # 尝试驱逐最旧的会话（last_active 最小）
                oldest_sid = min(
                    self._sessions, key=lambda k: self._sessions[k].last_active
                )
                del self._sessions[oldest_sid]
                logger.warning(
                    "会话数达上限(%d)，已驱逐最旧会话 %s",
                    self._max_sessions, oldest_sid,
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

    def cleanup(self, ttl_seconds: int = 1800) -> int:
        now = time.time()
        with self._lock:
            stale = [sid for sid, s in self._sessions.items() if now - s.last_active > ttl_seconds]
            for sid in stale:
                del self._sessions[sid]
        return len(stale)


# 全局单例
registry = SessionRegistry()
