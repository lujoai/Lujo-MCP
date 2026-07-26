"""集成测试：Redis L2 缓存真实读写链路。

目标：
- 验证 LLM 分析缓存的 L2 Redis 写入与回填
- 验证 Dashboard 概览缓存的 L2 Redis 写入与回读

说明：
- 这些测试依赖真实 Redis 服务，默认环境下会 skip
- 推荐启动方式：`redis-server --port 6379 --appendonly no`
"""

import socket

import pytest

from app.api import dashboard as dashboard_module
from app.llm import analyzer as analyzer_module


def _port_open(host: str, port: int) -> bool:
    s = socket.socket()
    s.settimeout(1)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


@pytest.fixture
def redis_url():
    return "redis://127.0.0.1:6379/0"


@pytest.fixture
def require_redis(redis_url):
    if not _port_open("127.0.0.1", 6379):
        pytest.skip("Redis 未启动，跳过 Redis L2 缓存集成测试")

    import redis

    client = redis.Redis.from_url(redis_url, socket_timeout=2, decode_responses=True)
    client.ping()
    yield client
    client.flushdb()
    client.close()


@pytest.fixture(autouse=True)
def reset_cache_state(monkeypatch, redis_url):
    monkeypatch.setattr("app.config.settings.redis_url", redis_url)
    analyzer_module._analysis_cache.clear()
    analyzer_module._redis_cache_client = None
    analyzer_module._redis_cache_initialized = False
    dashboard_module._cache.clear()
    yield
    analyzer_module._analysis_cache.clear()
    analyzer_module._redis_cache_client = None
    analyzer_module._redis_cache_initialized = False
    dashboard_module._cache.clear()


@pytest.mark.integration
def test_llm_cache_roundtrip_via_redis_l2(require_redis):
    fingerprint = "redis-l2-fp-001"
    payload = {
        "analysis": {"root_cause": "redis-l2", "impact": "low", "fix": "none", "confidence": "low"},
        "cached": False,
    }

    analyzer_module._set_cache_result(fingerprint, payload)

    raw = require_redis.get(f"ai-debug:llm:cache:{fingerprint}")
    assert raw is not None

    analyzer_module._analysis_cache.clear()
    restored = analyzer_module._get_cached_result(fingerprint)

    assert restored is not None
    assert restored["analysis"]["root_cause"] == "redis-l2"
    assert fingerprint in analyzer_module._analysis_cache


@pytest.mark.integration
def test_dashboard_cache_roundtrip_via_redis_l2(require_redis, monkeypatch):
    sample = [
        {
            "trace_id": "trace-001",
            "timestamp": 123.0,
            "type": "ERROR",
            "message": "boom",
            "trace_kind": "exception",
            "occurrence_count": 1,
            "has_silent_failure": False,
            "verify_count": 0,
        }
    ]

    monkeypatch.setattr(dashboard_module.errors, "list_recent", lambda limit=100: [])
    monkeypatch.setattr(dashboard_module.logs, "list_request_ids", lambda limit=100: [])

    # 首次调用走计算路径并写入 L2
    monkeypatch.setattr(dashboard_module, "_extract_trace_summary", lambda request_id: sample[0])
    monkeypatch.setattr(dashboard_module.logs, "list_request_ids", lambda limit=100: ["trace-001"])
    result1 = dashboard_module._collect_all_traces(limit=10)
    assert result1 == sample

    raw = require_redis.get(dashboard_module._REDIS_CACHE_KEY)
    assert raw is not None

    # 清空 L1，第二次调用应可从 L2 回读
    dashboard_module._cache.clear()
    monkeypatch.setattr(dashboard_module.logs, "list_request_ids", lambda limit=100: [])
    monkeypatch.setattr(dashboard_module.errors, "list_recent", lambda limit=100: [])
    result2 = dashboard_module._collect_all_traces(limit=10)

    assert result2 == sample
