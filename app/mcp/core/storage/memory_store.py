"""线程安全的内存存储 —— 加锁防并发崩溃"""

import time
import threading
from collections import OrderedDict
from typing import Optional

from app.mcp.core.storage.base import TraceStorage, SessionStorage


class MemoryTraceStore(TraceStorage):
    def __init__(self, max_entries: int = 10000):
        # OrderedDict 保留插入顺序，用于容量超限时按最旧条目 FIFO 淘汰
        self._store: "OrderedDict[str, list[dict]]" = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def save_entry(self, request_id: str, entry: dict) -> None:
        with self._lock:
            # 新 request_id 入库前，若已达容量上限，淘汰最早插入的条目（FIFO）
            if request_id not in self._store:
                if len(self._store) >= self._max_entries and self._store:
                    self._store.popitem(last=False)  # 弹出最早插入的 request_id
                self._store[request_id] = []
            self._store[request_id].append(entry)

    def save_entries(self, request_id: str, entries: list[dict]) -> None:
        """批量写入（单次锁，原子化）。覆写 ABC 默认实现以减少锁竞争。"""
        with self._lock:
            if request_id not in self._store:
                if len(self._store) >= self._max_entries and self._store:
                    self._store.popitem(last=False)  # 弹出最早插入的 request_id
                self._store[request_id] = []
            self._store[request_id].extend(entries)

    def get_entries(self, request_id: str) -> list[dict]:
        with self._lock:
            return self._store.get(request_id, []).copy()

    def delete(self, request_id: str) -> None:
        with self._lock:
            self._store.pop(request_id, None)

    def cleanup_expired(self, ttl_seconds: int) -> int:
        now = time.time()
        with self._lock:
            stale = [
                rid for rid, entries in self._store.items()
                if entries and now - entries[-1]["timestamp"] > ttl_seconds
            ]
            for rid in stale:
                del self._store[rid]
        return len(stale)

    def list_request_ids(self, limit: int = 50) -> list[str]:
        with self._lock:
            ranked = [
                (request_id, entries[-1].get("timestamp", 0))
                for request_id, entries in self._store.items()
                if entries
            ]
        ranked.sort(key=lambda item: item[1], reverse=True)
        return [request_id for request_id, _ in ranked[:limit]]


class MemorySessionStore(SessionStorage):
    def __init__(self):
        self._store: dict[str, dict] = {}
        self._lock = threading.Lock()

    def save(self, session_id: str, data: dict) -> None:
        data["last_active"] = time.time()
        with self._lock:
            self._store[session_id] = data.copy()

    def get(self, session_id: str) -> Optional[dict]:
        with self._lock:
            s = self._store.get(session_id)
            if s:
                s["last_active"] = time.time()
                return s.copy()
        return None

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._store.pop(session_id, None)

    def list_active(self, ttl_seconds: int) -> list[dict]:
        now = time.time()
        with self._lock:
            return [
                s.copy() for s in self._store.values()
                if now - s.get("last_active", 0) < ttl_seconds
            ]

    def cleanup_expired(self, ttl_seconds: int) -> int:
        now = time.time()
        with self._lock:
            stale = [
                sid for sid, s in self._store.items()
                if now - s.get("last_active", 0) > ttl_seconds
            ]
            for sid in stale:
                del self._store[sid]
        return len(stale)
