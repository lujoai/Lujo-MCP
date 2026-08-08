"""单元测试：熔断器（P3-8）"""

import pytest
from unittest.mock import patch, MagicMock


class TestCircuitBreakerConfig:
    """测试熔断器配置项是否正确加载"""

    def test_config_fields_exist(self):
        """config 中存在所有熔断器配置字段"""
        from app.config import settings

        assert hasattr(settings, "circuit_breaker_enabled")
        assert hasattr(settings, "cb_llm_max_failures")
        assert hasattr(settings, "cb_llm_reset_timeout")
        assert hasattr(settings, "cb_pg_max_failures")
        assert hasattr(settings, "cb_pg_reset_timeout")

    def test_config_defaults(self):
        """默认配置值正确"""
        from app.config import settings

        assert settings.circuit_breaker_enabled is False
        assert settings.cb_llm_max_failures == 5
        assert settings.cb_llm_reset_timeout == 30
        assert settings.cb_pg_max_failures == 3
        assert settings.cb_pg_reset_timeout == 15


class TestLLMCircuitBreaker:
    """测试 LLM 熔断器"""

    def setup_method(self):
        """每个测试前重置熔断器状态"""
        from app.llm.analyzer import _analysis_cache, _llm_circuit_breaker

        _analysis_cache.clear()
        if _llm_circuit_breaker:
            _llm_circuit_breaker.close()

    def teardown_method(self):
        """每个测试后清理熔断器实例"""
        import app.llm.analyzer as analyzer_module

        if analyzer_module._llm_circuit_breaker:
            analyzer_module._llm_circuit_breaker.close()
        analyzer_module._llm_circuit_breaker = None

    @patch("app.llm.analyzer._get_client")
    def test_llm_circuit_breaker_triggers_after_max_failures(self, mock_get_client):
        """LLM 连续失败达到 fail_max 后触发熔断"""
        from app.llm.analyzer import analyze
        import pybreaker

        cb = pybreaker.CircuitBreaker(
            fail_max=2,
            reset_timeout=1,
            exclude=[pybreaker.CircuitBreakerError],
        )

        import app.llm.analyzer as analyzer_module

        analyzer_module._llm_circuit_breaker = cb

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("LLM service unavailable")
        mock_get_client.return_value = mock_client

        ctx = {"request_id": "cb-test-001", "errors": ["test error"]}

        with pytest.raises(RuntimeError):
            analyze(ctx)

        result = analyze(ctx)
        assert result["_circuit_breaker_triggered"] is True
        assert result["model"] == "__circuit_breaker_fallback__"
        assert result["analysis"]["confidence"] == "low"
        assert "熔断器已触发" in result["analysis"]["root_cause"]

    @patch("app.llm.analyzer._get_client")
    def test_llm_circuit_breaker_disabled_when_setting_off(self, mock_get_client):
        """circuit_breaker_enabled=False 时不触发熔断"""
        from app.llm.analyzer import analyze

        import app.llm.analyzer as analyzer_module

        analyzer_module._llm_circuit_breaker = None

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("LLM service unavailable")
        mock_get_client.return_value = mock_client

        ctx = {"request_id": "cb-test-002", "errors": ["test error"]}

        with pytest.raises(RuntimeError):
            analyze(ctx)

    @patch("app.llm.analyzer._get_client")
    def test_llm_circuit_breaker_fallback_has_all_fields(self, mock_get_client):
        """熔断 fallback 结果包含所有必需字段"""
        from app.llm.analyzer import analyze
        import pybreaker

        cb = pybreaker.CircuitBreaker(
            fail_max=1,
            reset_timeout=1,
            exclude=[pybreaker.CircuitBreakerError],
        )

        import app.llm.analyzer as analyzer_module

        analyzer_module._llm_circuit_breaker = cb

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("LLM service unavailable")
        mock_get_client.return_value = mock_client

        ctx = {"request_id": "cb-test-003", "errors": ["test error"]}

        result = analyze(ctx)
        assert "analysis" in result
        assert "root_cause" in result["analysis"]
        assert "impact" in result["analysis"]
        assert "fix" in result["analysis"]
        assert "confidence" in result["analysis"]
        assert "model" in result
        assert "usage" in result
        assert "attempts" in result
        assert "_circuit_breaker_triggered" in result

    @patch("app.llm.analyzer._get_client")
    def test_llm_circuit_breaker_reopens_after_close_and_reset(self, mock_get_client):
        """熔断器触发后，重置可恢复正常调用"""
        from app.llm.analyzer import analyze
        import pybreaker

        cb = pybreaker.CircuitBreaker(
            fail_max=1,
            reset_timeout=60,
            exclude=[pybreaker.CircuitBreakerError],
        )

        import app.llm.analyzer as analyzer_module

        analyzer_module._llm_circuit_breaker = cb

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("LLM service unavailable")
        mock_get_client.return_value = mock_client

        ctx = {"request_id": "cb-test-004", "errors": ["test error"]}

        result = analyze(ctx)
        assert result["_circuit_breaker_triggered"] is True

        analyzer_module._llm_circuit_breaker = None

        mock_client.chat.completions.create.side_effect = RuntimeError("LLM service unavailable")

        with pytest.raises(RuntimeError):
            analyze(ctx)


class TestPGCircuitBreaker:
    """测试 PG 熔断器"""

    def teardown_method(self):
        """每个测试后清理熔断器实例"""
        import app.runtime.core.storage.pg_store as pg_store_module

        if pg_store_module._pg_circuit_breaker:
            pg_store_module._pg_circuit_breaker.close()
        pg_store_module._pg_circuit_breaker = None

    @patch("app.runtime.core.storage.pg_store._get_pool")
    def test_pg_circuit_breaker_triggers_after_max_failures(self, mock_get_pool):
        """PG 连续失败达到 fail_max 后触发熔断"""
        from app.runtime.core.storage.pg_store import _execute_with_retry
        import pybreaker
        import psycopg2

        cb = pybreaker.CircuitBreaker(
            fail_max=2,
            reset_timeout=1,
            exclude=[pybreaker.CircuitBreakerError],
        )

        import app.runtime.core.storage.pg_store as pg_store_module

        pg_store_module._pg_circuit_breaker = cb

        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        mock_get_pool.return_value = mock_pool

        mock_conn.cursor.return_value.execute.side_effect = psycopg2.OperationalError("PG connection failed")

        with pytest.raises(psycopg2.OperationalError):
            _execute_with_retry(mock_conn, "SELECT 1", max_retries=0)

        with pytest.raises(pybreaker.CircuitBreakerError):
            _execute_with_retry(mock_conn, "SELECT 1", max_retries=0)

    @patch("app.runtime.core.storage.pg_store._get_pool")
    @patch("app.runtime.core.storage.pg_store._get_pg_circuit_breaker")
    def test_pg_circuit_breaker_disabled_when_setting_off(self, mock_get_cb, mock_get_pool):
        """circuit_breaker_enabled=False 时不触发熔断"""
        from app.runtime.core.storage.pg_store import _execute_with_retry
        import psycopg2

        mock_get_cb.return_value = None

        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        mock_get_pool.return_value = mock_pool

        mock_conn.cursor.return_value.execute.side_effect = psycopg2.OperationalError("PG connection failed")

        for _ in range(5):
            with pytest.raises(psycopg2.OperationalError):
                _execute_with_retry(mock_conn, "SELECT 1", max_retries=0)

    @patch("app.runtime.core.storage.pg_store._get_pool")
    def test_pg_circuit_breaker_protects_queries(self, mock_get_pool):
        """PG 查询方法受熔断器保护"""
        from app.runtime.core.storage.pg_store import _query_with_retry
        import pybreaker
        import psycopg2

        cb = pybreaker.CircuitBreaker(
            fail_max=1,
            reset_timeout=1,
            exclude=[pybreaker.CircuitBreakerError],
        )

        import app.runtime.core.storage.pg_store as pg_store_module

        pg_store_module._pg_circuit_breaker = cb

        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        mock_get_pool.return_value = mock_pool

        mock_conn.cursor.return_value.execute.side_effect = psycopg2.OperationalError("PG query failed")

        with pytest.raises(pybreaker.CircuitBreakerError):
            _query_with_retry(mock_conn, "SELECT 1")


class TestCircuitBreakerDisabledWhenPybreakerMissing:
    """测试 pybreaker 未安装时熔断器功能被禁用"""

    @patch.dict("sys.modules", {"pybreaker": None})
    def test_llm_circuit_breaker_none_when_pybreaker_missing(self):
        """pybreaker 未安装时 LLM 熔断器为 None"""
        import importlib
        from app.llm import analyzer

        importlib.reload(analyzer)
        assert analyzer._llm_circuit_breaker is None

    @patch.dict("sys.modules", {"pybreaker": None})
    def test_pg_circuit_breaker_none_when_pybreaker_missing(self):
        """pybreaker 未安装时 PG 熔断器为 None"""
        import importlib
        from app.runtime.core.storage import pg_store

        importlib.reload(pg_store)
        assert pg_store._pg_circuit_breaker is None