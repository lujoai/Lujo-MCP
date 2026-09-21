"""线程安全的内存存储 —— 加锁防并发崩溃"""

import time
import threading
from collections import OrderedDict
from typing import Optional

from app.runtime.core.storage.base import TraceStorage, SessionStorage


class MemoryTraceStore(TraceStorage):
    # FIX(v0.7.1-b8-4): 单 request_id 条目数上限——此前只有 request_id 数量上限
    # （_max_entries），单个 request_id 的 entry 列表无界（长 trace/恶意刷单条
    # request_id 可无界涨内存）；现按「保留最新 N 条、丢最旧」截断。
    _MAX_ENTRIES_PER_REQUEST = 5000

    # ⚠️ 已知边界（W13 / P3-STORE-3，裁定：**不改计量口径，只登记**）：容量按
    # **条数**计（max_entries 个 request_id × 每个 _MAX_ENTRIES_PER_REQUEST 条），
    # 不按字节计，所以理论上限很松（默认 10000 × 5000）。改成按字节预算会直接
    # 改变「用户能查到多久以前的现场」这一可感知行为 —— 那是产品决策，不在维护
    # 包里顺手改。实际约束来自两处：单条 payload 受 MAX_BODY_SIZE（默认 1 MiB）
    # 与采集侧截断限制，以及 periodic_cleanup 的 TTL 清理（注意：该任务只在
    # HTTP/统一模式的 lifespan 里起，纯 stdio 下不跑，此时唯一约束就是条数上限）。
    # 真要收紧，正确做法是新增一个可配置的字节预算并与条数上限取严，且必须同时
    # 给出「清理任务在 stdio 下也跑」的方案。

    def __init__(self, max_entries: int = 10000):
        # OrderedDict 保留插入顺序，用于容量超限时按最旧条目 FIFO 淘汰
        self._store: "OrderedDict[str, list[dict]]" = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def _append_entry(self, request_id: str, entry: dict) -> None:
        self._store[request_id].append(entry)
        if len(self._store[request_id]) > self._MAX_ENTRIES_PER_REQUEST:
            # 丢最旧（FIFO），保持有界
            del self._store[request_id][:-self._MAX_ENTRIES_PER_REQUEST]

    def save_entry(self, request_id: str, entry: dict) -> None:
        with self._lock:
            # 新 request_id 入库前，若已达容量上限，淘汰最早插入的条目（FIFO）
            if request_id not in self._store:
                if len(self._store) >= self._max_entries and self._store:
                    self._store.popitem(last=False)  # 弹出最早插入的 request_id
                self._store[request_id] = []
            self._append_entry(request_id, entry)

    def save_entries(self, request_id: str, entries: list[dict]) -> None:
        """批量写入（单次锁，原子化）。覆写 ABC 默认实现以减少锁竞争。"""
        with self._lock:
            if request_id not in self._store:
                if len(self._store) >= self._max_entries and self._store:
                    self._store.popitem(last=False)  # 弹出最早插入的 request_id
                self._store[request_id] = []
            for entry in entries:
                self._append_entry(request_id, entry)

    def get_entries(self, request_id: str) -> list[dict]:
        with self._lock:
            return self._store.get(request_id, []).copy()

    def delete(self, request_id: str) -> None:
        with self._lock:
            self._store.pop(request_id, None)

    def cleanup_expired(self, ttl_seconds: int) -> int:
        now = time.time()
        with self._lock:
            stale = []
            for rid, entries in self._store.items():
                if not entries:
                    continue
                # W13 / P4：此前直接索引 entries[-1]["timestamp"]，任一条目缺该键
                # 即抛 KeyError，把整个 TTL 清理任务打掉——清理是周期性后台任务，
                # 它一死内存就只增不减（唯一剩下的约束是 max_entries 的 FIFO）。
                # 无法定龄的条目按「不清理」处理：宁可多留，不可误删用户现场。
                ts = entries[-1].get("timestamp")
                if ts is None:
                    continue
                if now - ts > ttl_seconds:
                    stale.append(rid)
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
