"""AsyncPGErrorStore 查询与端点链路单元测试"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.runtime.core.storage.async_pg_store import AsyncPGErrorStore
from app.config import settings


@pytest.mark.asyncio
async def test_async_pg_error_store_query_errors(monkeypatch):
    """验证 AsyncPGErrorStore.query_errors 拼接参数并解析 frames JSON。"""
    store = AsyncPGErrorStore()

    from datetime import datetime
    fake_rows = [
        {
            "error_id": "err-1",
            "fingerprint": "fp-1",
            "exception_type": "ValueError",
            "message": "bad val",
            "frames": '[{"file": "x.py", "line": 10, "function": "fn"}]',
            "frame_count": 1,
            "traceback": "tb",
            "source": "api",
            "session_id": "sess-1",
            "occurrence_count": 3,
            "first_seen": 100.0,
            "last_seen": 200.0,
            "created_at": datetime(2026, 9, 9, 22, 0, 14),
            "updated_at": datetime(2026, 9, 9, 22, 0, 14),
        }
    ]

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = fake_rows

    class FakePoolAcquire:
        async def __aenter__(self):
            return mock_conn

        async def __aexit__(self, *args):
            pass

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = FakePoolAcquire()

    monkeypatch.setattr("app.runtime.core.storage.async_pg_store._ensure_init", AsyncMock())
    monkeypatch.setattr("app.runtime.core.storage.async_pg_store._get_pool", AsyncMock(return_value=mock_pool))

    results = await store.query_errors(fingerprint="fp-1", session_id="sess-1", limit=10)
    assert len(results) == 1
    assert results[0]["error_id"] == "err-1"
    assert results[0]["fingerprint"] == "fp-1"
    assert results[0]["type"] == "ValueError"
    assert isinstance(results[0]["frames"], list)
    assert results[0]["frames"][0]["file"] == "x.py"
    assert results[0]["occurrence_count"] == 3
    assert results[0]["created_at"] == "2026-09-09T22:00:14"
    assert results[0]["updated_at"] == "2026-09-09T22:00:14"

    # 验证 SQL 调用参数
    args = mock_conn.fetch.call_args[0]
    sql = args[0]
    assert "fingerprint = $1" in sql
    assert "session_id = $2" in sql
    assert args[1] == "fp-1"
    assert args[2] == "sess-1"


def test_dashboard_errors_history_async_pg_dispatch(monkeypatch):
    """验证 GET /api/dashboard/errors/history 在 pg_async_enabled=True 时分发到 AsyncPGErrorStore。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.dashboard import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    monkeypatch.setattr(settings, "storage_backend", "postgresql")
    monkeypatch.setattr(settings, "pg_async_enabled", True)

    async def fake_query_errors(self, fingerprint=None, session_id=None, since_minutes=1440, limit=100):
        return [{
            "error_id": "err-async-test",
            "fingerprint": fingerprint or "fp-test",
            "type": "RuntimeError",
            "message": "async error",
            "frames": [],
            "frame_count": 0,
            "traceback": "",
            "source": "test",
            "session_id": session_id or "_global",
            "occurrence_count": 1,
            "first_seen": 1000.0,
            "last_seen": 1000.0,
            "created_at": None,
            "updated_at": None,
        }]

    monkeypatch.setattr(AsyncPGErrorStore, "query_errors", fake_query_errors)

    resp = client.get("/api/dashboard/errors/history?fingerprint=fp-test&limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["errors"][0]["error_id"] == "err-async-test"
    assert data["degraded"] is False


def test_dashboard_errors_history_async_pg_degraded_on_error(monkeypatch):
    """当 AsyncPG 查询抛出异常时，应安全降级返回空列表并标记 degraded=True。"""
    from app.config import settings
    from app.runtime.core.storage.async_pg_store import AsyncPGErrorStore
    from starlette.testclient import TestClient
    from app.api.dashboard import router
    from fastapi import FastAPI

    monkeypatch.setattr(settings, "pg_async_enabled", True)
    monkeypatch.setattr(settings, "storage_backend", "postgresql")

    async def fail_query_errors(*args, **kwargs):
        raise ConnectionRefusedError("Pool is closed")

    monkeypatch.setattr(AsyncPGErrorStore, "query_errors", fail_query_errors)

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    resp = client.get("/api/dashboard/errors/history?fingerprint=fp-test&limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["errors"] == []
    assert data["degraded"] is True


def test_dashboard_errors_history_rejects_invalid_backend(monkeypatch):
    """非法 STORAGE_BACKEND 必须在 dashboard 入口 fail-fast。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.dashboard import router

    monkeypatch.setattr(settings, "storage_backend", "postgres")
    monkeypatch.setattr(settings, "pg_async_enabled", True)

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    with pytest.raises(ValueError, match="Invalid STORAGE_BACKEND"):
        client.get("/api/dashboard/errors/history?limit=5")
