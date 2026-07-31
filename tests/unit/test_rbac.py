"""单元测试：app.auth.rbac —— 角色分级 + FastAPI 依赖。"""

import pytest
from fastapi import HTTPException

from app.auth.rbac import ROLES, get_role_for_key, require_role, role_at_least
from app.config import settings


# ---------------------------------------------------------------------------
# get_role_for_key
# ---------------------------------------------------------------------------

class TestGetRoleForKey:
    """get_role_for_key: 根据 key 解析角色。"""

    def test_rbac_disabled_returns_admin(self, monkeypatch):
        """rbac_enabled=False → admin（向后兼容）。"""
        monkeypatch.setattr(settings, "rbac_enabled", False)
        monkeypatch.setattr(settings, "rbac_role_mapping", "")
        assert get_role_for_key("any-key") == "admin"

    def test_rbac_disabled_ignores_mapping(self, monkeypatch):
        """rbac_enabled=False 时即使有 mapping 也返回 admin。"""
        monkeypatch.setattr(settings, "rbac_enabled", False)
        monkeypatch.setattr(settings, "rbac_role_mapping", "key1:viewer")
        assert get_role_for_key("key1") == "admin"

    def test_rbac_enabled_with_mapping(self, monkeypatch):
        """rbac_enabled=True + 命中映射 → 返回映射角色。"""
        monkeypatch.setattr(settings, "rbac_enabled", True)
        monkeypatch.setattr(settings, "rbac_role_mapping", "key1:admin,key2:viewer,key3:developer")
        assert get_role_for_key("key1") == "admin"
        assert get_role_for_key("key2") == "viewer"
        assert get_role_for_key("key3") == "developer"

    def test_rbac_enabled_key_not_in_mapping_returns_viewer(self, monkeypatch):
        """rbac_enabled=True + 未命中 → viewer（最小权限）。"""
        monkeypatch.setattr(settings, "rbac_enabled", True)
        monkeypatch.setattr(settings, "rbac_role_mapping", "key1:admin")
        assert get_role_for_key("unknown-key") == "viewer"

    def test_rbac_enabled_empty_key_returns_viewer(self, monkeypatch):
        """rbac_enabled=True + 空 key → viewer。"""
        monkeypatch.setattr(settings, "rbac_enabled", True)
        monkeypatch.setattr(settings, "rbac_role_mapping", "")
        assert get_role_for_key("") == "viewer"

    def test_mapping_with_whitespace_and_invalid_entries(self, monkeypatch):
        """映射含空白/无效条目应被忽略。"""
        monkeypatch.setattr(settings, "rbac_enabled", True)
        monkeypatch.setattr(settings, "rbac_role_mapping", "  key1 : admin , , badrole:xyz , key2:viewer")
        assert get_role_for_key("key1") == "admin"
        assert get_role_for_key("key2") == "viewer"
        # xyz 不是合法角色 → 该条目被忽略 → key "badrole" 未命中 → viewer
        assert get_role_for_key("badrole") == "viewer"

    def test_mapping_empty_string(self, monkeypatch):
        """rbac_enabled=True + 空 mapping → 所有 key 返回 viewer。"""
        monkeypatch.setattr(settings, "rbac_enabled", True)
        monkeypatch.setattr(settings, "rbac_role_mapping", "")
        assert get_role_for_key("any-key") == "viewer"


# ---------------------------------------------------------------------------
# require_role
# ---------------------------------------------------------------------------

class _FakeState:
    """模拟 starlette Request.state。"""

    def __init__(self, role=None):
        if role is not None:
            self.role = role


class _FakeRequest:
    """模拟 fastapi.Request，仅暴露 state 属性。"""

    def __init__(self, role=None):
        self.state = _FakeState(role)


class TestRequireRole:
    """require_role: FastAPI 依赖工厂。"""

    @pytest.mark.asyncio
    async def test_allowed_role_passes(self):
        dep = require_role("admin", "developer")
        request = _FakeRequest(role="developer")
        result = await dep(request)
        assert result == "developer"

    @pytest.mark.asyncio
    async def test_admin_allowed_when_admin_in_list(self):
        dep = require_role("admin")
        request = _FakeRequest(role="admin")
        result = await dep(request)
        assert result == "admin"

    @pytest.mark.asyncio
    async def test_disallowed_role_raises_403(self):
        dep = require_role("admin")
        request = _FakeRequest(role="viewer")
        with pytest.raises(HTTPException) as exc_info:
            await dep(request)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_role_raises_403(self, monkeypatch):
        """request.state 无 role 属性 + rbac_enabled=True → 403（fail-closed）。"""
        monkeypatch.setattr(settings, "rbac_enabled", True)
        dep = require_role("admin")
        request = _FakeRequest()  # state 上不设置 role 属性
        with pytest.raises(HTTPException) as exc_info:
            await dep(request)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_role_rbac_disabled_returns_admin(self, monkeypatch):
        """request.state 无 role + rbac_enabled=False → admin（向后兼容放行）。

        对应 rbac.py:71-74 的守卫分支：鉴权未启用时自动放行。
        """
        monkeypatch.setattr(settings, "rbac_enabled", False)
        dep = require_role("admin")
        request = _FakeRequest()  # state 上不设置 role 属性
        result = await dep(request)
        assert result == "admin"

    @pytest.mark.asyncio
    async def test_missing_role_rbac_enabled_defaults_viewer(self, monkeypatch):
        """request.state 无 role + rbac_enabled=True → viewer（fail-closed）。

        对应 rbac.py:75-76 的守卫分支。
        """
        monkeypatch.setattr(settings, "rbac_enabled", True)
        dep = require_role("admin", "developer", "viewer")
        request = _FakeRequest()
        result = await dep(request)
        assert result == "viewer"

    @pytest.mark.asyncio
    async def test_empty_allowed_roles_rejects_everyone(self):
        """未传任何 allowed_roles → 所有角色都被拒。"""
        dep = require_role()
        request = _FakeRequest(role="admin")
        with pytest.raises(HTTPException) as exc_info:
            await dep(request)
        assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# role_at_least
# ---------------------------------------------------------------------------

class TestRoleAtLeast:
    """role_at_least: 角色层级判断（admin > developer > viewer）。"""

    def test_admin_at_least_viewer(self):
        assert role_at_least("admin", "viewer") is True

    def test_admin_at_least_admin(self):
        assert role_at_least("admin", "admin") is True

    def test_admin_at_least_developer(self):
        assert role_at_least("admin", "developer") is True

    def test_developer_at_least_admin_is_false(self):
        assert role_at_least("developer", "admin") is False

    def test_developer_at_least_developer(self):
        assert role_at_least("developer", "developer") is True

    def test_developer_at_least_viewer(self):
        assert role_at_least("developer", "viewer") is True

    def test_viewer_at_least_admin_is_false(self):
        assert role_at_least("viewer", "admin") is False

    def test_viewer_at_least_developer_is_false(self):
        assert role_at_least("viewer", "developer") is False

    def test_viewer_at_least_viewer(self):
        assert role_at_least("viewer", "viewer") is True

    def test_unknown_role_at_least_anything_is_false(self):
        """未知角色 → 0 级，fail-closed 返回 False。"""
        assert role_at_least("unknown", "viewer") is False

    def test_known_role_at_least_unknown_minimum_is_true(self):
        """minimum 为未知角色 → 0 级，任何已知角色都满足。"""
        assert role_at_least("viewer", "unknown") is True


# ---------------------------------------------------------------------------
# ROLES 常量
# ---------------------------------------------------------------------------

class TestRolesConstant:
    def test_roles_set_contents(self):
        assert ROLES == {"admin", "developer", "viewer"}
