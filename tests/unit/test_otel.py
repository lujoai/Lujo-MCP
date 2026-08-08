"""单元测试：OpenTelemetry（P3-4）"""

from unittest.mock import patch, MagicMock


class TestOtelConfig:
    """测试 OTel 配置项"""

    def test_config_fields_exist(self):
        """config 中存在所有 OTel 配置字段"""
        from app.config import settings

        assert hasattr(settings, "otel_enabled")
        assert hasattr(settings, "otel_service_name")
        assert hasattr(settings, "otel_exporter_endpoint")
        assert hasattr(settings, "otel_metrics_interval_ms")

    def test_config_defaults(self):
        """默认配置值正确"""
        from app.config import settings

        assert settings.otel_enabled is False
        assert settings.otel_service_name == "ai-debug-mcp"
        assert settings.otel_exporter_endpoint == ""
        assert settings.otel_metrics_interval_ms == 60000


class TestOtelInitialization:
    """测试 OTel 初始化"""

    def teardown_method(self):
        """每个测试后重置 OTel 全局状态"""
        import app.observability as obs_module

        obs_module._otel_meter = None
        obs_module._otel_request_counter = None
        obs_module._otel_error_counter = None
        obs_module._otel_latency_histogram = None
        obs_module._otel_shutdown = None

    def test_otel_init_disabled_when_setting_off(self, monkeypatch):
        """otel_enabled=False 时不初始化 OTel"""
        from app.observability import _init_otel

        monkeypatch.setattr("app.config.settings.otel_enabled", False)

        meter, req_counter, err_counter, latency_hist, shutdown = _init_otel()

        assert meter is None
        assert req_counter is None
        assert err_counter is None
        assert latency_hist is None
        assert shutdown is None

    def test_otel_init_enabled_when_setting_on(self, monkeypatch):
        """otel_enabled=True 时初始化 OTel（mock OTel SDK）"""
        from app.observability import _init_otel

        monkeypatch.setattr("app.config.settings.otel_enabled", True)
        monkeypatch.setattr("app.config.settings.otel_service_name", "test-service")
        monkeypatch.setattr("app.config.settings.otel_exporter_endpoint", "")
        monkeypatch.setattr("app.config.settings.otel_metrics_interval_ms", 10000)

        mock_counter1 = MagicMock()
        mock_counter2 = MagicMock()
        mock_histogram = MagicMock()
        mock_meter = MagicMock()
        mock_meter.create_counter.side_effect = [mock_counter1, mock_counter2]
        mock_meter.create_histogram.return_value = mock_histogram

        mock_provider = MagicMock()
        mock_provider.get_meter.return_value = mock_meter

        with patch("opentelemetry.sdk.metrics.MeterProvider", return_value=mock_provider), \
             patch("opentelemetry.metrics.set_meter_provider"), \
             patch("opentelemetry.exporter.otlp.proto.grpc.metric_exporter.OTLPMetricExporter"), \
             patch("opentelemetry.sdk.metrics.export.PeriodicExportingMetricReader"), \
             patch("opentelemetry.sdk.resources.Resource"):

            meter, req_counter, err_counter, latency_hist, shutdown = _init_otel()

        assert meter is not None
        assert req_counter is not None
        assert err_counter is not None
        assert latency_hist is not None
        assert shutdown is not None

    def test_otel_init_failure_degrades_gracefully(self, monkeypatch):
        """OTel 初始化失败时降级为仅 Prometheus 文本端点"""
        from app.observability import _init_otel

        monkeypatch.setattr("app.config.settings.otel_enabled", True)

        with patch("app.observability.MeterProvider", side_effect=RuntimeError("OTel init failed")):
            meter, req_counter, err_counter, latency_hist, shutdown = _init_otel()

        assert meter is None
        assert req_counter is None
        assert err_counter is None
        assert latency_hist is None
        assert shutdown is None

    def test_otel_init_is_idempotent(self, monkeypatch):
        """OTel 初始化是幂等的"""
        from app.observability import _init_otel

        monkeypatch.setattr("app.config.settings.otel_enabled", True)

        mock_counter1 = MagicMock()
        mock_counter2 = MagicMock()
        mock_histogram = MagicMock()
        mock_meter = MagicMock()
        mock_meter.create_counter.side_effect = [mock_counter1, mock_counter2]
        mock_meter.create_histogram.return_value = mock_histogram

        mock_provider = MagicMock()
        mock_provider.get_meter.return_value = mock_meter

        with patch("opentelemetry.sdk.metrics.MeterProvider", return_value=mock_provider), \
             patch("opentelemetry.metrics.set_meter_provider"), \
             patch("opentelemetry.exporter.otlp.proto.grpc.metric_exporter.OTLPMetricExporter"), \
             patch("opentelemetry.sdk.metrics.export.PeriodicExportingMetricReader"), \
             patch("opentelemetry.sdk.resources.Resource"):

            result1 = _init_otel()
            result2 = _init_otel()

        assert result1 == result2


class TestMetricsMiddlewareOtelIntegration:
    """测试 MetricsMiddleware 与 OTel 的集成"""

    def teardown_method(self):
        """每个测试后重置指标存储和 OTel 状态"""
        import app.observability as obs_module

        obs_module._request_total.clear()
        obs_module._error_total.clear()
        obs_module._latency_sum.clear()
        obs_module._latency_count.clear()
        obs_module._otel_meter = None
        obs_module._otel_request_counter = None
        obs_module._otel_error_counter = None
        obs_module._otel_latency_histogram = None
        obs_module._otel_shutdown = None

    def test_middleware_records_to_both_stores_when_otel_enabled(self, monkeypatch):
        """OTel 启用时，指标同时写入内存存储和 OTel instruments"""
        from app.observability import MetricsMiddleware

        monkeypatch.setattr("app.config.settings.otel_enabled", True)

        mock_request_counter = MagicMock()
        mock_error_counter = MagicMock()
        mock_latency_histogram = MagicMock()

        with patch("app.observability._init_otel", return_value=(MagicMock(), mock_request_counter, mock_error_counter, mock_latency_histogram, MagicMock())):
            mock_response = MagicMock()
            mock_response.status_code = 200

            async def mock_call_next(req):
                return mock_response

            middleware = MetricsMiddleware(MagicMock())

            import asyncio
            asyncio.run(middleware.dispatch(MagicMock(method="GET", scope={"route": MagicMock(path="/test")}), mock_call_next))

            import app.observability as obs_module
            assert obs_module._request_total[("GET", "/test", 200)] == 1
            mock_request_counter.add.assert_called_once_with(1, {"method": "GET", "path": "/test", "status": "200"})
            mock_error_counter.add.assert_not_called()
            mock_latency_histogram.record.assert_called_once()

    def test_middleware_records_5xx_errors_to_error_counter(self, monkeypatch):
        """5xx 错误同时记录到 error_total 和 OTel error_counter"""
        from app.observability import MetricsMiddleware

        monkeypatch.setattr("app.config.settings.otel_enabled", True)

        mock_request_counter = MagicMock()
        mock_error_counter = MagicMock()
        mock_latency_histogram = MagicMock()

        with patch("app.observability._init_otel", return_value=(MagicMock(), mock_request_counter, mock_error_counter, mock_latency_histogram, MagicMock())):
            mock_response = MagicMock()
            mock_response.status_code = 500

            async def mock_call_next(req):
                return mock_response

            middleware = MetricsMiddleware(MagicMock())

            import asyncio
            asyncio.run(middleware.dispatch(MagicMock(method="POST", scope={"route": MagicMock(path="/api/debug")}), mock_call_next))

            import app.observability as obs_module
            assert obs_module._request_total[("POST", "/api/debug", 500)] == 1
            assert obs_module._error_total[("POST", "/api/debug")] == 1
            mock_request_counter.add.assert_called_once_with(1, {"method": "POST", "path": "/api/debug", "status": "500"})
            mock_error_counter.add.assert_called_once_with(1, {"method": "POST", "path": "/api/debug"})

    def test_middleware_works_without_otel(self, monkeypatch):
        """OTel 禁用时，仅写入内存存储"""
        from app.observability import MetricsMiddleware

        monkeypatch.setattr("app.config.settings.otel_enabled", False)

        mock_response = MagicMock()
        mock_response.status_code = 200

        async def mock_call_next(req):
            return mock_response

        middleware = MetricsMiddleware(MagicMock())

        import asyncio
        asyncio.run(middleware.dispatch(MagicMock(method="GET", scope={"route": MagicMock(path="/test")}), mock_call_next))

        import app.observability as obs_module
        assert obs_module._request_total[("GET", "/test", 200)] == 1


class TestShutdownObservability:
    """测试 shutdown_observability"""

    def test_shutdown_calls_otel_shutdown(self):
        """shutdown_observability 调用 OTel shutdown"""
        import app.observability as obs_module

        mock_shutdown = MagicMock()
        obs_module._otel_shutdown = mock_shutdown

        from app.observability import shutdown_observability
        shutdown_observability()

        mock_shutdown.assert_called_once()

    def test_shutdown_no_op_when_otel_not_initialized(self):
        """OTel 未初始化时 shutdown_observability 不报错"""
        import app.observability as obs_module
        obs_module._otel_shutdown = None

        from app.observability import shutdown_observability
        shutdown_observability()


class TestPrometheusEndpointBackwardCompat:
    """测试 /metrics 端点向后兼容性"""

    def test_prometheus_endpoint_format(self):
        """/metrics 返回正确的 Prometheus 文本格式"""
        import app.observability as obs_module

        obs_module._request_total[("GET", "/test", 200)] = 5
        obs_module._error_total[("POST", "/api",)] = 2
        obs_module._latency_sum["/test"] = 10.5
        obs_module._latency_count["/test"] = 5

        result = obs_module._render_prometheus()

        assert "http_requests_total" in result
        assert 'method="GET"' in result
        assert 'path="/test"' in result
        assert 'status="200"' in result
        assert "http_errors_total" in result
        assert "http_request_duration_seconds_sum" in result
        assert "http_request_duration_seconds_count" in result


class TestMetricCardinalityBounds:
    """FIX: P1-10b 指标 key 无界 —— 未命中路由归一化 + 上限裁剪"""

    def teardown_method(self):
        import app.observability as obs_module

        obs_module._request_total.clear()
        obs_module._error_total.clear()
        obs_module._latency_sum.clear()
        obs_module._latency_count.clear()

    def test_unmatched_route_normalized_to_404_other(self, monkeypatch):
        """未命中已注册路由（scope 无 route）时 path 归一化为 404-other"""
        from app.observability import MetricsMiddleware

        mock_response = MagicMock()
        mock_response.status_code = 404

        async def mock_call_next(req):
            return mock_response

        middleware = MetricsMiddleware(MagicMock())
        import asyncio
        asyncio.run(
            middleware.dispatch(
                # scope 无 route 键 → 模拟未命中路由的原始请求
                MagicMock(method="GET", scope={}),
                mock_call_next,
            )
        )

        import app.observability as obs_module
        # 高基数动态路径不会进指标表，统一为 404-other
        assert ("GET", "404-other", 404) in obs_module._request_total
        assert "404-other" in obs_module._latency_sum

    def test_metric_tables_trimmed_over_limit(self, monkeypatch):
        """指标表超上限时清空重置，防止高基数 key 撑爆内存"""
        import app.observability as obs_module

        # 直接把表填到超限（须持锁，与生产写路径一致）
        limit = obs_module._MAX_METRIC_KEYS
        with obs_module._counter_lock:
            for i in range(limit + 10):
                obs_module._request_total[("GET", f"/path/{i}", 200)] = 1

        with obs_module._counter_lock:
            obs_module._trim_metric_tables_if_needed()

        assert len(obs_module._request_total) == 0
