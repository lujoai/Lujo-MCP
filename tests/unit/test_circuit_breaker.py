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
        from app.llm.cache import _analysis_cache
        from app.llm.analyzer import _llm_circuit_breaker

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
        import app.runtime.core.storage.pg_executor as pg_store_module

        if pg_store_module._pg_circuit_breaker:
            pg_store_module._pg_circuit_breaker.close()
        pg_store_module._pg_circuit_breaker = None

    @patch("app.runtime.core.storage.pg_executor._get_pool")
    def test_pg_circuit_breaker_triggers_after_max_failures(self, mock_get_pool):
        """PG 连续失败达到 fail_max 后触发熔断"""
        from app.runtime.core.storage.pg_executor import _execute_with_retry
        import pybreaker
        import psycopg2

        cb = pybreaker.CircuitBreaker(
            fail_max=2,
            reset_timeout=1,
            exclude=[pybreaker.CircuitBreakerError],
        )

        import app.runtime.core.storage.pg_executor as pg_store_module

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

    @patch("app.runtime.core.storage.pg_executor._get_pool")
    @patch("app.runtime.core.storage.pg_executor._get_pg_circuit_breaker")
    def test_pg_circuit_breaker_disabled_when_setting_off(self, mock_get_cb, mock_get_pool):
        """circuit_breaker_enabled=False 时不触发熔断"""
        from app.runtime.core.storage.pg_executor import _execute_with_retry
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

    @patch("app.runtime.core.storage.pg_executor._get_pool")
    def test_pg_circuit_breaker_protects_queries(self, mock_get_pool):
        """PG 查询方法受熔断器保护"""
        from app.runtime.core.storage.pg_executor import _query_with_retry
        import pybreaker
        import psycopg2

        cb = pybreaker.CircuitBreaker(
            fail_max=1,
            reset_timeout=1,
            exclude=[pybreaker.CircuitBreakerError],
        )

        import app.runtime.core.storage.pg_executor as pg_store_module

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

    def test_llm_circuit_breaker_none_when_pybreaker_missing(self):
        """pybreaker 未安装时 LLM 熔断器为 None"""
        import importlib
        from app.llm import analyzer

        with patch.dict("sys.modules", {"pybreaker": None}):
            importlib.reload(analyzer)
            assert analyzer._llm_circuit_breaker is None
        # 恢复：以真实 pybreaker 重新加载，避免污染后续用例的模块级 pybreaker=None
        importlib.reload(analyzer)

    def test_pg_circuit_breaker_none_when_pybreaker_missing(self):
        """pybreaker 未安装时 PG 熔断器为 None"""
        import importlib
        from app.runtime.core.storage import pg_executor

        with patch.dict("sys.modules", {"pybreaker": None}):
            importlib.reload(pg_executor)
            assert pg_executor._pg_circuit_breaker is None
        importlib.reload(pg_executor)


class TestLLMCircuitBreakerAsync:
    """测试 analyze_async（原生 asyncio）经 _call_async_through_breaker 的熔断保护。

    v0.6.1：熔断开启时不再退回 to_thread 同步客户端，而是手动驱动 pybreaker 状态机
    包住 ``_retry_call_async``，语义与同步 ``analyze`` 一致。
    """

    def setup_method(self):
        from app.llm.cache import _analysis_cache
        from app.llm.analyzer import _llm_circuit_breaker

        _analysis_cache.clear()
        if _llm_circuit_breaker:
            _llm_circuit_breaker.close()

    def teardown_method(self):
        import app.llm.analyzer as analyzer_module

        if analyzer_module._llm_circuit_breaker:
            analyzer_module._llm_circuit_breaker.close()
        analyzer_module._llm_circuit_breaker = None

    @pytest.mark.asyncio
    @patch("app.llm.analyzer._get_knowledge_base_result", return_value=None)
    @patch("app.llm.analyzer._get_cached_result", return_value=None)
    @patch("app.llm.analyzer._get_async_client")
    async def test_async_triggers_fallback_after_max_failures(
        self, mock_get_async_client, mock_cache, mock_kb
    ):
        """异步路径连续失败达 fail_max 后触发熔断，且走的是 AsyncOpenAI。"""
        from unittest.mock import AsyncMock

        import pybreaker
        from app.llm.analyzer import analyze_async
        import app.llm.analyzer as analyzer_module

        cb = pybreaker.CircuitBreaker(
            fail_max=2,
            reset_timeout=1,
            exclude=[pybreaker.CircuitBreakerError],
        )
        analyzer_module._llm_circuit_breaker = cb

        async_client = MagicMock()
        async_client.chat.completions.create = AsyncMock(
            side_effect=RuntimeError("LLM service unavailable")
        )
        mock_get_async_client.return_value = async_client

        ctx = {"request_id": "cb-async-001", "errors": ["test error"]}

        # 第一次：未达阈值 → 原异常抛出
        with pytest.raises(RuntimeError):
            await analyze_async(ctx)

        # 第二次：达阈值 → 熔断 → fallback
        result = await analyze_async(ctx)
        assert result["_circuit_breaker_triggered"] is True
        assert result["model"] == "__circuit_breaker_fallback__"
        assert result["analysis"]["confidence"] == "low"
        # 熔断路径确实用的是异步客户端（两次尝试都 await 到 create）
        assert async_client.chat.completions.create.await_count == 2

    @pytest.mark.asyncio
    @patch("app.llm.analyzer._get_knowledge_base_result", return_value=None)
    @patch("app.llm.analyzer._get_cached_result", return_value=None)
    @patch("app.llm.analyzer._get_async_client")
    async def test_async_open_returns_fallback_without_call(
        self, mock_get_async_client, mock_cache, mock_kb
    ):
        """OPEN 且未到重置时间 → 直接 fallback，不调用 AsyncOpenAI。"""
        from unittest.mock import AsyncMock

        import pybreaker
        from app.llm.analyzer import analyze_async
        import app.llm.analyzer as analyzer_module

        cb = pybreaker.CircuitBreaker(
            fail_max=1,
            reset_timeout=60,
            exclude=[pybreaker.CircuitBreakerError],
        )
        analyzer_module._llm_circuit_breaker = cb

        async_client = MagicMock()
        async_client.chat.completions.create = AsyncMock(return_value=MagicMock())
        mock_get_async_client.return_value = async_client

        cb.open()  # 直接打开熔断

        ctx = {"request_id": "cb-async-002", "errors": ["test error"]}
        result = await analyze_async(ctx)
        assert result["_circuit_breaker_triggered"] is True
        async_client.chat.completions.create.assert_not_awaited()


class TestLLMCircuitBreakerStream:
    """测试流式分析路径纳入同一熔断状态机（v0.6.7 流式绕熔断修复）。

    OPEN 时流式路径应与非流式一样 fallback，不再直打 LLM；
    流式成功/失败同样计入熔断计数。
    """

    def setup_method(self):
        from app.llm.analyzer import _llm_circuit_breaker
        from app.llm.cache import _analysis_cache

        _analysis_cache.clear()
        if _llm_circuit_breaker:
            _llm_circuit_breaker.close()

    def teardown_method(self):
        import app.llm.analyzer as analyzer_module

        if analyzer_module._llm_circuit_breaker:
            analyzer_module._llm_circuit_breaker.close()
        analyzer_module._llm_circuit_breaker = None

    @patch("app.llm.analyzer._get_client")
    def test_stream_returns_fallback_when_breaker_open(self, mock_get_client):
        """熔断 OPEN 时同步流式路径 yield fallback，且不调用 LLM。"""
        import json

        import pybreaker

        import app.llm.analyzer as analyzer_module
        from app.llm.analyzer import analyze_stream

        cb = pybreaker.CircuitBreaker(
            fail_max=1, reset_timeout=60, exclude=[pybreaker.CircuitBreakerError]
        )
        analyzer_module._llm_circuit_breaker = cb
        cb.open()

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        ctx = {"request_id": "cb-stream-001", "errors": ["test error"]}

        chunks = list(analyze_stream(ctx))
        assert len(chunks) == 1
        fallback = json.loads(chunks[0])
        assert fallback["_circuit_breaker_triggered"] is True
        assert fallback["model"] == "__circuit_breaker_fallback__"
        mock_client.chat.completions.create.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.llm.analyzer._get_async_client")
    async def test_stream_async_returns_fallback_when_breaker_open(self, mock_get_async_client):
        """熔断 OPEN 时异步流式路径 yield fallback，且不调用 AsyncOpenAI。"""
        import json

        import pybreaker

        import app.llm.analyzer as analyzer_module
        from app.llm.analyzer import analyze_stream_async

        cb = pybreaker.CircuitBreaker(
            fail_max=1, reset_timeout=60, exclude=[pybreaker.CircuitBreakerError]
        )
        analyzer_module._llm_circuit_breaker = cb
        cb.open()

        mock_client = MagicMock()
        mock_get_async_client.return_value = mock_client

        ctx = {"request_id": "cb-stream-002", "errors": ["test error"]}

        chunks = []
        async for chunk in analyze_stream_async(ctx):
            chunks.append(chunk)
        assert len(chunks) == 1
        fallback = json.loads(chunks[0])
        assert fallback["_circuit_breaker_triggered"] is True
        mock_client.chat.completions.create.assert_not_called()

    @patch("app.llm.analyzer._get_client")
    def test_stream_failure_trips_breaker(self, mock_get_client):
        """流式调用失败应计入熔断计数：达阈值后转为 open 并 yield fallback。"""
        import json

        import pybreaker

        import app.llm.analyzer as analyzer_module
        from app.llm.analyzer import analyze_stream

        cb = pybreaker.CircuitBreaker(
            fail_max=1, reset_timeout=60, exclude=[pybreaker.CircuitBreakerError]
        )
        analyzer_module._llm_circuit_breaker = cb

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("LLM down")
        mock_get_client.return_value = mock_client

        ctx = {"request_id": "cb-stream-003", "errors": ["test error"]}

        chunks = list(analyze_stream(ctx))
        assert len(chunks) == 1
        fallback = json.loads(chunks[0])
        assert fallback["_circuit_breaker_triggered"] is True
        assert cb.current_state == pybreaker.STATE_OPEN

