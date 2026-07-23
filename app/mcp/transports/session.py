"""MCP 会话注册表 —— 管理 Streamable HTTP 传输的会话生命周期"""

import uuid
import time
import threading
import copy
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MCPSession:
    session_id: str
    initialized: bool = False
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)


class SessionRegistry:
    """线程安全的 MCP 会话表"""

    def __init__(self):
        self._sessions: dict[str, MCPSession] = {}
        self._lock = threading.Lock()

    def create(self) -> MCPSession:
        sid = str(uuid.uuid4())
        s = MCPSession(session_id=sid)
        with self._lock:
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
