"""真实 PostgreSQL/asyncpg 验证。

这些用例只允许在显式 ``LUJO_TEST_STORAGE_BACKEND=postgresql`` 且指向本轮
临时数据库时运行；普通 integration/e2e 测试继续由 conftest 默认隔离到 memory。
"""

import asyncio
import datetime as dt
import os
import uuid

import httpx
import pytest
import pytest_asyncio

from app.config import settings
from app.runtime.core import errors
from app.runtime.core.storage import async_pg_store, factory


TEST_DATABASE = "lujo_test_20260910_pgasync_a9d5600"
TEST_SOURCE_PREFIX = "pg-async-true:"


async def _poll(query, predicate, timeout: float = 5.0):
    """Poll an async query until predicate passes or an explicit deadline expires."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last = None
    while True:
        last = await query()
        if predicate(last):
            return last
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise AssertionError(f"condition not met before deadline; last={last!r}")
        await asyncio.sleep(min(0.05, remaining))


def _case(tag: str | None = None):
    tag = tag or uuid.uuid4().hex[:12]
    frames = [{"file": f"pg-async-{tag}.py", "line": 17, "function": "handler"}]
    fingerprint = errors.compute_fingerprint("ValueError", frames)
    source = f"{TEST_SOURCE_PREFIX}{tag}"
    return tag, fingerprint, frames, source


@pytest_asyncio.fixture(autouse=True)
async def _guard_true_pg(monkeypatch):
    """Require explicit test opt-in and close asyncpg between every test."""
    if os.environ.get("LUJO_TEST_STORAGE_BACKEND", "memory").lower() != "postgresql":
        pytest.skip("真实 PG 测试必须显式设置 LUJO_TEST_STORAGE_BACKEND=postgresql")
    if settings.pg_database != TEST_DATABASE:
        pytest.skip(f"真实 PG 测试必须指向本轮临时数据库 {TEST_DATABASE}")

    monkeypatch.setattr(settings, "storage_backend", "postgresql")
    monkeypatch.setattr(settings, "pg_async_enabled", True)
    errors._last_scheduled.clear()
    await async_pg_store.close_pool()

    yield

    # 只按本测试包自有 source 前缀清理；数据库本身也是本轮唯一创建的临时库。
    try:
        pool = await async_pg_store._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM errors WHERE source LIKE $1",
                f"{TEST_SOURCE_PREFIX}%",
            )
    finally:
        await async_pg_store.close_pool()
        errors._last_scheduled.clear()


async def _dashboard_client():
    from fastapi import FastAPI
    from app.api.dashboard import router

    app = FastAPI()
    app.include_router(router)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


@pytest.mark.integration
@pytest.mark.pg
@pytest.mark.asyncio
async def test_real_pg_throttle_isolated_by_session_and_query_filters(monkeypatch):
    """真实 PG 验证相同指纹的跨会话节流隔离、同会话节流和查询过滤。"""
    _, fingerprint, frames, source = _case()
    payload = {
        "type": "ValueError",
        "message": "shared fingerprint",
        "frames": frames,
        "traceback": "tb",
    }
    store = factory.get_error_store_async()
    scheduled = []
    from app.runtime.core.storage.async_pg_store import AsyncPGErrorStore

    original_upsert = AsyncPGErrorStore.upsert_error

    async def tracked_upsert(self, record_data):
        scheduled.append(dict(record_data))
        return await original_upsert(self, record_data)

    monkeypatch.setattr(AsyncPGErrorStore, "upsert_error", tracked_upsert)
    # Redis cache invalidation is unrelated to this PG timing contract and may
    # spend longer than the two-second throttle window when Redis is absent.
    from app.api import dashboard

    monkeypatch.setattr(dashboard, "invalidate_cache", lambda *args, **kwargs: None)

    # 同一 2 秒窗口内：相同 fingerprint + 同一 session 的第二次应被节流；
    # 不同 session 必须各自调度并在 PG 中形成独立记录。
    errors.record(payload, source=source, session_id="session-a")
    errors.record(payload, source=source, session_id="session-a")
    errors.record(payload, source=source, session_id="session-b")
    assert set(errors._last_scheduled) == {
        (fingerprint, "session-a"),
        (fingerprint, "session-b"),
    }

    rows = await _poll(
        lambda: store.query_errors(fingerprint=fingerprint, since_minutes=10),
        lambda value: len(value) == 2,
    )
    by_session = {row["session_id"]: row for row in rows}
    assert set(by_session) == {"session-a", "session-b"}
    assert [(item["fingerprint"], item["session_id"]) for item in scheduled] == [
        (fingerprint, "session-a"),
        (fingerprint, "session-b"),
    ]
    assert by_session["session-a"]["occurrence_count"] == 1, by_session["session-a"]
    assert by_session["session-b"]["occurrence_count"] == 1, by_session["session-b"]

    only_a = await store.query_errors(
        fingerprint=fingerprint,
        session_id="session-a",
        since_minutes=10,
    )
    only_b = await store.query_errors(
        fingerprint=fingerprint,
        session_id="session-b",
        since_minutes=10,
    )
    assert [row["session_id"] for row in only_a] == ["session-a"]
    assert [row["session_id"] for row in only_b] == ["session-b"]
    assert await store.query_errors(
        fingerprint=fingerprint,
        session_id="missing-session",
        since_minutes=10,
    ) == []


@pytest.mark.integration
@pytest.mark.pg
@pytest.mark.asyncio
async def test_real_pg_async_record_store_and_dashboard_history():
    """验证 running loop 内 record → asyncpg → PG → factory → dashboard 完整链路。"""
    tag, fingerprint, frames, source = _case()
    session_id = f"session-{tag}"
    payload = {
        "type": "ValueError",
        "message": "asyncpg round trip",
        "frames": frames,
        "traceback": "Traceback (most recent call last)",
    }

    error_id = errors.record(payload, source=source, session_id=session_id)
    store = factory.get_error_store_async()
    rows = await _poll(
        lambda: store.query_errors(
            fingerprint=fingerprint,
            session_id=session_id,
            since_minutes=10,
        ),
        lambda value: len(value) == 1,
    )
    row = rows[0]

    assert row["error_id"] == error_id
    assert row["fingerprint"] == fingerprint
    assert row["session_id"] == session_id
    assert row["occurrence_count"] == 1
    assert row["frames"] == frames
    assert isinstance(row["created_at"], str)
    assert isinstance(row["updated_at"], str)
    dt.datetime.fromisoformat(row["created_at"])
    dt.datetime.fromisoformat(row["updated_at"])

    # 再经一次真实 errors 表读取，避免只验证内存态/包装对象。
    pool = await async_pg_store._get_pool()
    async with pool.acquire() as conn:
        raw = await conn.fetchrow(
            "SELECT error_id, fingerprint, session_id, occurrence_count, frames, "
            "created_at, updated_at FROM errors WHERE fingerprint = $1 "
            "AND session_id = $2",
            fingerprint,
            session_id,
        )
    assert raw is not None
    assert raw["error_id"] == error_id
    assert raw["fingerprint"] == fingerprint
    assert raw["session_id"] == session_id
    assert raw["occurrence_count"] == 1

    async with await _dashboard_client() as client:
        response = await client.get(
            "/api/dashboard/errors/history",
            params={"fingerprint": fingerprint, "session_id": session_id, "limit": 5},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is False
    assert body["total"] == 1
    dashboard_row = body["errors"][0]
    assert dashboard_row["fingerprint"] == fingerprint
    assert dashboard_row["session_id"] == session_id
    assert dashboard_row["occurrence_count"] == 1
    assert dashboard_row["frames"] == frames
    dt.datetime.fromisoformat(dashboard_row["created_at"])
    dt.datetime.fromisoformat(dashboard_row["updated_at"])


@pytest.mark.integration
@pytest.mark.pg
@pytest.mark.asyncio
async def test_real_pg_dashboard_query_failure_degrades_with_warning(caplog, monkeypatch):
    """查询失败时 dashboard 返回空列表 + degraded=true，并留下 warning。"""
    import logging
    from app.runtime.core.storage.async_pg_store import AsyncPGErrorStore

    async def fail_query(*args, **kwargs):
        raise ConnectionError("simulated asyncpg connection failure")

    monkeypatch.setattr(AsyncPGErrorStore, "query_errors", fail_query)
    async with await _dashboard_client() as client:
        with caplog.at_level(logging.WARNING, logger="lujo-mcp.dashboard"):
            response = await client.get("/api/dashboard/errors/history?limit=5")

    assert response.status_code == 200
    body = response.json()
    assert body["errors"] == []
    assert body["total"] == 0
    assert body["degraded"] is True
    assert any("AsyncPG errors query" in record.getMessage() for record in caplog.records)


@pytest.mark.integration
@pytest.mark.pg
@pytest.mark.asyncio
async def test_invalid_storage_backend_fails_fast_in_dashboard():
    """非法 STORAGE_BACKEND 不得被 dashboard 静默变成空列表。"""
    from app.api.dashboard import get_errors_history

    settings.storage_backend = "postgres"
    with pytest.raises(ValueError, match="Invalid STORAGE_BACKEND"):
        await get_errors_history(limit=5)


@pytest.mark.integration
@pytest.mark.pg
@pytest.mark.asyncio
async def test_asyncpg_pool_explicit_close_reopens_on_same_loop():
    """显式关闭模块级 pool 后，同一事件循环可重新初始化且不泄漏绑定。"""
    _, fingerprint, frames, source = _case()
    store = factory.get_error_store_async()
    await store.upsert_error({
        "error_id": f"err-{uuid.uuid4().hex[:12]}",
        "fingerprint": fingerprint,
        "type": "ValueError",
        "message": "pool lifecycle",
        "frames": frames,
        "frame_count": len(frames),
        "traceback": "tb",
        "source": source,
        "session_id": "pool-session",
    })
    assert async_pg_store._pool is not None

    await async_pg_store.close_pool()
    assert async_pg_store._pool is None
    assert async_pg_store._initialized is False

    rows = await store.query_errors(
        fingerprint=fingerprint,
        session_id="pool-session",
        since_minutes=10,
    )
    assert len(rows) == 1
    assert async_pg_store._pool is not None
