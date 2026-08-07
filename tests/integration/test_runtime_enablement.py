"""集成测试：环境启用链路 smoke tests。

这些用例专门验证“需要额外环境或 feature flag 才能启用”的能力：
- PostgreSQL / asyncpg
- Redis 状态后端
- OpenTelemetry
- 熔断器

默认开发环境下允许 skip；一旦对应环境变量显式启用，则失败应被视为真实问题。
"""

import socket

import pytest

from app.config import settings


def _port_open(host: str, port: int) -> bool:
    s = socket.socket()
    s.settimeout(1)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def _require_postgresql() -> None:
    if settings.storage_backend != "postgresql":
        pytest.skip("STORAGE_BACKEND != postgresql")


def _require_redis() -> None:
    if settings.state_backend != "redis":
        pytest.skip("STATE_BACKEND != redis")


def _require_otel() -> None:
    if not settings.otel_enabled:
        pytest.skip("OTEL_ENABLED != true")


def _require_circuit_breaker() -> None:
    if not settings.circuit_breaker_enabled:
        pytest.skip("CIRCUIT_BREAKER_ENABLED != true")


@pytest.mark.integration
@pytest.mark.pg
def test_postgresql_psycopg2_connection_smoke():
    _require_postgresql()

    import psycopg2

    assert _port_open(settings.pg_host, settings.pg_port), (
        f"PostgreSQL 端口不可达: {settings.pg_host}:{settings.pg_port}"
    )

    conn = psycopg2.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        dbname=settings.pg_database,
        user=settings.pg_user,
        password=settings.pg_password,
        connect_timeout=3,
    )
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        row = cur.fetchone()
        assert row[0] == 1
    finally:
        conn.close()


@pytest.mark.integration
@pytest.mark.pg
@pytest.mark.asyncio
async def test_postgresql_asyncpg_connection_smoke():
    _require_postgresql()
    if not settings.pg_async_enabled:
        pytest.skip("PG_ASYNC_ENABLED != true")

    import asyncpg

    assert _port_open(settings.pg_host, settings.pg_port), (
        f"PostgreSQL 端口不可达: {settings.pg_host}:{settings.pg_port}"
    )

    conn = await asyncpg.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        database=settings.pg_database,
        user=settings.pg_user,
        password=settings.pg_password,
        timeout=3,
    )
    try:
        row = await conn.fetchrow("SELECT 1 AS n")
        assert row["n"] == 1
    finally:
        await conn.close()


@pytest.mark.integration
def test_redis_state_store_smoke():
    _require_redis()

    from app.state.store import RedisStateStore

    url = settings.redis_url
    assert "redis://" in url, f"REDIS_URL 非法: {url}"

    store = RedisStateStore(url)
    key = "runtime-enable:test"
    value = store.incr(key)
    try:
        assert value >= 1
        assert store.get(key) >= 1
        assert any(k.startswith(key) for k in store.keys("runtime-enable:"))
        assert store.allow("runtime-rate:test", limit=2, window=60) is True
    finally:
        try:
            store._r.delete(key, "runtime-rate:test")  # noqa: SLF001 - 测试清理
        finally:
            store.close()


@pytest.mark.integration
def test_metrics_endpoint_smoke_with_otel_enabled():
    _require_otel()

    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    health = client.get("/health")
    metrics = client.get("/metrics")

    assert health.status_code == 200
    assert metrics.status_code == 200
    assert "http_requests_total" in metrics.text


@pytest.mark.integration
def test_circuit_breaker_instances_available_when_enabled():
    _require_circuit_breaker()

    from app.llm.analyzer import _get_llm_circuit_breaker
    from app.runtime.core.storage.pg_store import _get_pg_circuit_breaker

    assert _get_llm_circuit_breaker() is not None
    assert _get_pg_circuit_breaker() is not None
