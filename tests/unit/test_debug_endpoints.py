"""P0-1 回归：debug.py 端点不因缺失 import time 而必然 500。

历史背景：list_sessions / debug_health 调用 time.time()，但文件顶部曾无
`import time`，两个端点必然 500。本测试直接调用端点函数证明 import 存在。
"""
from unittest.mock import patch


def test_debug_health_returns_timestamp():
    from app.api.debug import debug_health

    result = debug_health()
    assert result["status"] == "ok"
    assert isinstance(result["timestamp"], float)


def test_list_sessions_returns_ok():
    from app.api.debug import list_sessions

    with patch("app.api.debug.session_manager.list_active", return_value=[]):
        result = list_sessions()

    assert result["count"] == 0
    assert result["sessions"] == []


def test_list_sessions_uses_time_for_idle_seconds():
    """list_sessions 的 idle_seconds 计算依赖 time.time()，证明 import 有效。"""
    from app.api.debug import list_sessions

    fake_session = {
        "session_id": "s1",
        "created_at": 100.0,
        "last_active": 100.0,
        "metadata": {},
    }
    with patch("app.api.debug.session_manager.list_active", return_value=[fake_session]):
        result = list_sessions()

    assert result["count"] == 1
    idle = result["sessions"][0]["idle_seconds"]
    assert isinstance(idle, float)


# ---------------------------------------------------------------------------
# FIX: R7-A3 —— /api/debug/analyze/stream SSE 统一补缓冲控制头
# ---------------------------------------------------------------------------


def test_analyze_stream_has_buffer_control_headers():
    """R7-A3 回归：analyze/stream 必须带 Cache-Control/X-Accel-Buffering 头
    （与 dashboard 流对称，防 nginx 默认缓冲攒批延迟事件）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.debug import router
    from app.runtime.core.logs import add_log, create_request_id

    rid = create_request_id()
    add_log(rid, "request_start", {"method": "POST", "url": "/x"})

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    resp = client.post("/api/debug/analyze/stream", json={"request_id": rid})

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert resp.headers["Cache-Control"] == "no-cache"
    assert resp.headers["X-Accel-Buffering"] == "no"


# ---------------------------------------------------------------------------
# FIX(v0.7.1-b1-7): /analyze/stream build_context 包异常保护（R7 Minor）
# ---------------------------------------------------------------------------


def test_analyze_stream_build_context_error_returns_500():
    """build_context 抛错必须转 500 语义化响应（与兄弟端点 /analyze 一致）。

    修复前畸形 trace 会让异常裸抛成未处理 500（FastAPI 默认 500 但无日志/
    形状不齐），此处验证端点自身兜底路径可达。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.debug import router
    from app.runtime.core.logs import add_log, create_request_id

    rid = create_request_id()
    add_log(rid, "request_start", {"method": "POST", "url": "/x"})

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    with patch("app.api.debug.build_context", side_effect=RuntimeError("boom")):
        resp = client.post("/api/debug/analyze/stream", json={"request_id": rid})

    assert resp.status_code == 500
    assert resp.json()["detail"] == "Internal server error"
