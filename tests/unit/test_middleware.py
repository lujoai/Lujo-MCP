"""Unit tests for rate-limiting logic in app.state.store."""

import inspect
import time
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.config import settings
from app.state.store import MemoryStateStore, RedisStateStore


# ---------------------------------------------------------------------------
# MemoryStateStore.allow()
# ---------------------------------------------------------------------------

class TestMemoryStateStoreAllow:
    """Tests for MemoryStateStore.allow()."""

    def test_requests_within_limit_are_allowed(self):
        store = MemoryStateStore()
        limit, window = 3, 10
        for _ in range(limit):
            assert store.allow("client-a", limit, window) is True

    def test_requests_exceeding_limit_are_rejected(self):
        store = MemoryStateStore()
        limit, window = 3, 10
        for _ in range(limit):
            store.allow("client-a", limit, window)
        # The next request should be rejected
        assert store.allow("client-a", limit, window) is False

    def test_window_expiry_cleans_old_timestamps(self):
        store = MemoryStateStore()
        limit, window = 2, 0.1  # 100 ms window
        # Consume the limit
        assert store.allow("client-a", limit, window) is True
        assert store.allow("client-a", limit, window) is True
        # Should be rejected now
        assert store.allow("client-a", limit, window) is False

        # Wait for the window to expire
        time.sleep(0.15)

        # Old timestamps should be cleaned up; requests allowed again
        assert store.allow("client-a", limit, window) is True


# ---------------------------------------------------------------------------
# RedisStateStore.allow()  (Redis mocked) — sliding-window ZSET
# ---------------------------------------------------------------------------

class TestRedisStateStoreAllow:
    """Tests for RedisStateStore.allow() sliding-window ZSET implementation."""

    @patch("app.state.store.RedisStateStore.__init__", return_value=None)
    def _make_store(self, _mock_init):
        """Helper: build a RedisStateStore without touching a real Redis."""
        store = RedisStateStore.__new__(RedisStateStore)
        store._sliding_window_script = MagicMock()
        return store

    def test_allow_calls_sliding_window_script(self):
        store = self._make_store()
        store._sliding_window_script.return_value = 1

        store.allow("key-1", limit=5, window=60)

        store._sliding_window_script.assert_called_once()
        call = store._sliding_window_script.call_args
        assert call.kwargs["keys"] == ["key-1"]
        # args = [now, window, limit, member]
        assert call.kwargs["args"][1] == 60   # window
        assert call.kwargs["args"][2] == 5    # limit

    def test_allow_returns_true_when_within_limit(self):
        store = self._make_store()
        store._sliding_window_script.return_value = 1

        assert store.allow("key-1", limit=5, window=60) is True

    def test_allow_returns_false_when_exceeds_limit(self):
        store = self._make_store()
        store._sliding_window_script.return_value = 0

        assert store.allow("key-1", limit=5, window=60) is False

    def test_fail_closed_on_exception(self):
        store = self._make_store()
        store._sliding_window_script.side_effect = RuntimeError("boom")

        with patch("app.state.store.logger") as mock_logger:
            result = store.allow("key-1", limit=5, window=60)

        assert result is False
        mock_logger.error.assert_called_once()


# ---------------------------------------------------------------------------
# 端点级限流测试（P1-3）
# ---------------------------------------------------------------------------

class TestEndpointRateLimit:
    """测试 RateLimitMiddleware._get_endpoint_limit 静态方法"""

    def test_ingest_prefix_uses_120_per_minute(self):
        """/ingest/ 前缀路径应使用 120/min"""
        from app.middleware import RateLimitMiddleware

        limit, window = RateLimitMiddleware._get_endpoint_limit("/ingest/error")
        assert limit == 120
        assert window == 60

    def test_ingest_console_uses_120_per_minute(self):
        """/ingest/console 也应匹配 /ingest/ 前缀"""
        from app.middleware import RateLimitMiddleware

        limit, window = RateLimitMiddleware._get_endpoint_limit("/ingest/console")
        assert limit == 120
        assert window == 60

    def test_debug_analyze_uses_10_per_minute(self):
        """/api/debug/analyze 应使用 10/min"""
        from app.middleware import RateLimitMiddleware

        limit, window = RateLimitMiddleware._get_endpoint_limit("/api/debug/analyze")
        assert limit == 10
        assert window == 60

    def test_verify_ui_uses_5_per_minute(self):
        """/api/debug/verify/ui 应使用 5/min"""
        from app.middleware import RateLimitMiddleware

        limit, window = RateLimitMiddleware._get_endpoint_limit("/api/debug/verify/ui")
        assert limit == 5
        assert window == 60

    def test_unmatched_path_uses_global_default(self):
        """未匹配路径应使用全局默认值"""
        from app.middleware import RateLimitMiddleware

        limit, window = RateLimitMiddleware._get_endpoint_limit("/api/dashboard/traces")
        assert limit > 0  # 全局默认值（来自 settings.rate_limit_per_minute）
        assert window == 60


# ---------------------------------------------------------------------------
# FIX: A1 —— XFF 伪造绕过限流（trusted_proxy_count 可信代理模式）
# ---------------------------------------------------------------------------

def _ip_request(peer: str, headers: dict | None = None):
    """构造最小 Request 桩：_get_client_ip 只访问 client.host 与 headers.get()。"""
    from types import SimpleNamespace

    return SimpleNamespace(
        client=SimpleNamespace(host=peer),
        headers={k.lower(): v for k, v in (headers or {}).items()},
    )


class TestClientIpResolution:
    """A1 回归：限流键的客户端 IP 解析不得信任可伪造的转发头首段。"""

    def test_default_config_ignores_forged_xff(self, monkeypatch):
        """trusted_proxy_count=0（默认）：伪造 XFF 不得改变限流键 —— A1 核心。"""
        from app.middleware import RateLimitMiddleware

        monkeypatch.setattr(settings, "trusted_proxy_count", 0)
        req = _ip_request(
            "203.0.113.99",
            {"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "5.6.7.8"},
        )
        # 旧实现返回伪造的 1.2.3.4 → 每请求换一个伪造 IP 即绕过限流
        assert RateLimitMiddleware._get_client_ip(req) == "203.0.113.99"

    def test_trusted_proxy_resolves_real_client(self, monkeypatch):
        """count=1 + 私网对端（自有反代）：取 XFF 从右数第 2 个为客户端 IP。"""
        from app.middleware import RateLimitMiddleware

        monkeypatch.setattr(settings, "trusted_proxy_count", 1)
        req = _ip_request(
            "192.168.1.10",
            {"X-Forwarded-For": "203.0.113.5, 192.168.1.10"},
        )
        assert RateLimitMiddleware._get_client_ip(req) == "203.0.113.5"

    def test_trusted_proxy_multi_hop(self, monkeypatch):
        """count=2（两层代理）：跳过右起 2 段取真实客户端。"""
        from app.middleware import RateLimitMiddleware

        monkeypatch.setattr(settings, "trusted_proxy_count", 2)
        req = _ip_request(
            "172.16.0.5",
            {"X-Forwarded-For": "203.0.113.5, 10.0.0.1, 172.16.0.5"},
        )
        assert RateLimitMiddleware._get_client_ip(req) == "203.0.113.5"

    def test_trusted_proxy_public_peer_ignores_xff(self, monkeypatch):
        """count=1 但对端是公网直连：XFF 视为伪造，忽略。"""
        from app.middleware import RateLimitMiddleware

        monkeypatch.setattr(settings, "trusted_proxy_count", 1)
        req = _ip_request(
            "8.8.8.8",  # 攻击者公网直连（非自有反代）
            {"X-Forwarded-For": "1.2.3.4, 5.6.7.8"},
        )
        assert RateLimitMiddleware._get_client_ip(req) == "8.8.8.8"

    def test_forged_leftmost_segment_not_trusted(self, monkeypatch):
        """攻击者在 XFF 最左伪造额外段：解析结果不受影响（只取右起第 N+1 段）。"""
        from app.middleware import RateLimitMiddleware

        monkeypatch.setattr(settings, "trusted_proxy_count", 1)
        req = _ip_request(
            "192.168.1.10",
            {"X-Forwarded-For": "1.2.3.4, 203.0.113.5, 192.168.1.10"},
        )
        # 右起第 2 段是 203.0.113.5（真实客户端），伪造的 1.2.3.4 被忽略
        assert RateLimitMiddleware._get_client_ip(req) == "203.0.113.5"

    def test_insufficient_xff_falls_back_to_x_real_ip(self, monkeypatch):
        """XFF 条目不足以越过可信代理数 → 回退 X-Real-IP。"""
        from app.middleware import RateLimitMiddleware

        monkeypatch.setattr(settings, "trusted_proxy_count", 1)
        req = _ip_request(
            "192.168.1.10",
            {"X-Forwarded-For": "203.0.113.5", "X-Real-IP": "198.51.100.7"},
        )
        assert RateLimitMiddleware._get_client_ip(req) == "198.51.100.7"

    def test_invalid_xff_candidate_falls_back(self, monkeypatch):
        """候选段不是合法 IP → 回退 X-Real-IP，再回退对端 IP。"""
        from app.middleware import RateLimitMiddleware

        monkeypatch.setattr(settings, "trusted_proxy_count", 1)
        req = _ip_request(
            "192.168.1.10",
            {"X-Forwarded-For": "not-an-ip, 192.168.1.10", "X-Real-IP": "198.51.100.7"},
        )
        assert RateLimitMiddleware._get_client_ip(req) == "198.51.100.7"

    def test_no_forward_headers_returns_peer(self, monkeypatch):
        """无任何转发头：返回直连对端 IP。"""
        from app.middleware import RateLimitMiddleware

        monkeypatch.setattr(settings, "trusted_proxy_count", 1)
        req = _ip_request("10.0.0.5")
        assert RateLimitMiddleware._get_client_ip(req) == "10.0.0.5"

    def test_missing_client_returns_unknown(self, monkeypatch):
        """无 client 信息（TestClient 边界场景）：兜底 unknown，不抛异常。"""
        from types import SimpleNamespace

        from app.middleware import RateLimitMiddleware

        monkeypatch.setattr(settings, "trusted_proxy_count", 0)
        req = SimpleNamespace(client=None, headers={})
        assert RateLimitMiddleware._get_client_ip(req) == "unknown"


# ---------------------------------------------------------------------------
# FIX: P1-A4 —— 限流 key 用路由模板（动态段端点共享桶，防轮换 ID 绕过）
# ---------------------------------------------------------------------------

def _scope(app, method: str, path: str):
    """构造最小 http scope：_get_route_template 只需要 app/method/path。"""
    return {
        "type": "http",
        "app": app,
        "method": method,
        "path": path,
        "headers": [],
        "query_string": b"",
    }


class TestRouteTemplateResolution:
    """A4 回归：限流 key 的路由模板归一化。"""

    def _make_app(self):
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/ingest/network/{trace_id}")
        def _network(trace_id: str):  # pragma: no cover - 仅注册路由
            return {}

        @app.get("/api/dashboard/traces")
        def _static():  # pragma: no cover
            return {}

        return app

    def test_dynamic_path_normalized_to_template(self):
        """动态段路径 → 路由模板（不同 ID 得到同一 key）。"""
        from fastapi import Request

        from app.middleware import RateLimitMiddleware

        app = self._make_app()
        for tid in ("abc123", "def456", "rec-xyz789abc123"):
            req = Request(_scope(app, "GET", f"/ingest/network/{tid}"))
            assert RateLimitMiddleware._get_route_template(req) == "/ingest/network/{trace_id}"

    def test_static_path_unchanged(self):
        """静态路径：模板 == 原始 path（行为不变）。"""
        from fastapi import Request

        from app.middleware import RateLimitMiddleware

        app = self._make_app()
        req = Request(_scope(app, "GET", "/api/dashboard/traces"))
        assert RateLimitMiddleware._get_route_template(req) == "/api/dashboard/traces"

    def test_method_mismatch_still_resolves_template(self):
        """方法不匹配（PARTIAL）也按路径模板聚合（限流按路径聚合）。"""
        from fastapi import Request

        from app.middleware import RateLimitMiddleware

        app = self._make_app()
        req = Request(_scope(app, "POST", "/api/dashboard/traces"))
        assert RateLimitMiddleware._get_route_template(req) == "/api/dashboard/traces"

    def test_unknown_path_falls_back_to_raw(self):
        """404 路径：回退原始 path（保守）。"""
        from fastapi import Request

        from app.middleware import RateLimitMiddleware

        app = self._make_app()
        req = Request(_scope(app, "GET", "/no/such/route"))
        assert RateLimitMiddleware._get_route_template(req) == "/no/such/route"

    def test_no_app_in_scope_falls_back_to_raw(self):
        """scope 无 app：回退原始 path，不抛异常。"""
        from fastapi import Request

        from app.middleware import RateLimitMiddleware

        req = Request(_scope(None, "GET", "/ingest/network/abc"))
        assert RateLimitMiddleware._get_route_template(req) == "/ingest/network/abc"

    def test_dynamic_ids_share_rate_limit_bucket(self, monkeypatch):
        """行为验证：轮换动态 ID 不再各自成桶 —— 同模板共享限额。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.middleware import RateLimitMiddleware

        monkeypatch.setattr(settings, "rate_limit_per_minute", 2)
        app = FastAPI()
        app.add_middleware(RateLimitMiddleware)

        @app.get("/items/{item_id}")
        def _items(item_id: str):
            return {"ok": True}

        client = TestClient(app)
        # 旧实现：3 个不同 ID = 3 个独立桶，全部 200（限流被绕过）
        # 新实现：同模板共享桶，第 3 次触发 429
        assert client.get("/items/a").status_code == 200
        assert client.get("/items/b").status_code == 200
        assert client.get("/items/c").status_code == 429


# ---------------------------------------------------------------------------
# SEC-07: 限流 fail-closed（Redis 不可用 → 429）
# ---------------------------------------------------------------------------

class TestRateLimitFailClosed:
    """SEC-07: 状态后端不可用时，限流中间件应 fail-closed 返回 429，而非降级放行。"""

    def test_state_store_init_failure_returns_429(self):
        """get_state_store() 初始化失败（如 Redis 不可用）→ 返回 429 而非降级放行。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import app.middleware as mw

        app = FastAPI()

        @app.get("/api/debug/analyze")
        def _analyze():
            return {"ok": True}

        # 仅挂载限流中间件，隔离其它中间件干扰
        app.add_middleware(mw.RateLimitMiddleware)
        client = TestClient(app)

        # 模拟 Redis 不可用：get_state_store() 初始化抛 ConnectionError
        with patch.object(mw, "get_state_store", side_effect=ConnectionError("Redis unavailable")):
            resp = client.get("/api/debug/analyze")

        # fail-closed：状态后端不可用时拒绝请求（429），而非降级放行（200）
        assert resp.status_code == 429
        assert "unavailable" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Track C: AuthMiddleware 多 key 轮换 + RBAC 角色注入
# ---------------------------------------------------------------------------

def _build_auth_app():
    """构造仅挂载 AuthMiddleware 的 FastAPI app，含一个受保护端点 + 一个角色回显端点。"""
    import app.middleware as mw

    app = FastAPI()

    @app.get("/api/debug/test")
    def _test():
        return {"ok": True}

    @app.get("/api/debug/role-test")
    def _role_test(request: Request):
        return {"role": getattr(request.state, "role", None)}

    app.add_middleware(mw.AuthMiddleware)
    return app


class TestAuthMiddlewareMultiKey:
    """多 key 轮换：api_keys 配置的新旧 key 同时有效，无效 key 被拒。"""

    def test_both_new_and_old_keys_pass(self, monkeypatch):
        monkeypatch.setattr(settings, "api_keys", "key1,key2")
        monkeypatch.setattr(settings, "api_key", None)
        monkeypatch.setattr(settings, "rbac_enabled", False)

        client = TestClient(_build_auth_app())
        assert client.get("/api/debug/test", headers={"X-API-Key": "key1"}).status_code == 200
        assert client.get("/api/debug/test", headers={"X-API-Key": "key2"}).status_code == 200

    def test_invalid_key_rejected_401(self, monkeypatch):
        monkeypatch.setattr(settings, "api_keys", "key1,key2")
        monkeypatch.setattr(settings, "api_key", None)

        client = TestClient(_build_auth_app())
        resp = client.get("/api/debug/test", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 401

    def test_bearer_token_auth_still_works(self, monkeypatch):
        """Authorization: Bearer <key> 头仍可鉴权（_extract_key 未改）。"""
        monkeypatch.setattr(settings, "api_keys", "key1,key2")
        monkeypatch.setattr(settings, "api_key", None)

        client = TestClient(_build_auth_app())
        resp = client.get("/api/debug/test", headers={"Authorization": "Bearer key1"})
        assert resp.status_code == 200


class TestAuthMiddlewareRoleInjection:
    """鉴权通过后 request.state.role 应被正确注入。"""

    def test_rbac_disabled_injects_admin(self, monkeypatch):
        """rbac_enabled=False → role="admin"（向后兼容）。"""
        monkeypatch.setattr(settings, "api_keys", "key1")
        monkeypatch.setattr(settings, "api_key", None)
        monkeypatch.setattr(settings, "rbac_enabled", False)
        monkeypatch.setattr(settings, "rbac_role_mapping", "")

        client = TestClient(_build_auth_app())
        resp = client.get("/api/debug/role-test", headers={"X-API-Key": "key1"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    def test_rbac_enabled_injects_mapped_role(self, monkeypatch):
        """rbac_enabled=True + key 命中 mapping → 注入映射角色。"""
        monkeypatch.setattr(settings, "api_keys", "key1,key2")
        monkeypatch.setattr(settings, "api_key", None)
        monkeypatch.setattr(settings, "rbac_enabled", True)
        monkeypatch.setattr(settings, "rbac_role_mapping", "key1:admin,key2:viewer")

        client = TestClient(_build_auth_app())
        assert client.get(
            "/api/debug/role-test", headers={"X-API-Key": "key1"}
        ).json()["role"] == "admin"
        assert client.get(
            "/api/debug/role-test", headers={"X-API-Key": "key2"}
        ).json()["role"] == "viewer"

    def test_rbac_enabled_unmapped_key_gets_viewer(self, monkeypatch):
        """rbac_enabled=True + key 未在 mapping → 注入 viewer（最小权限）。"""
        monkeypatch.setattr(settings, "api_keys", "key1,key3")
        monkeypatch.setattr(settings, "api_key", None)
        monkeypatch.setattr(settings, "rbac_enabled", True)
        monkeypatch.setattr(settings, "rbac_role_mapping", "key1:admin")

        client = TestClient(_build_auth_app())
        resp = client.get("/api/debug/role-test", headers={"X-API-Key": "key3"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "viewer"


class TestAuthMiddlewareBackwardCompat:
    """向后兼容：单 api_key（无 api_keys）仍可工作。"""

    def test_single_api_key_works(self, monkeypatch):
        monkeypatch.setattr(settings, "api_keys", "")
        monkeypatch.setattr(settings, "api_key", "legacy-secret")
        monkeypatch.setattr(settings, "rbac_enabled", False)

        client = TestClient(_build_auth_app())
        assert client.get(
            "/api/debug/test", headers={"X-API-Key": "legacy-secret"}
        ).status_code == 200

    def test_single_api_key_wrong_rejected(self, monkeypatch):
        monkeypatch.setattr(settings, "api_keys", "")
        monkeypatch.setattr(settings, "api_key", "legacy-secret")

        client = TestClient(_build_auth_app())
        assert client.get(
            "/api/debug/test", headers={"X-API-Key": "wrong"}
        ).status_code == 401

    def test_no_keys_configured_auth_disabled(self, monkeypatch):
        """无任何 key 配置 → 鉴权关闭，所有请求放行。"""
        monkeypatch.setattr(settings, "api_keys", "")
        monkeypatch.setattr(settings, "api_key", None)

        client = TestClient(_build_auth_app())
        # 不带 key 也能通过（鉴权关闭）
        assert client.get("/api/debug/test").status_code == 200


class TestAuthMiddlewareFailClosed:
    """fail-closed：空 key / 缺失 key → 401。"""

    def test_empty_key_returns_401(self, monkeypatch):
        monkeypatch.setattr(settings, "api_keys", "key1")
        monkeypatch.setattr(settings, "api_key", None)

        client = TestClient(_build_auth_app())
        # 无任何鉴权头
        resp = client.get("/api/debug/test")
        assert resp.status_code == 401

    def test_empty_bearer_returns_401(self, monkeypatch):
        monkeypatch.setattr(settings, "api_keys", "key1")
        monkeypatch.setattr(settings, "api_key", None)

        client = TestClient(_build_auth_app())
        resp = client.get("/api/debug/test", headers={"Authorization": "Bearer "})
        assert resp.status_code == 401

    def test_public_paths_bypass_auth(self, monkeypatch):
        """PUBLIC_PATHS 中的路径免鉴权（保持原行为）。"""
        monkeypatch.setattr(settings, "api_keys", "key1")
        monkeypatch.setattr(settings, "api_key", None)

        client = TestClient(_build_auth_app())
        # /health 在 PUBLIC_PATHS 中
        # 注意：_build_auth_app 未注册 /health，但免鉴权检查发生在路由前，
        # 故应返回 404（而非 401），证明已绕过鉴权
        assert client.get("/health").status_code == 404


class TestAuthMiddlewareSignatureUnchanged:
    """Track C 硬约束：dispatch 签名必须保持 (self, request, call_next)，确保与
    ingest.py（含 L194-205 gzip 解压区）零接触面，不破坏与轨道 A/B 的并行性。"""

    def test_dispatch_signature_parameters(self):
        from app.middleware import AuthMiddleware

        sig = inspect.signature(AuthMiddleware.dispatch)
        params = list(sig.parameters.keys())
        assert params == ["self", "request", "call_next"]

    def test_dispatch_is_coroutine_function(self):
        import asyncio

        from app.middleware import AuthMiddleware

        assert asyncio.iscoroutinefunction(AuthMiddleware.dispatch)

    def test_init_takes_only_app(self):
        """__init__ 签名保持 (self, app)，setup_middleware 调用无需改动。"""
        from app.middleware import AuthMiddleware

        sig = inspect.signature(AuthMiddleware.__init__)
        params = list(sig.parameters.keys())
        assert params == ["self", "app"]
