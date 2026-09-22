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


# ---------------------------------------------------------------------------
# P2-API-1（W6b）：/api/debug/analyze 对 ingest 来源的现场静默失效
#   根因：trace_repo 落库 step=trace_data，而 build_context 只认
#   request_start / response_ready / error → context["errors"] 恒空 →
#   _get_error_signal 无指纹 → KB 精确指纹命中与回写必落空。
#   修法：仅在 app/api/debug.py 的调用点补位（不改 build_context 本身）。
# ---------------------------------------------------------------------------

_INGEST_FRAMES = [
    {"file": "app/services/orders.py", "line": 42, "function": "create_order"},
    {"file": "app/api/routes.py", "line": 10, "function": "handler"},
]


def _analyze_client():
    """仅挂 debug router 的 TestClient（与上方既有用例同构）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.debug import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _ingest_trace(message: str = "ingest boom", exc_type: str = "ValueError") -> str:
    """用 trace_repo.save_trace 造一条真实 ingest 记录（= /ingest/error 的落库路径）。"""
    from app.runtime.core.trace_repo import save_trace

    return save_trace(exc_type=exc_type, message=message, frames=_INGEST_FRAMES, source="ingest")


def _captured_analyze_context(request_id: str):
    """调 POST /api/debug/analyze，捕获端点实际传给 analyze 的 context。

    patch 靶点是被测模块里的名字（app.api.debug.analyze）——本仓库既有纪律。
    不依赖 LLM：patch 后端点不会真实调用外部服务。
    """
    captured = {}

    def _fake_analyze(context, model=None):
        captured["context"] = context
        return {
            "analysis": {"root_cause": "stub", "impact": "low", "fix": "stub", "confidence": "low"}
        }

    with patch("app.api.debug.analyze", side_effect=_fake_analyze):
        resp = _analyze_client().post("/api/debug/analyze", json={"request_id": request_id})
    return resp, captured.get("context")


class TestAnalyzeSeesIngestedTrace:
    """C1/C2：ingest 来源的现场必须拿到 errors / exception / 指纹。"""

    def test_ingest_trace_errors_and_exception_and_fingerprint(self):
        from app.runtime.core.errors import compute_fingerprint

        trace_id = _ingest_trace()
        resp, ctx = _captured_analyze_context(trace_id)

        assert resp.status_code == 200, resp.text
        assert ctx["errors"], "ingest 来源的 context['errors'] 不应为空（P2-API-1）"
        assert ctx["exception"]["frames"], "errors 含 frames 时必须提升为 exception"
        assert ctx["exception"]["fingerprint"] == compute_fingerprint("ValueError", _INGEST_FRAMES)

    def test_downstream_error_signal_has_fingerprint(self):
        from app.llm.context_prep import _get_error_signal

        trace_id = _ingest_trace()
        resp, ctx = _captured_analyze_context(trace_id)

        assert resp.status_code == 200, resp.text
        _, _, fingerprint = _get_error_signal(ctx)
        assert fingerprint is not None, "_get_error_signal 必须能取到指纹（KB 命中不再必然 miss）"

    def test_existing_error_step_context_unchanged(self):
        """C3/A4 守卫：既有 step='error' 路径不重复、不追加、顺序不变。"""
        from app.runtime.core.logs import add_log, create_request_id

        rid = create_request_id()
        add_log(rid, "request_start", {"method": "POST", "url": "/x"})
        add_log(
            rid,
            "error",
            {
                "type": "RuntimeError",
                "message": "run boom",
                "frames": [{"file": "a.py", "line": 1, "function": "f"}],
                "fingerprint": "fp-existing",
            },
        )

        resp, ctx = _captured_analyze_context(rid)

        assert resp.status_code == 200, resp.text
        assert len(ctx["errors"]) == 1, "既有 error 条目不得被补位追加或重复"
        assert ctx["errors"][0]["type"] == "RuntimeError"
        assert ctx["errors"][0]["fingerprint"] == "fp-existing"

    def test_ingest_message_is_redacted(self):
        """C4：落库前已脱敏；补位后送给 LLM 的路径不得出现原文。"""
        import json

        secret = "hunter2secret"
        trace_id = _ingest_trace(message=f'login failed password="{secret}"', exc_type="AuthError")

        resp, ctx = _captured_analyze_context(trace_id)

        assert resp.status_code == 200, resp.text
        blob = json.dumps(ctx, ensure_ascii=False, default=str)
        assert secret not in blob, "脱敏后的 context 不得出现密钥原文"
        assert "***" in blob, "应出现脱敏掩码"

    def test_missing_request_id_still_404(self):
        resp = _analyze_client().post("/api/debug/analyze", json={"request_id": "w6b-no-such-rid"})
        assert resp.status_code == 404

    def test_trace_without_trace_data_keeps_errors_empty(self):
        """C5：有 trace 但无 trace_data 时保持原样（不报错、不塞空 dict）。"""
        from app.runtime.core.logs import add_log, create_request_id

        rid = create_request_id()
        add_log(rid, "request_start", {"method": "POST", "url": "/x"})

        resp, ctx = _captured_analyze_context(rid)

        assert resp.status_code == 200, resp.text
        assert ctx["errors"] == []
        assert "exception" not in ctx


class TestAnalyzeStreamAndAsyncSeeIngestedTrace:
    """C6：/analyze/stream 与 /analyze/async 传给 LLM 的 context 同样带 errors。

    只测 context 组装（W8 实测：starlette TestClient 对永不结束的 SSE 流无法
    __enter__，故 stream 端点用即时结束的 stub 生成器，不去测流本身）。
    """

    def test_analyze_stream_context_has_errors(self):
        trace_id = _ingest_trace()
        captured = {}

        async def _fake_stream(context, model=None):
            captured["context"] = context
            yield "chunk"

        with patch("app.api.debug.analyze_stream_async", _fake_stream):
            resp = _analyze_client().post(
                "/api/debug/analyze/stream", json={"request_id": trace_id}
            )

        assert resp.status_code == 200, resp.text
        assert captured["context"]["errors"], "/analyze/stream 的 context['errors'] 不应为空"

    def test_analyze_async_context_has_errors(self, monkeypatch):
        from app.config import settings

        trace_id = _ingest_trace()
        captured = {}

        class _FakeQueue:
            async def enqueue(self, context, model=None):
                captured["context"] = context
                return "job-w6b"

        monkeypatch.setattr(settings, "llm_async_analysis_enabled", True)
        with patch("app.api.debug.get_analysis_queue", return_value=_FakeQueue()):
            resp = _analyze_client().post(
                "/api/debug/analyze/async", json={"request_id": trace_id}
            )

        assert resp.status_code == 200, resp.text
        assert captured["context"]["errors"], "/analyze/async 的 context['errors'] 不应为空"

    def test_repair_async_context_has_errors(self, monkeypatch):
        import asyncio
        from app.config import settings
        from app.mcp.tools.repair_api import repair_async_handler

        trace_id = _ingest_trace()
        captured = {}

        class _FakeRepairQueue:
            async def enqueue(self, context, model=None):
                captured["context"] = context
                return "job-repair-1"

        monkeypatch.setattr(settings, "agent_enabled", True)
        with patch("app.api.debug.get_repair_queue", return_value=_FakeRepairQueue()):
            resp = _analyze_client().post(
                "/api/debug/repair/async", json={"request_id": trace_id}
            )

        assert resp.status_code == 200, resp.text
        assert captured["context"]["errors"], "/repair/async 的 context['errors'] 不应为空"
        assert captured["context"].get("exception"), "/repair/async 的 context['exception'] 必须被提升"

        # 同时也验证 MCP 工具 repair_async_handler 具有相同的补位行为
        mcp_captured = {}

        class _FakeMcpRepairQueue:
            async def enqueue(self, context, model=None):
                mcp_captured["context"] = context
                return "job-mcp-1"

        with patch("app.mcp.tools.repair_api.get_repair_queue", return_value=_FakeMcpRepairQueue()):
            res = asyncio.run(repair_async_handler({"request_id": trace_id}))

        assert res.get("job_id") == "job-mcp-1"
        assert mcp_captured["context"]["errors"], "mcp repair_async 的 context['errors'] 不应为空"
        assert mcp_captured["context"].get("exception"), "mcp repair_async 的 context['exception'] 必须被提升"
