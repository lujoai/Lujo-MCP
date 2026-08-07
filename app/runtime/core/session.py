"""MCP 会话管理模块（使用存储层）"""

import time
import uuid
from typing import Optional

from app.mcp.core.storage.factory import get_session_store
from app.config import settings


class SessionManager:
    """会话管理器 —— 封装存储层"""

    def create(self, metadata: Optional[dict] = None) -> dict:
        session_id = str(uuid.uuid4())
        data = {
            "session_id": session_id,
            "created_at": time.time(),
            "last_active": time.time(),
            "metadata": metadata or {},
        }
        get_session_store().save(session_id, data)
        return data

    def get(self, session_id: str) -> Optional[dict]:
        return get_session_store().get(session_id)

    def remove(self, session_id: str) -> bool:
        s = get_session_store().get(session_id)
        if s:
            get_session_store().delete(session_id)
            return True
        return False

    def list_active(self, ttl_seconds: Optional[int] = None) -> list[dict]:
        ttl = ttl_seconds or settings.session_ttl_seconds
        return get_session_store().list_active(ttl)

    def cleanup(self, ttl_seconds: Optional[int] = None) -> int:
        ttl = ttl_seconds or settings.session_ttl_seconds
        return get_session_store().cleanup_expired(ttl)


# 全局单例
session_manager = SessionManager()
