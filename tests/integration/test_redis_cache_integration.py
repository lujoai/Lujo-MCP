"""集成测试：Redis L2 缓存真实读写链路。

目标：
- 验证 LLM 分析缓存的 L2 Redis 写入与回填
- 验证 Dashboard 概览缓存的 L2 Redis 写入与回读

说明：
- 这些测试依赖真实 Redis 服务，**默认不运行**：必须显式设置环境变量
  ``LUJO_TEST_REAL_REDIS=1`` 才会连接本机 127.0.0.1:6379 的真实 Redis
  （环境缺失门禁，属 CODE_REVIEW §0.6.2 第 6 条允许的 skip 形态——默认
  不跑危险测试，而非把断言失败转成 skip）。
- P1-TEST-10：旧版门禁只看 6379 端口可连——开发者本机若跑着别的用途的
  Redis（缓存、队列、别的项目），测试会连上去；且 teardown 直接全库
  flush 清空整个 db0，把不属于本测试的数据一并删除、不可恢复。
  现改为：测试写入的每个 key 都带本轮 uuid 前缀，teardown 用
  ``scan_iter(match)`` + ``delete`` 精确清理自己写的 key，任何情况下
  不做全库 flush。
- 推荐启动方式：`redis-server --port 6379 --appendonly no`
"""

import os
import socket
import uuid

import pytest

from app.api import dashboard as dashboard_module
from app.llm import cache as cache_module

_REAL_REDIS_ENV = "LUJO_TEST_REAL_REDIS"


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
def require_redis(redis_url, monkeypatch):
    """真实 Redis 门禁 + 本轮唯一 key 前缀 + 精确清理。

    两道门禁（均为带 reason 的环境缺失门禁）：
    1. 未显式设置 LUJO_TEST_REAL_REDIS=1 → skip（默认不跑危险测试）；
    2. 已设置但 6379 不可连 → skip（写明如何启动）。

    清理契约：teardown 只删除含本轮 uuid 的 key；任何情况下不做全库
    flush（旧实现清空整个 db0 会删掉别人的数据）。
    """
    if os.environ.get(_REAL_REDIS_ENV) != "1":
        pytest.skip(
            f"真实 Redis 集成测试默认不运行：设置 {_REAL_REDIS_ENV}=1 后才会"
            "连接本机 127.0.0.1:6379 的真实 Redis（写入并按前缀删除测试 key）"
        )
    if not _port_open("127.0.0.1", 6379):
        pytest.skip(
            f"{_REAL_REDIS_ENV}=1 已设置，但 127.0.0.1:6379 无 Redis 监听；"
            "请先启动：redis-server --port 6379 --appendonly no"
        )

    import redis

    client = redis.Redis.from_url(redis_url, socket_timeout=2, decode_responses=True)
    client.ping()

    # 本轮唯一前缀（含 uuid）：本测试写入的所有 key 都可由它识别
    prefix = f"w7-{uuid.uuid4().hex}-"
    # dashboard L2 key 由模块常量 _REDIS_CACHE_KEY 派生（_redis_cache_key(tier)
    # 返回 f"{_REDIS_CACHE_KEY}:{tier}"），monkeypatch 它使 dashboard key 以
    # 本轮前缀开头；LLM 缓存 key 是 app/llm/cache.py 内联 f-string
    # f"ai-debug:llm:cache:{fingerprint}"（非模块常量，不可 monkeypatch），
    # 故由用例把 prefix 放进 fingerprint，使 key 含本轮 uuid。
    monkeypatch.setattr(
        dashboard_module,
        "_REDIS_CACHE_KEY",
        f"{prefix}ai-debug:dashboard:all_traces",
    )

    yield client, prefix

    # teardown：只删含本轮 uuid 的 key（scan_iter + delete），绝不做全库 flush
    for key in client.scan_iter(match=f"*{prefix}*"):
        client.delete(key)
    client.close()


@pytest.fixture(autouse=True)
def reset_cache_state(monkeypatch, redis_url):
    monkeypatch.setattr("app.config.settings.redis_url", redis_url)
    cache_module._analysis_cache.clear()
    cache_module._redis_cache_client = None
    cache_module._redis_cache_initialized = False
    dashboard_module._cache.clear()
    yield
    cache_module._analysis_cache.clear()
    cache_module._redis_cache_client = None
    cache_module._redis_cache_initialized = False
    dashboard_module._cache.clear()


@pytest.mark.integration
def test_llm_cache_roundtrip_via_redis_l2(require_redis):
    client, prefix = require_redis
    # fingerprint 携带本轮 uuid 前缀 → L2 key 含 uuid，teardown 按前缀精确清理
    fingerprint = f"{prefix}redis-l2-fp-001"
    payload = {
        "analysis": {"root_cause": "redis-l2", "impact": "low", "fix": "none", "confidence": "low"},
        "cached": False,
    }

    cache_module._set_cache_result(fingerprint, payload)

    raw = client.get(f"ai-debug:llm:cache:{fingerprint}")
    assert raw is not None

    cache_module._analysis_cache.clear()
    restored = cache_module._get_cached_result(fingerprint)

    assert restored is not None
    assert restored["analysis"]["root_cause"] == "redis-l2"
    assert fingerprint in cache_module._analysis_cache


@pytest.mark.integration
def test_dashboard_cache_roundtrip_via_redis_l2(require_redis, monkeypatch):
    client, _prefix = require_redis
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

    # 生产按 limit 档位写 key：首次调用 limit=10 → tier=100 → _redis_cache_key(100)
    # （此前误读不带档位后缀的 _REDIS_CACHE_KEY，真实 Redis 下必然为 None）
    # _REDIS_CACHE_KEY 已被 fixture monkeypatch 为带本轮 uuid 前缀，key 含 uuid
    raw = client.get(dashboard_module._redis_cache_key(100))
    assert raw is not None

    # 清空 L1，第二次调用应可从 L2 回读
    dashboard_module._cache.clear()
    monkeypatch.setattr(dashboard_module.logs, "list_request_ids", lambda limit=100: [])
    monkeypatch.setattr(dashboard_module.errors, "list_recent", lambda limit=100: [])
    result2 = dashboard_module._collect_all_traces(limit=10)

    assert result2 == sample
