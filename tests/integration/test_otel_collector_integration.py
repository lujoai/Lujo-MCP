"""集成测试：OpenTelemetry OTLP exporter -> 本地 collector 端到端验证。"""

import threading
from concurrent import futures

import grpc
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.metrics.v1 import (
    metrics_service_pb2,
    metrics_service_pb2_grpc,
)

import app.observability as obs_module


class _FakeMetricsCollector(metrics_service_pb2_grpc.MetricsServiceServicer):
    def __init__(self):
        self.requests = []
        self.event = threading.Event()

    def Export(self, request, context):
        self.requests.append(request)
        self.event.set()
        return metrics_service_pb2.ExportMetricsServiceResponse()


@pytest.fixture
def reset_observability():
    obs_module._request_total.clear()
    obs_module._error_total.clear()
    obs_module._latency_sum.clear()
    obs_module._latency_count.clear()
    obs_module._otel_meter = None
    obs_module._otel_request_counter = None
    obs_module._otel_error_counter = None
    obs_module._otel_latency_histogram = None
    obs_module._otel_shutdown = None
    yield
    obs_module._request_total.clear()
    obs_module._error_total.clear()
    obs_module._latency_sum.clear()
    obs_module._latency_count.clear()
    obs_module._otel_meter = None
    obs_module._otel_request_counter = None
    obs_module._otel_error_counter = None
    obs_module._otel_latency_histogram = None
    obs_module._otel_shutdown = None


@pytest.fixture
def local_metrics_collector():
    collector = _FakeMetricsCollector()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(collector, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield collector, f"http://127.0.0.1:{port}"
    finally:
        server.stop(0)


@pytest.mark.integration
def test_otel_exports_metrics_to_local_collector(monkeypatch, reset_observability, local_metrics_collector):
    collector, endpoint = local_metrics_collector

    monkeypatch.setattr("app.config.settings.otel_enabled", True)
    monkeypatch.setattr("app.config.settings.otel_exporter_endpoint", endpoint)
    monkeypatch.setattr("app.config.settings.otel_metrics_interval_ms", 100)
    monkeypatch.setattr("app.config.settings.metrics_auth_enabled", False)
    monkeypatch.setattr(obs_module.otel_metrics, "set_meter_provider", lambda provider: None)

    app = FastAPI()

    @app.get("/ok")
    def ok():
        return {"ok": True}

    obs_module.setup_observability(app)

    with TestClient(app) as client:
        resp = client.get("/ok")
        assert resp.status_code == 200
        metrics_resp = client.get("/metrics")
        assert metrics_resp.status_code == 200
        assert "http_requests_total" in metrics_resp.text

    obs_module.shutdown_observability()

    assert collector.event.wait(5), "OTLP collector 未收到任何 export 请求"
    assert collector.requests, "collector 请求列表为空"

    metric_names = set()
    for export_req in collector.requests:
        for resource_metrics in export_req.resource_metrics:
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    metric_names.add(metric.name)

    assert "http_requests_total" in metric_names
    assert "http_request_duration_seconds" in metric_names
