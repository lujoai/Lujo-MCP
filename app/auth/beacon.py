"""Beacon 短时令牌（CODE_REVIEW S1）。

背景：``sendBeacon`` / ``EventSource`` 无法设置自定义 header，历史实现把永久
API Key 放进 ``?api_key=`` 查询参数——会被反向代理 / CDN / 浏览器历史 / Referer
以明文持久记录，构成永久 Key 泄露面。

方案：SDK 先携带永久 Key（header）调用 ``POST /auth/beacon-token`` 换取短时
（默认 60s）作用域令牌；随后 URL 中只带该令牌上报。令牌：
- 随机生成，仅存哈希（防存储侧泄露明文）
- 绑定 role 与 scope 前缀（默认 ``/ingest``），超出作用域 fail-closed
- 短 TTL（``beacon_token_ttl_seconds``），过期后失效

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
        if path.startswith(scope):
            return payload.get("role") or "viewer"
    return None
