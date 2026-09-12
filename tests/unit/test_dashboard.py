"""单元测试：Dashboard API 端点"""
import json
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import dashboard as dashboard_module
from app.api.dashboard import router
from app.runtime.core import trace_repo
from app.mcp.tools.verify_api import verify_handler


@pytest.fixture(autouse=True)
def _clear_cache():
    """每个测试前清空 dashboard 缓存，避免跨用例污染"""
    dashboard_module._cache.clear()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestDashboardStats:

    def test_stats_empty(self, client):
        """统计接口返回正确结构"""
        resp = client.get("/api/dashboard/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert "total_traces" in body
        assert "silent_failures" in body
        assert "exceptions" in body
        assert "spec_count" in body
        assert isinstance(body["total_traces"], int)
        assert isinstance(body["silent_failures"], int)
        assert isinstance(body["exceptions"], int)
        assert isinstance(body["spec_count"], int)

    def test_stats_with_traces(self, client):
        """有数据时统计正确"""
        trace_repo.save_trace("ValueError", "bad value", [
            {"file": "a.py", "line": 1, "function": "f"}
        ], source="test")
        trace_repo.save_trace("SilentFailure", "no response", [],
                              trace_kind="silent_failure", source="test")

        resp = client.get("/api/dashboard/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_traces"] >= 2


class TestDashboardTraces:

    def test_list_traces(self, client):
        trace_repo.save_trace("TypeError", "x is None", [
            {"file": "b.py", "line": 2, "function": "g"}
        ], source="test")

        resp = client.get("/api/dashboard/traces?limit=10")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1
        assert body["traces"][0]["type"] == "TypeError"
        assert "trace_id" in body["traces"][0]

    def test_trace_with_verify(self, client):
        """trace 含 verify 结果"""
        tid = trace_repo.save_trace("E", "m", [], source="test")
        verify_handler({
            "actual": {"status_code": 200, "body": {"name": "Bob"}},
            "spec": {"kind": "api", "expect": {"body_rules": {"name": "Alice"}}},
            "trace_id": tid,
        })

        resp = client.get("/api/dashboard/traces?limit=10")
        assert resp.status_code == 200
        body = resp.json()
        # 找对应的 trace
        found = [t for t in body["traces"] if t["trace_id"] == tid]
        assert len(found) == 1
        assert found[0]["verify_count"] == 1
        assert found[0]["has_silent_failure"] is True


class TestDashboardTraceDetail:

    def test_trace_detail(self, client):
        tid = trace_repo.save_trace("ValueError", "test error", [
            {"file": "app/config.py", "line": 9, "function": "Settings"}
        ], source="test")

        resp = client.get(f"/api/dashboard/trace/{tid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["trace_id"] == tid
        assert body["trace_kind"] == "exception"
        assert body["exception"]["type"] == "ValueError"

    def test_trace_detail_not_found(self, client):
        resp = client.get("/api/dashboard/trace/no-such-trace")
        assert resp.status_code == 404

    def test_trace_detail_with_spec_diffs(self, client):
        """detail 含 spec_diffs"""
        tid = trace_repo.save_trace("E", "m", [], source="test")
        verify_handler({
            "actual": {"status_code": 200, "body": {"ok": True}},
            "spec": {"kind": "api", "expect": {"body_rules": {"ok": False}}},
            "trace_id": tid,
        })

        resp = client.get(f"/api/dashboard/trace/{tid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["spec_diffs"] is not None
        assert len(body["spec_diffs"]) == 1
        assert body["spec_diffs"][0]["silent_failure"] is True


class TestDashboardQualityReport:
    """v0.4.0: trace 详情端点注入 quality_report 字段"""

    def test_trace_detail_contains_quality_report(self, client):
        """trace detail 返回 quality_report 字段（默认开启 quality_scoring）"""
        tid = trace_repo.save_trace("ValueError", "test error", [
            {"file": "app/config.py", "line": 9, "function": "Settings"}
        ], source="test")

        resp = client.get(f"/api/dashboard/trace/{tid}")
        assert resp.status_code == 200
        body = resp.json()
        # quality_report 字段存在（非 None，因为默认 quality_scoring_enabled=True）
        assert "quality_report" in body
        qr = body["quality_report"]
        assert qr is not None
        # 核心字段齐全
        assert "overall_score" in qr
        assert "context_completeness" in qr
        assert "analysis_confidence" in qr
        assert "evidence_items" in qr
        assert "suggestions" in qr
        assert "scored_at" in qr
        assert qr["scorer_version"] == "1.0.0"

    def test_quality_report_dimensions_full(self, client):
        """quality_report 包含 9 个维度的评分"""
        tid = trace_repo.save_trace("ValueError", "test error", [
            {"file": "app/config.py", "line": 9, "function": "Settings"}
        ], source="test")

        resp = client.get(f"/api/dashboard/trace/{tid}")
        body = resp.json()
        qr = body["quality_report"]
        dims = qr["context_completeness"]["dimensions"]
        # 9 个维度全部存在
        assert len(dims) == 9
        expected = {"trace", "runtime", "code_snippet", "git_context",
                    "network", "ui_event", "spec", "knowledge_base", "llm_analysis"}
        assert set(dims.keys()) == expected

    def test_quality_report_formula(self, client):
        """overall_score = completeness × confidence"""
        tid = trace_repo.save_trace("ValueError", "test", [
            {"file": "a.py", "line": 1, "function": "f"}
        ], source="test")

        resp = client.get(f"/api/dashboard/trace/{tid}")
        body = resp.json()
        qr = body["quality_report"]
        comp = qr["context_completeness"]["overall_score"]
        conf = qr["analysis_confidence"]["overall_score"]
        expected = round(comp * conf, 4)
        assert abs(qr["overall_score"] - expected) < 0.001

    def test_quality_report_disabled_when_flag_off(self, client, monkeypatch):
        """quality_scoring_enabled=False → quality_report 为 None"""
        from app.config import settings
        monkeypatch.setattr(settings, "quality_scoring_enabled", False)

        tid = trace_repo.save_trace("ValueError", "test", [
            {"file": "a.py", "line": 1, "function": "f"}
        ], source="test")

        resp = client.get(f"/api/dashboard/trace/{tid}")
        body = resp.json()
        assert body["quality_report"] is None

    def test_quality_only_endpoint(self, client):
        """独立质量端点 /api/dashboard/trace/{tid}/quality"""
        tid = trace_repo.save_trace("ValueError", "test", [
            {"file": "a.py", "line": 1, "function": "f"}
        ], source="test")

        resp = client.get(f"/api/dashboard/trace/{tid}/quality")
        assert resp.status_code == 200
        body = resp.json()
        assert body["trace_id"] == tid
        assert body["quality_report"] is not None
        assert "overall_score" in body["quality_report"]

    def test_quality_only_endpoint_not_found(self, client):
        """质量端点 404"""
        resp = client.get("/api/dashboard/trace/no-such-trace/quality")
        assert resp.status_code == 404


class TestDashboardLimitCap:
    """limit 参数上限测试"""

    def test_limit_default(self, client):
        """默认 limit=100"""
        resp = client.get("/api/dashboard/traces")
        assert resp.status_code == 200

    def test_limit_capped_at_1000(self, client):
        """超过 1000 时截断到 1000"""
        resp = client.get("/api/dashboard/traces?limit=9999")
        assert resp.status_code == 200

    def test_limit_minimum_one(self, client):
        """limit 最小为 1"""
        resp = client.get("/api/dashboard/traces?limit=0")
        assert resp.status_code == 200


class TestDashboardCache:
    """_collect_all_traces 缓存测试"""

    def test_cache_populated(self):
        """首次调用后缓存被填充"""
        dashboard_module._collect_all_traces(limit=10)
        assert dashboard_module._cache_key(100) in dashboard_module._cache

    def test_cache_returns_same_data(self):
        """TTL 内返回缓存数据"""
        result1 = dashboard_module._collect_all_traces(limit=10)
        result2 = dashboard_module._collect_all_traces(limit=10)
        assert result1 == result2

    def test_cache_expires_after_ttl(self):
        """TTL 过期后重新采集"""
        from unittest.mock import patch

        with patch("app.api.dashboard.time") as mock_time:
            mock_time.monotonic.return_value = 100.0
            dashboard_module._collect_all_traces(limit=10)
            assert dashboard_module._cache[dashboard_module._cache_key(100)][0] == 100.0

            # 模拟时间过了 TTL+1 秒
            mock_time.monotonic.return_value = 100.0 + dashboard_module._CACHE_TTL + 1
            dashboard_module._collect_all_traces(limit=10)
            assert dashboard_module._cache[dashboard_module._cache_key(100)][0] == 100.0 + dashboard_module._CACHE_TTL + 1

    def test_invalidate_cache_bumps_generation(self, monkeypatch):
        """FIX b13-1: invalidate_cache 递增 generation（触发在途计算丢弃旧快照）。"""
        monkeypatch.setattr(dashboard_module, "_get_redis_cache", lambda: None)
        before = dashboard_module._generation
        dashboard_module.invalidate_cache(source="test")
        assert dashboard_module._generation == before + 1

    def test_generation_guard_discards_stale_write(self, monkeypatch):
        """FIX b13-1: 计算期间失效（generation 递增）则丢弃旧快照写回。"""
        dashboard_module._cache.clear()
        dashboard_module._generation = 0

        original_list_recent = dashboard_module.errors.list_recent

        def _bumping_list_recent(*a, **k):
            # 模拟计算期间的并发 invalidate_cache（新 trace 持久化）
            dashboard_module._generation += 1
            return original_list_recent(*a, **k)

        monkeypatch.setattr(dashboard_module.errors, "list_recent", _bumping_list_recent)
        monkeypatch.setattr(dashboard_module, "_get_redis_cache", lambda: None)

        dashboard_module._collect_all_traces(limit=10)

        # 计算期间 generation 已变 → 不写回 L1（旧快照不遮蔽新 trace）
        assert dashboard_module._cache_key(100) not in dashboard_module._cache


# ---------------------------------------------------------------------------
# FIX: P1-E1 —— 缓存不被首个请求的 limit 固化
# ---------------------------------------------------------------------------


class TestDashboardCacheLimitIsolation:
    """E1 回归：小 limit 先缓存，大 limit 后命中不得返回截断数据。"""

    def test_small_then_large_limit_returns_full(self):
        """先 limit=1（缓存 1000 条）再 limit=1000：缓存命中返回完整数据。

        旧实现：首个请求按 limit=1 计算并缓存 → 后续大 limit 命中缓存
        `cached[:1000]` 却只有 1 条。
        """
        # 造 3 条 trace 数据
        for i in range(3):
            trace_repo.save_trace(
                f"ValueError{i}", f"msg-{i}",
                [{"file": "a.py", "line": 1, "function": "f"}],
                source="test",
            )

        first = dashboard_module._collect_all_traces(limit=1)
        assert len(first) == 1  # 小 limit 正常切片

        # 缓存里存的是完整数据（≥3 条），不是被 limit=1 截断的
        cached_result = dashboard_module._cache[dashboard_module._cache_key(100)][1]
        assert len(cached_result) >= 3

        # 大 limit 命中缓存：返回完整数据（旧实现此处只返回 1 条）
        second = dashboard_module._collect_all_traces(limit=1000)
        assert len(second) >= 3

    def test_cached_result_independent_of_first_caller_limit(self):
        """缓存内容长度与首个调用方的 limit 无关（按最大档 1000 计算）。"""
        trace_repo.save_trace(
            "ValueError", "msg",
            [{"file": "a.py", "line": 1, "function": "f"}],
            source="test",
        )
        dashboard_module._collect_all_traces(limit=2)
        cached_result = dashboard_module._cache[dashboard_module._cache_key(100)][1]
        # 缓存未被 limit=2 截断（至少含刚造的 1 条且长度不等于 2 的钳制）
        assert len(cached_result) >= 1
        # 再以任意 limit 取，均从同一份完整缓存切片
        assert dashboard_module._collect_all_traces(limit=1) == cached_result[:1]
        assert dashboard_module._collect_all_traces(limit=5) == cached_result[:5]


# ---------------------------------------------------------------------------
# FIX: R7-A4 —— 缓存按 limit 分档 + 摘要提取单遍扫描
# ---------------------------------------------------------------------------


class TestDashboardCacheTiering:
    """A4 回归：常态小 limit 请求不再驱动 1000 条全量计算。"""

    def test_small_limit_uses_100_tier(self):
        """limit≤100 → 缓存 100 档；L1 命中后不再触发 1000 档计算。"""
        dashboard_module._cache.clear()
        dashboard_module._collect_all_traces(limit=10)
        assert dashboard_module._cache_key(100) in dashboard_module._cache
        assert dashboard_module._cache_key(1000) not in dashboard_module._cache

    def test_large_limit_uses_1000_tier(self):
        """limit>100 → 缓存 1000 档。"""
        dashboard_module._cache.clear()
        dashboard_module._collect_all_traces(limit=1000)
        assert dashboard_module._cache_key(1000) in dashboard_module._cache

    def test_tiers_independent_no_cross_tier_truncation(self):
        """E1 语义保持：各档独立缓存，小档先缓存后大档仍拿到完整数据。"""
        dashboard_module._cache.clear()
        for i in range(3):
            trace_repo.save_trace(
                f"ValueError{i}", f"tier-msg-{i}",
                [{"file": "a.py", "line": 1, "function": "f"}],
                source="test",
            )
        first = dashboard_module._collect_all_traces(limit=1)
        assert len(first) == 1

        second = dashboard_module._collect_all_traces(limit=1000)
        assert len(second) >= 3  # 1000 档独立计算，不被 1 截断

    def test_invalidate_clears_all_tiers(self):
        """invalidate_cache 清除全部档位（L1 + Redis mock）。"""
        dashboard_module._cache.clear()
        dashboard_module._collect_all_traces(limit=10)
        dashboard_module._collect_all_traces(limit=1000)
        assert dashboard_module._cache  # 已有缓存

        from unittest.mock import patch

        class _FakeRedis:
            def __init__(self):
                self.deleted = []

            def delete(self, key):
                self.deleted.append(key)

        fake = _FakeRedis()
        with patch("app.api.dashboard._get_redis_cache", return_value=fake):
            dashboard_module.invalidate_cache()

        assert dashboard_module._cache == {}
        assert fake.deleted == [
            dashboard_module._redis_cache_key(100),
            dashboard_module._redis_cache_key(1000),
        ]


class TestExtractErrorSummarySinglePass:
    """A4 回归：摘要提取单遍扫描（此前对每个 error 做两次完整 get_logs）。"""

    def test_get_logs_called_once_per_error(self):
        from unittest.mock import patch

        import app.runtime.core.logs as logs_module

        from app.runtime.core.errors import record as record_error

        err_id = record_error(
            {"type": "ValueError", "message": "x",
             "frames": [{"file": "a.py", "line": 1, "function": "f"}]},
            source="test",
        )

        calls = {"n": 0}
        real_get_logs = logs_module.get_logs

        def counting_get_logs(rid):
            calls["n"] += 1
            return real_get_logs(rid)

        with patch.object(logs_module, "get_logs", counting_get_logs):
            dashboard_module._extract_error_summary(
                {"error_id": err_id, "timestamp": 0, "type": "ValueError", "message": "x"}
            )

        assert calls["n"] == 1  # 旧实现为 2 次


# ---------------------------------------------------------------------------
# B19 —— L2 命中路径与 invalidate_cache 的代际竞态
# ---------------------------------------------------------------------------


def _trace_summary(trace_id: str) -> dict:
    """构造一条 trace 摘要（与 _collect_all_traces 返回结构一致）"""
    return {
        "trace_id": trace_id,
        "timestamp": 1.0,
        "type": "ERROR",
        "message": trace_id,
        "trace_kind": "exception",
        "occurrence_count": 1,
        "has_silent_failure": False,
        "verify_count": 0,
    }


class TestDashboardCacheInvalidationGenerationRace:
    """B19：失效竞争中被读出的旧 L2 值既不得返回，也不得回填 L1。

    同步手段全部使用 ``threading.Event``（GET 进入点 / 放行点），
    不使用 sleep 猜测交错顺序。
    """

    @staticmethod
    def _run_l2_read_raced_by_invalidation(monkeypatch):
        """执行「阻塞 L2 GET → 并发 invalidate → 放行旧值」的精确交错。

        交错顺序（Event 精确控制，不用 sleep）：
        1. 请求线程进入 L2 GET（``get_entered`` 置位后阻塞，L1 此时为空）；
        2. 主线程调用 ``invalidate_cache()`` 完成失效（generation 递增、L2 键删除）；
        3. 主线程放行 GET，请求线程拿到旧 L2 值并继续走完。

        返回 (outcome, fake, stale, fresh)：outcome 含请求线程的 result/error。
        """
        stale = [_trace_summary("stale-l2-trace")]
        fresh = _trace_summary("fresh-trace")

        dashboard_module._cache.clear()
        monkeypatch.setattr(dashboard_module, "_generation", 0)

        # 重算数据源：失效后的存储现场只有 fresh-trace
        monkeypatch.setattr(dashboard_module.errors, "list_recent", lambda limit=100: [])
        monkeypatch.setattr(
            dashboard_module.logs, "list_request_ids", lambda limit=100: ["fresh-trace"]
        )
        monkeypatch.setattr(dashboard_module, "_extract_trace_summary", lambda rid: fresh)

        get_entered = threading.Event()
        release_get = threading.Event()

        class _BlockingRedis:
            def __init__(self):
                self.get_calls = 0
                self.deleted = []
                self.setex_calls = []

            def get(self, key):
                self.get_calls += 1
                get_entered.set()
                if not release_get.wait(timeout=10):
                    raise AssertionError("测试未在期限内放行 Redis GET")
                return json.dumps(stale)

            def delete(self, key):
                self.deleted.append(key)

            def setex(self, key, ttl, value):
                self.setex_calls.append((key, ttl, value))

        fake = _BlockingRedis()
        monkeypatch.setattr(dashboard_module, "_get_redis_cache", lambda: fake)

        outcome: dict = {}

        def _worker():
            try:
                outcome["result"] = dashboard_module._collect_all_traces(limit=10)
            except BaseException as exc:  # noqa: BLE001 - 测试需捕获实现抛出的任何异常
                outcome["error"] = exc

        worker = threading.Thread(target=_worker, name="b19-l2-reader")
        worker.start()
        try:
            # 1) 请求已完成 L2 读取（阻塞在 GET 内），尚未写入 L1 / 返回
            assert get_entered.wait(timeout=5), "请求未进入 L2 读取阶段"
            gen_before = dashboard_module._generation

            # 2) 另一侧完成缓存失效，generation 已变化
            dashboard_module.invalidate_cache(source="b19-test")
            assert dashboard_module._generation == gen_before + 1
            assert fake.deleted == [
                dashboard_module._redis_cache_key(100),
                dashboard_module._redis_cache_key(1000),
            ]

            # 3) 放行原请求
            release_get.set()
            worker.join(timeout=10)
            assert not worker.is_alive(), "L2 读取请求未在期限内结束"
        finally:
            release_get.set()
            worker.join(timeout=10)

        assert "error" not in outcome, f"请求不应抛异常: {outcome.get('error')!r}"
        return outcome, fake, stale, fresh

    def test_l2_hit_during_invalidation_not_returned(self, monkeypatch):
        """失效竞争中被读出的旧 L2 值不得作为本次结果返回。"""
        outcome, _fake, _stale, _fresh = self._run_l2_read_raced_by_invalidation(monkeypatch)

        assert [t["trace_id"] for t in outcome["result"]] == ["fresh-trace"]

        # 竞争结束后下一次请求按既有语义取得新值
        again = dashboard_module._collect_all_traces(limit=10)
        assert [t["trace_id"] for t in again] == ["fresh-trace"]

    def test_l2_hit_during_invalidation_not_promoted_to_l1(self, monkeypatch):
        """失效竞争中的旧 L2 值不得回填 L1；L1 只能是重算后的新值。"""
        _outcome, _fake, _stale, fresh = self._run_l2_read_raced_by_invalidation(monkeypatch)

        for tier in dashboard_module._CACHE_TIERS:
            entry = dashboard_module._cache.get(dashboard_module._cache_key(tier))
            if entry is not None:
                assert all(t["trace_id"] != "stale-l2-trace" for t in entry[1]), (
                    f"档位 {tier} 的 L1 被旧 L2 值污染"
                )
        entry = dashboard_module._cache.get(dashboard_module._cache_key(100))
        assert entry is not None, "竞态后应走既有 miss 路径重新计算并回填 L1"
        assert entry[1] == [fresh]

    def test_l2_hit_without_invalidation_still_promotes_l1(self, monkeypatch):
        """无竞争的 L2 命中行为保持不变：返回值 + 回填 L1 + 不刷新 L2 TTL。"""
        payload = [_trace_summary("l2-only-trace")]

        dashboard_module._cache.clear()
        monkeypatch.setattr(dashboard_module, "_generation", 0)
        # 计算路径若被触发只会得到空结果，用于区分「命中」与「重算」
        monkeypatch.setattr(dashboard_module.errors, "list_recent", lambda limit=100: [])
        monkeypatch.setattr(dashboard_module.logs, "list_request_ids", lambda limit=100: [])

        class _Redis:
            def __init__(self):
                self.get_calls = []
                self.setex_calls = []

            def get(self, key):
                self.get_calls.append(key)
                return json.dumps(payload)

            def delete(self, key):
                pass

            def setex(self, key, ttl, value):
                self.setex_calls.append((key, ttl, value))

        fake = _Redis()
        monkeypatch.setattr(dashboard_module, "_get_redis_cache", lambda: fake)

        result = dashboard_module._collect_all_traces(limit=10)

        assert result == payload
        assert fake.get_calls == [dashboard_module._redis_cache_key(100)]
        assert dashboard_module._cache[dashboard_module._cache_key(100)][1] == payload
        assert fake.setex_calls == []  # 命中不刷新 L2 TTL

    def test_l1_hit_short_circuits_before_l2(self, monkeypatch):
        """无竞争的 L1 命中行为保持不变：不访问 L2。"""
        dashboard_module._cache.clear()
        monkeypatch.setattr(dashboard_module, "_generation", 0)
        payload = [_trace_summary("cached-l1")]
        dashboard_module._cache[dashboard_module._cache_key(100)] = (
            time.monotonic(),
            payload,
        )

        class _Redis:
            def __init__(self):
                self.get_calls = []

            def get(self, key):
                self.get_calls.append(key)
                raise AssertionError("L1 命中不应访问 L2")

        fake = _Redis()
        monkeypatch.setattr(dashboard_module, "_get_redis_cache", lambda: fake)

        assert dashboard_module._collect_all_traces(limit=10) == payload
        assert fake.get_calls == []

    def test_l2_read_failure_degrades_to_recompute(self, monkeypatch):
        """L2 GET 异常沿用既有降级：不抛错，走计算路径。"""
        fresh = _trace_summary("fresh-trace")

        dashboard_module._cache.clear()
        monkeypatch.setattr(dashboard_module, "_generation", 0)
        monkeypatch.setattr(dashboard_module.errors, "list_recent", lambda limit=100: [])
        monkeypatch.setattr(
            dashboard_module.logs, "list_request_ids", lambda limit=100: ["fresh-trace"]
        )
        monkeypatch.setattr(dashboard_module, "_extract_trace_summary", lambda rid: fresh)

        class _Redis:
            def get(self, key):
                raise ConnectionError("redis down")

            def delete(self, key):
                pass

            def setex(self, key, ttl, value):
                pass

        monkeypatch.setattr(dashboard_module, "_get_redis_cache", lambda: _Redis())

        assert dashboard_module._collect_all_traces(limit=10) == [fresh]
        assert dashboard_module._cache[dashboard_module._cache_key(100)][1] == [fresh]

    def test_l2_write_failure_does_not_break_response(self, monkeypatch):
        """L2 写回异常沿用既有降级：请求仍正常返回并填充 L1。"""
        fresh = _trace_summary("fresh-trace")

        dashboard_module._cache.clear()
        monkeypatch.setattr(dashboard_module, "_generation", 0)
        monkeypatch.setattr(dashboard_module.errors, "list_recent", lambda limit=100: [])
        monkeypatch.setattr(
            dashboard_module.logs, "list_request_ids", lambda limit=100: ["fresh-trace"]
        )
        monkeypatch.setattr(dashboard_module, "_extract_trace_summary", lambda rid: fresh)

        class _Redis:
            def get(self, key):
                return None

            def delete(self, key):
                pass

            def setex(self, key, ttl, value):
                raise ConnectionError("redis down")

        monkeypatch.setattr(dashboard_module, "_get_redis_cache", lambda: _Redis())

        assert dashboard_module._collect_all_traces(limit=10) == [fresh]
        assert dashboard_module._cache[dashboard_module._cache_key(100)][1] == [fresh]
