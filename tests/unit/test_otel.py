"""单元测试：OpenTelemetry（P3-4）"""

from unittest.mock import patch, MagicMock

import pytest


def _reset_otel_globals(module) -> None:
    """关闭并清空 module 中全部 ``_otel_*`` 模块级全局。

    程序化遍历而非逐个手写：app/observability.py 的 OTel 面现有 18 个全局
    （11 counter + 5 histogram + meter + shutdown），手写清单漏项会让残留的
    MagicMock instrument 静默顶替全局，从而在进程内破坏「OTel 关闭即不做
    OTel 双写」的不变量，且行为随测试执行顺序漂移。

    匹配用 ``str.startswith("_otel_")``（小写、大小写敏感）：``_OTEL_AVAILABLE``
    是全大写常量，天然不被匹配；不要改成大小写不敏感匹配，否则会把「SDK 是否
    可用」这一环境事实一并清掉，让降级路径的断言失去意义。
    """
    # 先关再清：若某个测试意外建出真实 provider，置 None 前不调用 shutdown
    # 会把已启动的导出线程孤儿化；关闭本身失败也不能让 teardown 自己抛异常
    shutdown = getattr(module, "_otel_shutdown", None)
    if shutdown is not None:
        try:
            shutdown()
        except Exception:
            pass

    # 先对 vars(module) 取名字快照再遍历，避免边迭代模块字典边改属性
    names = [name for name in vars(module) if name.startswith("_otel_")]
    for name in names:
        setattr(module, name, None)


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
        assert settings.otel_service_name == "lujo-mcp"
        assert settings.otel_exporter_endpoint == ""
        assert settings.otel_metrics_interval_ms == 60000


class TestOtelInitialization:
    """测试 OTel 初始化"""

    def teardown_method(self):
        """每个测试后重置 OTel 全局状态（含 v0.6.0+ 扩展 instrument）"""
        import app.observability as obs_module

        _reset_otel_globals(obs_module)

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
        """otel_enabled=True 时初始化 OTel（mock OTel SDK，并断言构造参数）"""
        from app.observability import _init_otel

        monkeypatch.setattr("app.config.settings.otel_enabled", True)
        monkeypatch.setattr("app.config.settings.otel_service_name", "test-service")
        monkeypatch.setattr("app.config.settings.otel_exporter_endpoint", "")
        monkeypatch.setattr("app.config.settings.otel_metrics_interval_ms", 10000)

        mock_counter1 = MagicMock()
        mock_counter2 = MagicMock()
        mock_histogram = MagicMock()
        mock_meter = MagicMock()
        # _init_otel 会创建 11 个 counter / 5 个 histogram（含 v0.6.0+ 扩展面），
        # 前几个固定 mock 以断言返回值对应关系，其余补足避免 StopIteration
        mock_meter.create_counter.side_effect = (
            [mock_counter1, mock_counter2] + [MagicMock() for _ in range(9)]
        )
        mock_meter.create_histogram.side_effect = (
            [mock_histogram] + [MagicMock() for _ in range(4)]
        )

        mock_provider = MagicMock()
        mock_provider.get_meter.return_value = mock_meter

        # set_meter_provider 走"模块对象 + 运行时属性查找"，patch 上游有效；
        # 其余四个在 app.observability 里是 from-import 的直接引用，
        # 必须 patch app.observability.* 才能命中，patch 上游完全无效
        with patch("app.observability.MeterProvider", return_value=mock_provider) as mock_provider_cls, \
             patch("opentelemetry.metrics.set_meter_provider"), \
             patch("app.observability.OTLPMetricExporter") as mock_exporter_cls, \
             patch("app.observability.PeriodicExportingMetricReader") as mock_reader_cls, \
             patch("app.observability.Resource") as mock_resource_cls:

            meter, req_counter, err_counter, latency_hist, shutdown = _init_otel()

        assert meter is not None
        assert req_counter is not None
        assert err_counter is not None
        assert latency_hist is not None
        assert shutdown is not None
        # 返回值与 mock 的对应关系成立（而非仅仅"不是 None"）
        assert meter is mock_meter
        assert req_counter is mock_counter1
        assert err_counter is mock_counter2
        assert latency_hist is mock_histogram
        mock_provider.get_meter.assert_called_once_with("lujo-mcp")

        # 构造参数：endpoint 为空走无参构造分支；interval 用测试注入的 10000
        mock_exporter_cls.assert_called_once_with()
        mock_reader_cls.assert_called_once_with(
            mock_exporter_cls.return_value, export_interval_millis=10000
        )
        mock_provider_cls.assert_called_once_with(
            resource=mock_resource_cls.return_value,
            metric_readers=[mock_reader_cls.return_value],
        )
        shutdown()
        mock_provider.shutdown.assert_called_once()

    def test_otel_init_passes_endpoint_when_configured(self, monkeypatch):
        """otel_exporter_endpoint 非空时必须以 endpoint= 关键字传给 exporter。

        9999 是刻意选的、本机不会有服务的端口；exporter 已被 mock，不会真连。
        """
        from app.observability import _init_otel

        monkeypatch.setattr("app.config.settings.otel_enabled", True)
        monkeypatch.setattr(
            "app.config.settings.otel_exporter_endpoint", "http://127.0.0.1:9999"
        )

        mock_meter = MagicMock()
        mock_meter.create_counter.side_effect = [MagicMock() for _ in range(11)]
        mock_meter.create_histogram.side_effect = [MagicMock() for _ in range(5)]
        mock_provider = MagicMock()
        mock_provider.get_meter.return_value = mock_meter

        with patch("app.observability.MeterProvider", return_value=mock_provider), \
             patch("opentelemetry.metrics.set_meter_provider"), \
             patch("app.observability.OTLPMetricExporter") as mock_exporter_cls, \
             patch("app.observability.PeriodicExportingMetricReader"), \
             patch("app.observability.Resource"):
            result = _init_otel()

        assert result[0] is mock_meter
        mock_exporter_cls.assert_called_once_with(endpoint="http://127.0.0.1:9999")

    def test_otel_init_failure_degrades_gracefully(self, monkeypatch):
        """OTel 初始化失败时降级为仅 Prometheus 文本端点"""
        from app.observability import _init_otel

        monkeypatch.setattr("app.config.settings.otel_enabled", True)

        # 只让 MeterProvider 注入失败；exporter/reader/Resource 同样要 mock，
        # 否则注入失败点之前（exporter → reader → provider）会先建出真实
        # exporter 与已启动导出线程的 reader
        with patch("app.observability.MeterProvider", side_effect=RuntimeError("OTel init failed")), \
             patch("app.observability.PeriodicExportingMetricReader"), \
             patch("app.observability.OTLPMetricExporter"), \
             patch("app.observability.Resource"):
            meter, req_counter, err_counter, latency_hist, shutdown = _init_otel()

        assert meter is None
        assert req_counter is None
        assert err_counter is None
        assert latency_hist is None
        assert shutdown is None

    def test_otel_init_failure_shuts_down_created_reader(self, monkeypatch):
        """provider 注入失败时，已创建并启动导出线程的 reader 必须被显式关闭。

        _init_otel 的构造顺序是 exporter → reader → provider：在 provider 处
        抛异常时 reader 的周期导出线程已经启动，降级路径若不调用其 shutdown，
        该线程会永久重试导出（孤儿线程 + 持续向 endpoint 发起真实连接）。
        """
        from app.observability import _init_otel

        monkeypatch.setattr("app.config.settings.otel_enabled", True)

        mock_reader_cls = MagicMock()
        with patch("app.observability.MeterProvider", side_effect=RuntimeError("OTel init failed")), \
             patch("app.observability.PeriodicExportingMetricReader", mock_reader_cls), \
             patch("app.observability.OTLPMetricExporter"), \
             patch("app.observability.Resource"):
            result = _init_otel()

        assert result == (None, None, None, None, None)
        mock_reader_cls.return_value.shutdown.assert_called()

    def test_otel_init_is_idempotent(self, monkeypatch):
        """OTel 初始化是幂等的"""
        from app.observability import _init_otel

        monkeypatch.setattr("app.config.settings.otel_enabled", True)

        mock_meter = MagicMock()
        mock_meter.create_counter.side_effect = [MagicMock() for _ in range(11)]
        mock_meter.create_histogram.side_effect = [MagicMock() for _ in range(5)]
        mock_provider = MagicMock()
        mock_provider.get_meter.return_value = mock_meter

        # 靶点说明同 test_otel_init_enabled_when_setting_on：除
        # set_meter_provider（模块属性查找）外必须 patch app.observability.*
        with patch("app.observability.MeterProvider", return_value=mock_provider) as mock_provider_cls, \
             patch("opentelemetry.metrics.set_meter_provider"), \
             patch("app.observability.OTLPMetricExporter"), \
             patch("app.observability.PeriodicExportingMetricReader"), \
             patch("app.observability.Resource"):

            result1 = _init_otel()
            result2 = _init_otel()

        assert result1 == result2
        # 第二次走 _otel_meter 缓存早返回，provider 只能被构造一次
        mock_provider_cls.assert_called_once()


class TestMetricsMiddlewareOtelIntegration:
    """测试 MetricsMiddleware 与 OTel 的集成"""

    def teardown_method(self):
        """每个测试后重置指标存储和 OTel 状态"""
        import app.observability as obs_module

        obs_module._request_total.clear()
        obs_module._error_total.clear()
        obs_module._latency_sum.clear()
        obs_module._latency_count.clear()
        _reset_otel_globals(obs_module)

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

    def teardown_method(self):
        """每个测试后重置 OTel 全局状态：本类用例会把 _otel_shutdown 置为
        MagicMock / None，不清理则子集运行（-k）或随机排序下残留会外溢"""
        import app.observability as obs_module

        _reset_otel_globals(obs_module)

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


class TestMetricsAuthExemption:
    """P2-F2：/metrics 在全局 AuthMiddleware 的豁免与 METRICS_AUTH_ENABLED 解耦。

    修复生产强制 API_KEY 下 Prometheus 抓 /metrics 恒 401、监控链路静默失效的问题：
    - METRICS_AUTH_ENABLED=False：/metrics 在全局中间件豁免（供监控栈无凭据抓取）
    - METRICS_AUTH_ENABLED=True：/metrics 保留全局中间件保护（端点层还会再校验）
    """

    @staticmethod
    def _make_request(path: str):
        from starlette.requests import Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
            "scheme": "http",
            "state": {},
        }
        return Request(scope)

    @staticmethod
    def _run(dispatch, req, call_next):
        import asyncio
        return asyncio.run(dispatch(req, call_next))

    def test_metrics_exempt_when_metrics_auth_disabled(self, monkeypatch):
        """metrics_auth_enabled=False 且鉴权开启时，/metrics 直接放行（call_next 被调用）。"""
        from app.config import settings
        from app.middleware import AuthMiddleware
        from starlette.responses import JSONResponse

        monkeypatch.setattr(settings, "metrics_auth_enabled", False)
        mw = AuthMiddleware.__new__(AuthMiddleware)
        mw.enabled = True

        async def call_next(req):
            return JSONResponse(content={"ok": True})

        resp = self._run(mw.dispatch, self._make_request("/metrics"), call_next)
        assert resp.status_code == 200

    def test_metrics_requires_key_when_metrics_auth_enabled(self, monkeypatch):
        """metrics_auth_enabled=True 时 /metrics 不豁免，无 key 请求被 401 拒绝。"""
        from app.config import settings
        from app.middleware import AuthMiddleware
        from starlette.responses import JSONResponse

        monkeypatch.setattr(settings, "metrics_auth_enabled", True)
        mw = AuthMiddleware.__new__(AuthMiddleware)
        mw.enabled = True

        async def call_next(req):
            return JSONResponse(content={"never": True})

        resp = self._run(mw.dispatch, self._make_request("/metrics"), call_next)
        assert resp.status_code == 401

    def test_non_metrics_still_auth_required(self, monkeypatch):
        """豁免仅限 /metrics，其余路径在无 key 时仍被 401 拒绝（不扩大放行面）。"""
        from app.config import settings
        from app.middleware import AuthMiddleware
        from starlette.responses import JSONResponse

        monkeypatch.setattr(settings, "metrics_auth_enabled", False)
        mw = AuthMiddleware.__new__(AuthMiddleware)
        mw.enabled = True

        async def call_next(req):
            return JSONResponse(content={"ok": True})

        resp = self._run(mw.dispatch, self._make_request("/api/debug/analyze"), call_next)
        assert resp.status_code == 401

    def test_metrics_exempt_when_auth_disabled(self, monkeypatch):
        """鉴权本身关闭时 /metrics 同样放行（不因豁免逻辑而回退到鉴权）。"""
        from app.config import settings
        from app.middleware import AuthMiddleware
        from starlette.responses import JSONResponse

        monkeypatch.setattr(settings, "metrics_auth_enabled", False)
        mw = AuthMiddleware.__new__(AuthMiddleware)
        mw.enabled = False  # auth 关闭

        async def call_next(req):
            return JSONResponse(content={"ok": True})

        resp = self._run(mw.dispatch, self._make_request("/metrics"), call_next)
        assert resp.status_code == 200

    # ------------------------------------------------------------------
    # W10 / P2-SEC-2：豁免只在回环绑定时成立
    # ------------------------------------------------------------------

    def _dispatch_metrics(self, monkeypatch, host: str):
        """按给定配置绑定地址跑一次 /metrics 请求，返回响应。"""
        from app.config import settings
        from app.middleware import AuthMiddleware
        from starlette.responses import JSONResponse

        monkeypatch.setattr(settings, "metrics_auth_enabled", False)
        monkeypatch.setattr(settings, "host", host)
        mw = AuthMiddleware.__new__(AuthMiddleware)
        mw.enabled = True  # 鉴权开启：只考 /metrics 的豁免分支

        async def call_next(req):
            return JSONResponse(content={"ok": True})

        return self._run(mw.dispatch, self._make_request("/metrics"), call_next)

    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
    def test_metrics_still_exempt_on_loopback_bind(self, monkeypatch, host):
        """回环绑定 → 豁免保留（P2-F2 的原始动机：本机监控栈无凭据抓取）。"""
        assert self._dispatch_metrics(monkeypatch, host).status_code == 200

    @pytest.mark.parametrize(
        "host", ["0.0.0.0", "::", "", "   ", "10.0.0.5", "192.168.1.50"]
    )
    def test_metrics_not_exempt_on_non_loopback_bind(self, monkeypatch, host):
        """绑到可路由/通配地址 → /metrics 必须鉴权。

        此前只要 METRICS_AUTH_ENABLED=False 就全局豁免，等于把调用量、错误率、
        延迟、工具名这些运营情报无偿开放给整个网段（标签已消毒不代表没有情报
        价值）。判据用**配置绑定地址**而不是 request.client.host：反代部署下
        对端恒为代理（常常就是回环），按对端判会直接 fail-open（P3-13 同源教训）。
        """
        resp = self._dispatch_metrics(monkeypatch, host)
        assert resp.status_code == 401, (
            "非回环绑定 %r 下 /metrics 仍免鉴权（P2-SEC-2）" % host
        )


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


class TestOtelGlobalReset:
    """B1 回归：OTel 全局清理必须覆盖全部 _otel_*（含 v0.6.0+ 扩展面）。

    修复前 _init_otel 成功路径会填充 18 个模块级全局（11 counter + 5 histogram
    + meter + shutdown），而清理只重置其中 5 个，残留的 MagicMock instrument
    会静默顶替全局，使「OTel 关闭即不做 OTel 双写」的不变量依赖测试执行顺序。
    """

    def teardown_method(self):
        """兜底清理：即使断言失败，本类也不留下残留全局与脏指标表"""
        import app.observability as obs_module

        _reset_otel_globals(obs_module)
        # record_llm_request 一次调用会写 4 张表，按文件既有 .clear() 约定逐个清空；
        # 放 teardown 而非用例内，断言失败时也不会留下脏数据（本类是文件最后一个类）
        obs_module._llm_requests_total.clear()
        obs_module._llm_latency_sum.clear()
        obs_module._llm_latency_count.clear()
        obs_module._llm_tokens_total.clear()

    @staticmethod
    def _fill_otel_globals(monkeypatch):
        """按既有测试相同的 mock 靶点跑一次真实 _init_otel()，填充全部 _otel_* 全局"""
        import app.observability as obs_module

        monkeypatch.setattr("app.config.settings.otel_enabled", True)

        mock_meter = MagicMock()
        # 数量与 app/observability.py 当前实现一致：11 counter / 5 histogram
        mock_meter.create_counter.side_effect = [MagicMock() for _ in range(11)]
        mock_meter.create_histogram.side_effect = [MagicMock() for _ in range(5)]
        mock_provider = MagicMock()
        mock_provider.get_meter.return_value = mock_meter

        with patch("app.observability.MeterProvider", return_value=mock_provider), \
             patch("opentelemetry.metrics.set_meter_provider"), \
             patch("app.observability.OTLPMetricExporter"), \
             patch("app.observability.PeriodicExportingMetricReader"), \
             patch("app.observability.Resource"):
            obs_module._init_otel()

        return obs_module

    def test_reset_clears_all_otel_globals(self, monkeypatch):
        """全局清空不变量：helper 后不存在任何非 None 的 _otel_* 全局"""
        obs_module = self._fill_otel_globals(monkeypatch)

        # 先证明扩展面确实被填充，否则下面的清空断言可能因"本来就没建"而假绿
        assert obs_module._otel_llm_req_counter is not None
        assert obs_module._otel_kb_hit_counter is not None

        _reset_otel_globals(obs_module)

        assert [
            name
            for name in vars(obs_module)
            if name.startswith("_otel_") and getattr(obs_module, name) is not None
        ] == []
        # 前缀匹配大小写敏感的边界：环境常量 _OTEL_AVAILABLE 不得被误伤
        assert obs_module._OTEL_AVAILABLE is True
        # 收尾：避免本用例自己成为新的污染源
        _reset_otel_globals(obs_module)

    def test_record_llm_request_does_not_double_write_when_otel_disabled(self, monkeypatch):
        """顺序无关性：清空后 OTel 关闭时 record_llm_request 不得再做 OTel 双写"""
        obs_module = self._fill_otel_globals(monkeypatch)
        stale_counter = obs_module._otel_llm_req_counter
        assert stale_counter is not None

        _reset_otel_globals(obs_module)

        monkeypatch.setattr("app.config.settings.otel_enabled", False)
        assert obs_module._otel_llm_req_counter is None

        # 用增量而非绝对值断言，避免依赖其它测试留下的累计值
        key = ("openai", "gpt", "ok")
        before = obs_module._llm_requests_total.get(key, 0)
        obs_module.record_llm_request("openai", "gpt", "ok", 0.5, 10, 20)

        # 修复前残留的 MagicMock 会在这里被 .add() 一次，即"OTel 关闭仍双写"
        assert obs_module._otel_llm_req_counter is None
        stale_counter.add.assert_not_called()
        # 清理 OTel 全局不得影响 Prometheus 文本指标主路径的累加
        assert obs_module._llm_requests_total[key] == before + 1
