"""Beacon 短时令牌（CODE_REVIEW S1）。

背景：``sendBeacon`` / ``EventSource`` 无法设置自定义 header，历史实现把永久
API Key 放进 ``?api_key=`` 查询参数——会被反向代理 / CDN / 浏览器历史 / Referer
以明文持久记录，构成永久 Key 泄露面。

方案：SDK 先携带永久 Key（header）调用 ``POST /auth/beacon-token`` 换取短时
（默认 60s）作用域令牌；随后 URL 中只带该令牌上报。令牌：
- 随机生成，仅存哈希（防存储侧泄露明文）
- 绑定 role 与 scope 前缀（默认 ``/ingest``），超出作用域 fail-closed
- 短 TTL（``beacon_token_ttl_seconds``），过期后失效

FIX(v0.7.1-b7-4): 令牌**可重放**——TTL 内同一令牌可被复用（无单次使用跟踪），
泄露的令牌在其剩余 TTL 内等同其 role/scope 的凭证。短 TTL（默认 60s）把重放
窗口压到最小，且令牌仅授权受限 scope（/ingest 上报 + dashboard 只读流），
不含管理权限；此语义为刻意设计（避免每次上报引入存储状态），显式文档化。

存储：优先复用 Redis（``state_backend=redis``，多实例共享），否则进程内存（单机）。
"""

import hashlib
import json
import secrets
import threading
import time

from app.config import settings

_mem: dict[str, dict] = {}
_lock = threading.Lock()

# 内存令牌表容量上限。满时先清理已过期项，仍满则驱逐最接近过期的令牌
# （优先保留较新令牌），防止 Redis 降级/单机模式下 _mem 无限增长（内存泄漏）。
_MAX_MEM_TOKENS = 10000


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _redis():
    """返回 RedisStateStore 的 redis 客户端；非 Redis 后端返回 None。"""
    from app.state.store import get_state_store

    store = get_state_store()
    return getattr(store, "_r", None)


def _scopes(raw: str | None) -> list[str]:
    """解析逗号分隔的作用域前缀列表。"""
    raw = raw or settings.beacon_token_scope
    return [s.strip() for s in raw.split(",") if s.strip()]


def issue_beacon_token(role: str, scope: str | None = None) -> str:
    """签发一个短时 beacon 令牌，绑定 role 与 scope。返回明文 token。"""
    token = secrets.token_urlsafe(32)
    ttl = max(1, int(settings.beacon_token_ttl_seconds))
    effective_scope = scope or settings.beacon_token_scope
    payload = {
        "role": role or "viewer",
        "scope": effective_scope,
        "expires_at": time.time() + ttl,
    }
    key = f"beacon:{_digest(token)}"

    r = _redis()
    if r is not None:
        try:
            r.setex(key, ttl, json.dumps(payload))
            return token
        except Exception:
            # Redis 异常时降级到内存（与限流失败关闭语义相反，这里降级不影响可用性）
            pass
    with _lock:
        # P3-5: 达容量上限时先清理所有已过期项；仍满则按 expires_at 升序
        # 驱逐最接近过期的项，直到留出空位（优先保留较新令牌）。
        if len(_mem) >= _MAX_MEM_TOKENS:
            now = time.time()
            for expired in [k for k, v in _mem.items() if v["expires_at"] <= now]:
                _mem.pop(expired, None)
            while len(_mem) >= _MAX_MEM_TOKENS:
                oldest = min(_mem, key=lambda k: _mem[k]["expires_at"])
                _mem.pop(oldest, None)
        _mem[key] = payload
    return token


def verify_beacon_token(token: str, path: str) -> str | None:
    """校验令牌是否有效且作用域覆盖 ``path``。有效返回 role，否则返回 None（fail-closed）。"""
    if not token:
        return None
    key = f"beacon:{_digest(token)}"

    payload: dict | None = None
    r = _redis()
    if r is not None:
        try:
            raw = r.get(key)
            if raw:
                payload = json.loads(raw)
        except Exception:
            payload = None
    if payload is None:
        with _lock:
            payload = _mem.get(key)
            if payload is not None and payload["expires_at"] <= time.time():
                _mem.pop(key, None)
                payload = None

    if payload is None:
        return None
    if payload["expires_at"] <= time.time():
        return None
    for scope in _scopes(payload.get("scope")):
        # FIX: P3-4 startswith 无边界，/ingest 会误放行 /ingest-malicious / /ingestion / /ingestfoo
        # 等前缀相似但属于不同端点的路径。要求 path 等于 scope，或以 scope + "/" 开头。
        if path == scope or path.startswith(scope + "/"):
            return payload.get("role") or "viewer"
    return None
