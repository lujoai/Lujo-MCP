"""RBAC 角色分级 —— admin > developer > viewer。

- admin：完全访问（运维/部署）。
- developer：读 + 调试（开发联调）。
- viewer：只读（仪表盘）。

向后兼容：``rbac_enabled=False`` 时 ``get_role_for_key`` 始终返回 ``"admin"``，
即未启用 RBAC = 全权访问，与历史单 key 行为一致。

最小权限：``rbac_enabled=True`` 但 key 未在 ``rbac_role_mapping`` 中时返回 ``"viewer"``。
"""

from fastapi import HTTPException, Request

from app.config import settings

ROLES = {"admin", "developer", "viewer"}

# 角色层级：数值越大权限越高。
_ROLE_HIERARCHY = {"admin": 3, "developer": 2, "viewer": 1}


def get_role_for_key(key: str) -> str:
    """根据 key 解析角色。

    - ``rbac_enabled=False`` → ``"admin"``（向后兼容）。
    - ``rbac_enabled=True`` + 命中 ``rbac_role_mapping`` → 映射角色。
    - ``rbac_enabled=True`` + 未命中 / 空 key → ``"viewer"``（最小权限）。
    """
    if not settings.rbac_enabled:
        return "admin"
    if not key:
        return "viewer"
    mapping = _parse_role_mapping(settings.rbac_role_mapping or "")
    return mapping.get(key, "viewer")


def _parse_role_mapping(raw: str) -> dict[str, str]:
    """解析 ``"key1:admin,key2:viewer"`` → dict。

    容错：空白条目、无冒号条目、非法角色值均被丢弃。
    """
    mapping: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        k, _, v = entry.partition(":")
        k = k.strip()
        v = v.strip()
        if k and v in ROLES:
            mapping[k] = v
    return mapping


def require_role(*allowed_roles: str):
    """FastAPI 依赖工厂：仅允许指定角色通过，否则 403。

    用法::

        @app.post("/api/debug/analyze", dependencies=[Depends(require_role("admin", "developer"))])
        async def analyze(): ...

    向后兼容：当鉴权未启用（无 API Key 配置）时，自动放行并视为 admin，
    保持与历史单 key 行为一致。
    """
    allowed = set(allowed_roles)

    async def dependency(request: Request) -> str:
        role = getattr(request.state, "role", None)
        if role is None:
            # 鉴权未启用（无 API Key 配置或 PUBLIC_PATHS），向后兼容放行
            if not settings.rbac_enabled:
                return "admin"
            # rbac_enabled=True 但无 key → fail-closed 为 viewer
            role = "viewer"
        if role not in allowed:
            raise HTTPException(status_code=403, detail="Forbidden: insufficient role")
        return role

    return dependency


def role_at_least(role: str, minimum: str) -> bool:
    """判断 role 是否不低于 minimum 级别（admin > developer > viewer）。

    未知 role（不在 ROLES 中）视为 0，始终返回 False（fail-closed）。
    未知 minimum（不在 ROLES 中）同样 fail-closed 返回 False——避免调用方拼错
    角色名（如 ``"super_admin"``）时被当作「无门槛」放行一切角色（footgun）。
    """
    # FIX(v0.7.1-b5-9): minimum 未知时 fail-closed。此前 .get(minimum, 0) 使未知
    # minimum 降为 0 级，任何已知角色都满足——拼错角色名即放行一切（fail-open）。
    if minimum not in _ROLE_HIERARCHY:
        return False
    return _ROLE_HIERARCHY.get(role, 0) >= _ROLE_HIERARCHY[minimum]
