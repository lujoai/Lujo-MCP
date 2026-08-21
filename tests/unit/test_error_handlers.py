"""全局异常处理器测试"""
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from app.error_handlers import setup_error_handlers


def test_unhandled_exception_returns_500_with_semantic_payload():
    app = FastAPI()

    @app.middleware("http")
    async def add_trace_id_state(request: Request, call_next):
        request.state.trace_id = request.headers.get("X-Request-ID", "fallback-trace")
        return await call_next(request)

    setup_error_handlers(app)

    @app.get("/trigger-crash")
    def trigger_crash():
        raise RuntimeError("database connection failed unexpectedly")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/trigger-crash", headers={"X-Request-ID": "req-crash-1"})

    assert resp.status_code == 500
    data = resp.json()
    assert data["detail"] == "服务内部错误: RuntimeError"
    assert data["error_code"] == "INTERNAL_SERVER_ERROR"
    assert data["trace_id"] == "req-crash-1"
