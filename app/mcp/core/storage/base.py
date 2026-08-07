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

    def ping(self) -> bool:
        """连通性探活（可选，A1）：健康检查经此抽象而非直接操作后端连接池。

        默认实现恒返回 True；需要真实探活的后端（如 PG）覆写为实际检查。
        探活失败返回 False，不应抛异常。
        """
        return True


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


class ErrorStorage(ABC):
    """错误持久化存储的抽象接口（Phase 2.3 errors 表）。

    方案 C 拆分：把 PG 专属的模块级 `upsert_error` 收敛为 ABC 契约，
    使 memory / pg / async_pg 三后端契约对齐，调用方经工厂分发而非硬编码 pg import。
    """

    @abstractmethod
    def upsert_error(self, record_data: dict) -> None:
        """upsert 一条错误记录（按 fingerprint+session_id 去重聚合）。"""
        ...


class SpecStorage(ABC):
    """Spec 持久化存储的抽象接口（Phase 2.4 specs 表）。

    方案 C 拆分：把 PG 专属的模块级 spec CRUD 收敛为 ABC 契约，
    消除 spec_store.py 对 pg_store 模块函数的硬编码 import 与 try/except 降级。
    """

    @abstractmethod
    def save_spec(self, spec: dict) -> None:
        """upsert 一条 spec（按 id 去重）。"""
        ...

    @abstractmethod
    def get_spec(self, spec_id: str) -> Optional[dict]:
        """读取一条 spec，不存在返回 None。"""
        ...

    @abstractmethod
    def list_specs(
        self,
        kind: Optional[str] = None,
        target: Optional[str] = None,
    ) -> list[dict]:
        """列出 specs（可按 kind/target 过滤），按 updated_at 倒序。"""
        ...

    @abstractmethod
    def delete_spec(self, spec_id: str) -> bool:
        """删除一条 spec，返回是否删除成功。"""
        ...
