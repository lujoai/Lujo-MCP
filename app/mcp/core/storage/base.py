"""存储抽象层 —— 定义统一接口"""

from abc import ABC, abstractmethod
from typing import Optional


class TraceStorage(ABC):
    """追踪日志存储的抽象接口"""

    @abstractmethod
    def save_entry(self, request_id: str, entry: dict) -> None:
        ...

    @abstractmethod
    def get_entries(self, request_id: str) -> list[dict]:
        ...

    def save_entries(self, request_id: str, entries: list[dict]) -> None:
        """批量写入多条条目（默认逐条写入，子类可覆写优化）。

        子类覆写时应保证：
        - 按 entries 列表顺序写入（保持调用方语义，对 SEC-13 commit-marker 至关重要）
        - 尽可能原子化（全部成功或全部失败）

        默认实现逐条调用 save_entry，适用于未做批量优化的后端（如 PG）。
        """
        for entry in entries:
            self.save_entry(request_id, entry)

    @abstractmethod
    def delete(self, request_id: str) -> None:
        ...

    @abstractmethod
    def cleanup_expired(self, ttl_seconds: int) -> int:
        """清理过期条目，返回清理数量"""
        ...

    def list_request_ids(self, limit: int = 50) -> list[str]:
        """列出最近的 request_id（可选，默认返回空列表）"""
        return []


class SessionStorage(ABC):
    """会话存储的抽象接口"""

    @abstractmethod
    def save(self, session_id: str, data: dict) -> None:
        ...

    @abstractmethod
    def get(self, session_id: str) -> Optional[dict]:
        ...

    @abstractmethod
    def delete(self, session_id: str) -> None:
        ...

    @abstractmethod
    def list_active(self, ttl_seconds: int) -> list[dict]:
        ...

    @abstractmethod
    def cleanup_expired(self, ttl_seconds: int) -> int:
        ...
