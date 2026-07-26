"""鉴权模块 —— API key 轮换 + RBAC 角色分级。

公开接口：
- :func:`verify_api_key` / :func:`auth_enabled` / :func:`get_valid_keys`：密钥校验与轮换。
- :func:`get_role_for_key` / :func:`require_role` / :func:`role_at_least`：RBAC 角色分级。
- :data:`ROLES`：合法角色集合。
"""

from app.auth.key_rotation import auth_enabled, get_valid_keys, verify_api_key
from app.auth.rbac import ROLES, get_role_for_key, require_role, role_at_least

__all__ = [
    "ROLES",
    "auth_enabled",
    "get_role_for_key",
    "get_valid_keys",
    "require_role",
    "role_at_least",
    "verify_api_key",
]
