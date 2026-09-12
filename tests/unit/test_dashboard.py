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


class _PromotionWatchingL1Cache(dict):
    """测试用 L1 替身：在真实写入前阻塞，并记录提交瞬间的 generation。

    只在 ``__setitem__``（生产代码的 ``_cache[key] = ...``）前提供一个确定性
    同步点，不改变 dict 语义；其他读写（get/pop/clear）沿用 dict 行为。
    """

    def __init__(self, write_entered: threading.Event, allow_write: threading.Event):
        super().__init__()
        self._write_entered = write_entered
        self._allow_write = allow_write
        self.commit_generations: list = []

    def __setitem__(self, key, value):
        self._write_entered.set()
        if not self._allow_write.wait(timeout=10):
            raise AssertionError("测试未放行 L1 回填")
        # 记录真实提交瞬间的 generation：若「校验」与「提交」不在同一临界区，
        # 两者观察到的 generation 会不一致。
        self.commit_generations.append(dashboard_module._generation)
        super().__setitem__(key, value)


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

    @staticmethod
    def _race_l1_commit_with_invalidation(monkeypatch):
        """让请求阻塞在 L1 提交处，并发执行 invalidate_cache()。

        精确交错（Event + 锁探测，无 sleep）：
        1. worker 已通过 generation 校验，进入 L1 回填；
        2. 回填被测试缓存阻塞（``write_entered`` 置位后等待放行）；
        3. 另一线程执行 invalidate_cache()（清 L1 + generation++ + 删 L2）；
        4. 探测 worker 是否正持有临界区锁：持锁则失效必须排队（修复后），
           无锁则失效会先于回填提交完成（修复前）；
        5. 放行回填，等待两侧结束。

        返回 (outcome, cache, lock_free_during_promotion)。
        """
        write_entered = threading.Event()
        allow_write = threading.Event()
        cache = _PromotionWatchingL1Cache(write_entered, allow_write)
        monkeypatch.setattr(dashboard_module, "_cache", cache)

        invalidation_done = threading.Event()
        outcome: dict = {}

        def _worker():
            try:
                outcome["result"] = dashboard_module._collect_all_traces(limit=10)
            except BaseException as exc:  # noqa: BLE001 - 测试需捕获实现抛出的任何异常
                outcome["error"] = exc

        def _invalidate():
            try:
                dashboard_module.invalidate_cache(source="b19-atomic")
            finally:
                invalidation_done.set()

        worker = threading.Thread(target=_worker, name="b19-promotion")
        invalidator = None
        worker.start()
        try:
            assert write_entered.wait(timeout=5), "worker 未进入 L1 回填阶段"

            invalidator = threading.Thread(target=_invalidate, name="b19-invalidate")
            invalidator.start()

            lock = getattr(dashboard_module, "_cache_lock", None)
            lock_free = True
            if lock is not None:
                if lock.acquire(blocking=False):
                    lock.release()
                else:
                    lock_free = False
            if lock_free:
                # 修复前：失效不受临界区阻塞，会在回填提交前整体完成
                assert invalidation_done.wait(timeout=5), "失效未在期限内完成"
        finally:
            allow_write.set()
            worker.join(timeout=10)
            if invalidator is not None:
                invalidator.join(timeout=10)

        assert not worker.is_alive(), "worker 未在期限内结束"
        assert invalidator is not None and not invalidator.is_alive(), "失效线程未在期限内结束"
        assert "error" not in outcome, f"请求不应抛异常: {outcome.get('error')!r}"
        return outcome, cache, lock_free

    def _l2_promotion_race(self, monkeypatch):
        """L2 命中路径残留竞态现场：L2 返回旧值，计算路径无数据。"""
        stale = [_trace_summary("stale-l2-trace")]

        monkeypatch.setattr(dashboard_module, "_generation", 0)
        monkeypatch.setattr(dashboard_module.errors, "list_recent", lambda limit=100: [])
        monkeypatch.setattr(dashboard_module.logs, "list_request_ids", lambda limit=100: [])

        class _Redis:
            def __init__(self):
                self.deleted = []

            def get(self, key):
                return json.dumps(stale)

            def delete(self, key):
                self.deleted.append(key)

            def setex(self, key, ttl, value):
                pass

        monkeypatch.setattr(dashboard_module, "_get_redis_cache", lambda: _Redis())
        return self._race_l1_commit_with_invalidation(monkeypatch)

    def _compute_writeback_race(self, monkeypatch):
        """计算路径残留竞态现场：L2 miss，计算得到 fresh-trace。"""
        fresh = _trace_summary("fresh-trace")

        monkeypatch.setattr(dashboard_module, "_generation", 0)
        monkeypatch.setattr(dashboard_module.errors, "list_recent", lambda limit=100: [])
        monkeypatch.setattr(
            dashboard_module.logs, "list_request_ids", lambda limit=100: ["fresh-trace"]
        )
        monkeypatch.setattr(dashboard_module, "_extract_trace_summary", lambda rid: fresh)

        class _Redis:
            def __init__(self):
                self.setex_calls = []

            def get(self, key):
                return None

            def delete(self, key):
                pass

            def setex(self, key, ttl, value):
                self.setex_calls.append((key, ttl, value))

        monkeypatch.setattr(dashboard_module, "_get_redis_cache", lambda: _Redis())
        return self._race_l1_commit_with_invalidation(monkeypatch)

    def test_l2_promotion_commit_is_atomic_with_generation_check(self, monkeypatch):
        """残留竞态：L2 命中的 generation 校验与 L1 回填必须在同一临界区。

        修复前 invalidate_cache() 可在「校验通过」与「写 L1」之间整体完成，
        提交瞬间的 generation（1）已不等于校验时的值（0）。
        """
        _outcome, cache, _lock_free = self._l2_promotion_race(monkeypatch)

        assert cache.commit_generations == [0]

    def test_l2_promotion_after_invalidation_does_not_pollute_l1(self, monkeypatch):
        """残留竞态：失效先完成时，旧 L2 值不得被写进失效后的 L1。"""
        _outcome, cache, _lock_free = self._l2_promotion_race(monkeypatch)

        entry = cache.get(dashboard_module._cache_key(100))
        assert entry is None or all(t["trace_id"] != "stale-l2-trace" for t in entry[1])

    def test_l2_promotion_after_invalidation_does_not_return_old_value(self, monkeypatch):
        """残留竞态：失效先完成时，旧 L2 值不得作为本次请求结果返回。"""
        outcome, cache, lock_free = self._l2_promotion_race(monkeypatch)

        if lock_free:
            # 修复前：失效不受临界区阻塞，在回填提交前整体完成 → 不得返回旧值
            assert all(t["trace_id"] != "stale-l2-trace" for t in outcome["result"])
        else:
            # 修复后：失效只能排在回填之后，本次返回旧值属「读取先于失效」的
            # 合法线性化；此时仍要求校验与提交同代际。
            assert cache.commit_generations == [0]

    def test_compute_writeback_commit_is_atomic_with_generation_check(self, monkeypatch):
        """同类残留竞态：计算路径的 generation 校验与 L1 写回必须在同一临界区。"""
        _outcome, cache, _lock_free = self._compute_writeback_race(monkeypatch)

        assert cache.commit_generations == [0]

    def test_compute_writeback_after_invalidation_does_not_pollute_l1(self, monkeypatch):
        """同类残留竞态：失效先完成时，计算的旧快照不得被写进失效后的 L1。"""
        _outcome, cache, _lock_free = self._compute_writeback_race(monkeypatch)

        assert cache.get(dashboard_module._cache_key(100)) is None

    def test_l2_hit_rejected_while_invalidation_delete_pending(self, monkeypatch):
        """终审问题一：generation 已递增、Redis delete 未完成时，旧 L2 不得被接受。

        修复前：请求在 delete 完成前读到旧 L2，因 gen_l2 已是新代际而通过检查，
        旧值被写进 L1 并返回（L2 尚未删除的窗口被当成有效命中）。

        精确交错（Event 控制，无 sleep）：
        1. invalidate_cache() 进入 fake Redis delete() 并阻塞（delete_entered 置位）；
        2. 此时 generation 已递增、L1 已清空；
        3. 主线程调用 _collect_all_traces()，fake get() 返回旧 L2 payload；
        4. 断言旧值不返回、不写 L1；
        5. 放行 delete、join 失效线程，确认随后缓存恢复健康。
        """
        stale = [_trace_summary("stale-l2")]
        fresh = _trace_summary("fresh-trace")

        monkeypatch.setattr(dashboard_module, "_generation", 0)
        # 失效后的存储现场只有 fresh-trace
        monkeypatch.setattr(dashboard_module.errors, "list_recent", lambda limit=100: [])
        monkeypatch.setattr(
            dashboard_module.logs, "list_request_ids", lambda limit=100: ["fresh-trace"]
        )
        monkeypatch.setattr(dashboard_module, "_extract_trace_summary", lambda rid: fresh)

        delete_entered = threading.Event()
        release_delete = threading.Event()
        l2 = {
            dashboard_module._redis_cache_key(100): json.dumps(stale),
            dashboard_module._redis_cache_key(1000): json.dumps(stale),
        }

        class _Redis:
            def get(self, key):
                return l2.get(key)

            def delete(self, key):
                delete_entered.set()
                if not release_delete.wait(timeout=10):
                    raise AssertionError("测试未放行 Redis delete")
                l2.pop(key, None)

            def setex(self, key, ttl, value):
                l2[key] = value

        monkeypatch.setattr(dashboard_module, "_get_redis_cache", lambda: _Redis())

        def _invalidate():
            dashboard_module.invalidate_cache(source="b19-l2-delete-window")

        invalidator = threading.Thread(target=_invalidate, name="b19-invalidate")
        invalidator.start()
        try:
            assert delete_entered.wait(timeout=5), "invalidate 未进入 Redis delete"
            # delete 阻塞期间 generation 已递增（清 L1 + gen++ 已完成）
            assert dashboard_module._generation == 1

            returned = dashboard_module._collect_all_traces(limit=10)
        finally:
            release_delete.set()
            invalidator.join(timeout=10)

        assert not invalidator.is_alive(), "失效线程未在期限内结束"

        # 旧值不得作为本次结果返回
        assert [t["trace_id"] for t in returned] == ["fresh-trace"]
        # 旧值不得写入 L1（任何档位）
        for tier in dashboard_module._CACHE_TIERS:
            entry = dashboard_module._cache.get(dashboard_module._cache_key(tier))
            if entry is not None:
                assert all(t["trace_id"] != "stale-l2" for t in entry[1]), (
                    f"档位 {tier} 的 L1 被旧 L2 值污染"
                )

        # 失效结束后缓存恢复健康：下一次请求按 miss 路径重算并回填
        again = dashboard_module._collect_all_traces(limit=10)
        assert [t["trace_id"] for t in again] == ["fresh-trace"]
        assert dashboard_module._cache[dashboard_module._cache_key(100)][1] == [fresh]

    @staticmethod
    def _race_setex_with_invalidation(monkeypatch):
        """让计算线程阻塞在 L2 setex，并发执行 invalidate_cache()。

        精确交错（Event + 有界探测，无 sleep）：
        1. 首次请求完成计算并进入 setex（fake setex 阻塞，setex_entered 置位）；
        2. 另一线程执行 invalidate_cache()（清 L1、generation++、删 L2）；
        3. 探测失效是否在放行 setex 前完成：修复前不等待在途 setex，
           会在期限内完成；修复后必须等 setex 结束，期限内不可能完成；
        4. 放行 setex 并 join 两侧。

        返回 (outcome, l2, state, invalidation_finished_before_release)。
        """
        old_compute = _trace_summary("old-compute")

        monkeypatch.setattr(dashboard_module, "_generation", 0)
        monkeypatch.setattr(dashboard_module.errors, "list_recent", lambda limit=100: [])
        monkeypatch.setattr(
            dashboard_module.logs, "list_request_ids", lambda limit=100: ["trace-1"]
        )
        state = {"summary": old_compute}
        monkeypatch.setattr(dashboard_module, "_extract_trace_summary", lambda rid: state["summary"])

        setex_entered = threading.Event()
        release_setex = threading.Event()
        l2: dict = {}

        class _Redis:
            def get(self, key):
                return l2.get(key)

            def delete(self, key):
                l2.pop(key, None)

            def setex(self, key, ttl, value):
                payload = json.loads(value)
                if payload and payload[0]["trace_id"] == "old-compute":
                    setex_entered.set()
                    if not release_setex.wait(timeout=10):
                        raise AssertionError("测试未放行 L2 setex")
                l2[key] = value

        monkeypatch.setattr(dashboard_module, "_get_redis_cache", lambda: _Redis())

        outcome: dict = {}

        def _worker():
            try:
                outcome["result"] = dashboard_module._collect_all_traces(limit=10)
            except BaseException as exc:  # noqa: BLE001 - 测试需捕获实现抛出的任何异常
                outcome["error"] = exc

        invalidator_done = threading.Event()

        def _invalidate():
            try:
                dashboard_module.invalidate_cache(source="b19-l2-setex")
            finally:
                invalidator_done.set()

        worker = threading.Thread(target=_worker, name="b19-compute")
        invalidator = None
        worker.start()
        try:
            assert setex_entered.wait(timeout=5), "计算线程未进入 L2 setex"

            invalidator = threading.Thread(target=_invalidate, name="b19-invalidate")
            invalidator.start()
            # 有界探测：修复后失效必须等在途 setex，期限内不可能完成；
            # 修复前无等待，会在放行 setex 之前整体完成（含 Redis 删除）。
            invalidation_finished_before_release = invalidator_done.wait(timeout=2)
        finally:
            release_setex.set()
            worker.join(timeout=10)
            if invalidator is not None:
                invalidator.join(timeout=10)

        assert not worker.is_alive(), "计算线程未在期限内结束"
        assert invalidator is not None and not invalidator.is_alive(), "失效线程未在期限内结束"
        assert "error" not in outcome, f"请求不应抛异常: {outcome.get('error')!r}"
        return outcome, l2, state, invalidation_finished_before_release

    def test_invalidation_waits_for_inflight_setex(self, monkeypatch):
        """终审问题二（协调机制）：invalidate_cache 必须等已登记的 setex 结束再删 L2。

        修复前失效不等待在途 setex，会在 setex 完成前结束（旧值随后写回 L2）。
        """
        _outcome, _l2, _state, invalidation_finished_before_release = (
            self._race_setex_with_invalidation(monkeypatch)
        )

        assert not invalidation_finished_before_release, (
            "invalidate_cache 不得在在途 setex 完成前结束（否则旧值会在 delete 后写回 L2）"
        )

    def test_old_compute_setex_cannot_resurrect_l2_after_invalidation(self, monkeypatch):
        """终审问题二（后果）：失效完成后，旧计算结果不得重新进入 L1/L2 或返回。

        修复前：delete 先执行、旧 setex 后写回 → 下一请求从 L2 读回 old-compute。
        """
        outcome, l2, state, _finished = self._race_setex_with_invalidation(monkeypatch)

        assert [t["trace_id"] for t in outcome["result"]] == ["old-compute"]

        # 旧计算结果不得残留在 L2（修复前：setex 在 delete 之后写回）
        for tier in dashboard_module._CACHE_TIERS:
            raw = l2.get(dashboard_module._redis_cache_key(tier))
            if raw:
                assert all(t["trace_id"] != "old-compute" for t in json.loads(raw)), (
                    f"L2 档位 {tier} 残留失效前的旧计算结果"
                )
        # 旧值不得残留在 L1
        for tier in dashboard_module._CACHE_TIERS:
            entry = dashboard_module._cache.get(dashboard_module._cache_key(tier))
            if entry is not None:
                assert all(t["trace_id"] != "old-compute" for t in entry[1]), (
                    f"L1 档位 {tier} 残留失效前的旧计算结果"
                )

        # 失效后的数据源已变化：下一次请求不得从 L2/缓存拿到 old-compute
        state["summary"] = _trace_summary("new-data")
        again = dashboard_module._collect_all_traces(limit=10)
        assert [t["trace_id"] for t in again] == ["new-data"]

    def test_l2_read_started_during_invalidation_rejected_after_delete(self, monkeypatch):
        """终审遗漏交错：GET 始于失效期间、最终判定晚于 delete 完成时，旧值仍须拒绝。

        修复前：reader 在失效期间快照到新代际，GET 在 delete 前取到旧值；等
        invalidate_cache() 完成（_l2_invalidating 归零）后 promotion 判定只看
        当前状态，于是「代际相等 + 当前不在失效中」成立 → 旧值被回填 L1 并返回。

        精确交错（Event 控制，无 sleep）：
        1. fake delete() 阻塞（delete_entered 置位）；
        2. 启动 invalidate_cache()；
        3. 确认 generation 已递增、_l2_invalidating > 0（reader 尚未开始）；
        4. 启动 reader；
        5. reader 的 fake get() 阻塞（get_entered 置位），旧值已在途；
        6. 放行 delete，等 invalidate_cache() 完全结束（_l2_invalidating 归零）；
        7. 此时 reader 仍停在 GET 内；
        8. 放行 get，返回旧 L2 payload，等待 reader 结束；
        9. 断言旧值不返回、不回填 L1，且按既有 miss 路径重算。
        """
        stale = [_trace_summary("stale-during-invalidation")]
        fresh = _trace_summary("fresh-trace")

        monkeypatch.setattr(dashboard_module, "_generation", 0)
        # 重算数据源：失效后的存储现场只有 fresh-trace
        monkeypatch.setattr(dashboard_module.errors, "list_recent", lambda limit=100: [])
        monkeypatch.setattr(
            dashboard_module.logs, "list_request_ids", lambda limit=100: ["fresh-trace"]
        )
        monkeypatch.setattr(dashboard_module, "_extract_trace_summary", lambda rid: fresh)

        delete_entered = threading.Event()
        release_delete = threading.Event()
        get_entered = threading.Event()
        release_get = threading.Event()

        class _Redis:
            def get(self, key):
                get_entered.set()
                if not release_get.wait(timeout=10):
                    raise AssertionError("测试未放行 Redis GET")
                return json.dumps(stale)

            def delete(self, key):
                delete_entered.set()
                if not release_delete.wait(timeout=10):
                    raise AssertionError("测试未放行 Redis delete")

            def setex(self, key, ttl, value):
                pass

        monkeypatch.setattr(dashboard_module, "_get_redis_cache", lambda: _Redis())

        outcome: dict = {}

        def _reader():
            try:
                outcome["result"] = dashboard_module._collect_all_traces(limit=10)
            except BaseException as exc:  # noqa: BLE001 - 测试需捕获实现抛出的任何异常
                outcome["error"] = exc

        def _invalidate():
            dashboard_module.invalidate_cache(source="b19-l2-late-read")

        invalidator = threading.Thread(target=_invalidate, name="b19-invalidate")
        reader = None
        invalidator.start()
        try:
            assert delete_entered.wait(timeout=5), "invalidate 未进入 Redis delete"
            assert dashboard_module._generation == 1, "generation 应已递增"
            assert dashboard_module._l2_invalidating > 0, "读取开始前应处于失效期间"

            reader = threading.Thread(target=_reader, name="b19-reader")
            reader.start()
            assert get_entered.wait(timeout=5), "reader 未进入 L2 GET"

            # 放行 delete，等失效完全结束（_l2_invalidating 归零），GET 仍未放行
            release_delete.set()
            invalidator.join(timeout=10)
            assert not invalidator.is_alive(), "失效线程未在期限内结束"
            assert dashboard_module._l2_invalidating == 0
        finally:
            release_delete.set()
            release_get.set()
            invalidator.join(timeout=10)
            if reader is not None:
                reader.join(timeout=10)

        assert reader is not None and not reader.is_alive(), "reader 未在期限内结束"
        assert "error" not in outcome, f"请求不应抛异常: {outcome.get('error')!r}"

        # 旧值不得作为本次结果返回
        assert [t["trace_id"] for t in outcome["result"]] == ["fresh-trace"]
        # 旧值不得回填 L1（任何档位）
        for tier in dashboard_module._CACHE_TIERS:
            entry = dashboard_module._cache.get(dashboard_module._cache_key(tier))
            if entry is not None:
                assert all(t["trace_id"] != "stale-during-invalidation" for t in entry[1]), (
                    f"档位 {tier} 的 L1 被失效期间读到的旧值污染"
                )

        # 读取按既有 miss 路径继续：重算并回填新值
        assert dashboard_module._cache[dashboard_module._cache_key(100)][1] == [fresh]
        again = dashboard_module._collect_all_traces(limit=10)
        assert [t["trace_id"] for t in again] == ["fresh-trace"]
