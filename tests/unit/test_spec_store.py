"""单元测试：规范存储 spec_store"""
import pytest
from app.mcp.verifier import spec_store


@pytest.fixture(autouse=True)
def _isolate_spec_store():
    """每个用例前后清空 spec_store，避免跨用例污染。"""
    spec_store.clear()
    # clear() 已重置 _specs + _restored=False，但 trace_store 中残留历史 spec 记录
    # 设置 _restored=True 防止 list_specs() 恢复时读到历史数据
    spec_store._restored = True
    yield
    spec_store.clear()
    spec_store._restored = True


class TestCreateAndGet:

    def test_create_returns_id(self):
        spec_id = spec_store.create({
            "kind": "api",
            "target": "GET /api/user",
            "expect": {"status": 200, "body_rules": {"name": "Alice"}},
        })
        assert spec_id.startswith("spec-")

    def test_create_with_explicit_id(self):
        spec_id = spec_store.create({
            "id": "my-spec-1",
            "kind": "api",
            "target": "GET /api/user",
            "expect": {"status": 200},
        })
        assert spec_id == "my-spec-1"

    def test_get_existing(self):
        spec_id = spec_store.create({
            "kind": "api",
            "target": "POST /api/login",
            "expect": {"status": 200},
        })
        spec = spec_store.get(spec_id)
        assert spec is not None
        assert spec["id"] == spec_id
        assert spec["kind"] == "api"
        assert spec["target"] == "POST /api/login"
        assert spec["expect"] == {"status": 200}
        assert "created_at" in spec
        assert "updated_at" in spec

    def test_get_nonexistent(self):
        assert spec_store.get("no-such-id") is None


class TestUpdate:

    def test_update_partial(self):
        spec_id = spec_store.create({
            "kind": "api",
            "target": "GET /api/user",
            "expect": {"status": 200},
        })
        updated = spec_store.update(spec_id, {"expect": {"status": 201}})
        assert updated is not None
        assert updated["expect"] == {"status": 201}
        assert updated["kind"] == "api"  # 未改的字段保留

        # 确认内存也更新了
        again = spec_store.get(spec_id)
        assert again["expect"] == {"status": 201}

    def test_update_nonexistent(self):
        result = spec_store.update("no-such-id", {"kind": "ui"})
        assert result is None

    def test_update_id_immutable(self):
        spec_id = spec_store.create({"kind": "api", "target": "x", "expect": {}})
        spec_store.update(spec_id, {"id": "hacked"})
        spec = spec_store.get(spec_id)
        assert spec["id"] == spec_id  # id 没被改


class TestDelete:

    def test_delete_existing(self):
        spec_id = spec_store.create({"kind": "api", "target": "x", "expect": {}})
        assert spec_store.delete(spec_id) is True
        assert spec_store.get(spec_id) is None

    def test_delete_nonexistent(self):
        assert spec_store.delete("no-such-id") is False


class TestListSpecs:

    def test_list_all(self):
        spec_store.create({"kind": "api", "target": "GET /a", "expect": {}})
        spec_store.create({"kind": "ui", "target": "click #btn", "expect": {}})
        specs = spec_store.list_specs()
        assert len(specs) == 2

    def test_list_filter_by_kind(self):
        spec_store.create({"kind": "api", "target": "GET /a", "expect": {}})
        spec_store.create({"kind": "ui", "target": "click #btn", "expect": {}})
        spec_store.create({"kind": "api", "target": "GET /b", "expect": {}})

        api_specs = spec_store.list_specs(kind="api")
        assert len(api_specs) == 2
        assert all(s["kind"] == "api" for s in api_specs)

    def test_list_filter_by_target(self):
        spec_store.create({"kind": "api", "target": "GET /api/user", "expect": {}})
        spec_store.create({"kind": "api", "target": "GET /api/order", "expect": {}})

        user_specs = spec_store.list_specs(target="user")
        assert len(user_specs) == 1
        assert user_specs[0]["target"] == "GET /api/user"

    def test_list_empty(self):
        assert spec_store.list_specs() == []


class TestAssertEngineIntegration:

    def test_spec_from_store_works_with_assert(self):
        """spec_store 存的 spec 能直接喂给 assert_behavior"""
        from app.mcp.verifier.assert_engine import assert_behavior

        spec_id = spec_store.create({
            "kind": "api",
            "target": "GET /api/user",
            "expect": {"status": 200, "body_rules": {"name": "Alice"}},
        })
        spec = spec_store.get(spec_id)

        actual = {"status_code": 200, "body": {"name": "Alice"}}
        result = assert_behavior(actual, spec)
        assert result["matched"] is True


class TestRestoreFromStorage:
    """C4 对标：验证重启后 spec 能从 trace_store 恢复。"""

    def test_restore_after_memory_clear(self):
        """写入 spec → 模拟重启（清空内存）→ list_specs 应能从存储层恢复。"""
        spec_id = spec_store.create({
            "kind": "api",
            "target": "GET /api/user",
            "expect": {"status": 200},
        })
        # 确保 list_specs 已触发过恢复标志
        spec_store.list_specs()
        # 模拟重启：清空内存 + 重置恢复标志
        with spec_store._lock:
            spec_store._specs.clear()
            spec_store._restored = False
        # list_specs 应自动从 trace_store 恢复
        specs = spec_store.list_specs()
        assert len(specs) >= 1
        found = any(s["id"] == spec_id for s in specs)
        assert found, f"期望在恢复结果中找到 {spec_id}"

    def test_restore_does_not_duplicate(self):
        """多次调用 list_specs() 不应重复插入 spec。"""
        spec_store.create({"kind": "api", "target": "x", "expect": {}})
        spec_store.list_specs()
        count_after_first = len(spec_store._specs)
        spec_store.list_specs()
        count_after_second = len(spec_store._specs)
        assert count_after_first == count_after_second

    def test_list_after_memory_only_no_crash(self):
        """仅内存中有 spec（未持久化）时 list_specs 不崩溃。"""
        # 手动往 _specs 塞数据，跳过 create() 的 add_log
        with spec_store._lock:
            spec_store._specs["manual-spec"] = {
                "id": "manual-spec",
                "kind": "ui",
                "target": "click #btn",
                "expect": {},
                "created_at": 0,
                "updated_at": 0,
            }
        # 不调用 list_specs 触发恢复（或恢复了也找不到）
        specs = spec_store.list_specs()
        assert any(s["id"] == "manual-spec" for s in specs)


class TestAtomicWrites:
    """SEC-13：验证 update 的 crash-safe append 语义与多版本读取。"""

    def test_update_appends_new_version_without_delete(self):
        """update 不再调用 delete_logs，仅追加新版本到存储层。"""
        from unittest.mock import patch
        from app.mcp.core.logs import get_logs as _get_logs

        spec_id = spec_store.create({
            "kind": "api",
            "target": "GET /api/user",
            "expect": {"status": 200},
        })
        with patch("app.mcp.verifier.spec_store.delete_logs") as mock_delete:
            updated = spec_store.update(spec_id, {"expect": {"status": 201}})
            assert mock_delete.call_count == 0
        assert updated is not None
        # 存储层应存在该 spec 的条目，且 step=="spec"
        entries = [e for e in _get_logs(spec_id) if e.get("step") == "spec"]
        assert len(entries) >= 1

    def test_read_returns_newest_when_multiple_versions(self):
        """多版本共存时 get() 应返回 updated_at 最大者。"""
        from app.mcp.core.logs import add_log as _add_log
        from app.mcp.verifier.spec_store import _STEP_SPEC

        spec_id = "multi-version-spec"
        # 注入旧版本
        _add_log(spec_id, _STEP_SPEC, {
            "id": spec_id, "kind": "api", "target": "old",
            "expect": {}, "created_at": 100.0, "updated_at": 100.0,
        })
        # 注入新版本
        _add_log(spec_id, _STEP_SPEC, {
            "id": spec_id, "kind": "api", "target": "new",
            "expect": {}, "created_at": 100.0, "updated_at": 200.0,
        })
        # 清空内存 + 重置恢复标志，强制走存储回读
        with spec_store._lock:
            spec_store._specs.clear()
            spec_store._restored = False
        spec = spec_store.get(spec_id)
        assert spec is not None
        assert spec["target"] == "new"
        assert spec["updated_at"] == 200.0

    def test_restore_picks_newest_version(self):
        """_restore_if_needed 应将最新版本写入 _specs。"""
        from app.mcp.core.logs import add_log as _add_log
        from app.mcp.verifier.spec_store import _STEP_SPEC

        spec_id = "restore-newest-spec"
        # 注入旧版本
        _add_log(spec_id, _STEP_SPEC, {
            "id": spec_id, "kind": "api", "target": "old",
            "expect": {}, "created_at": 100.0, "updated_at": 100.0,
        })
        # 注入新版本
        _add_log(spec_id, _STEP_SPEC, {
            "id": spec_id, "kind": "api", "target": "new",
            "expect": {}, "created_at": 100.0, "updated_at": 200.0,
        })
        # 清空内存 + 重置恢复标志
        with spec_store._lock:
            spec_store._specs.clear()
            spec_store._restored = False
        # list_specs 触发 _restore_if_needed
        spec_store.list_specs()
        with spec_store._lock:
            restored = spec_store._specs.get(spec_id)
        assert restored is not None
        assert restored["target"] == "new"
        assert restored["updated_at"] == 200.0
