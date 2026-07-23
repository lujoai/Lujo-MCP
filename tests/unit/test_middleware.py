"""Unit tests for rate-limiting logic in app.state.store."""

import time
from unittest.mock import MagicMock, patch


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
