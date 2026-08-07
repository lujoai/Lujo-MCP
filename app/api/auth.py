"""鉴权辅助端点（CODE_REVIEW S1）—— 短时 beacon 令牌签发。

SDK / 仪表盘在无法设置自定义 header 的场景（``sendBeacon`` / ``EventSource``）
先携带永久 Key 调用本端点换取短时令牌，再在 URL 中携带令牌上报，
避免永久 API Key 进入查询参数被明文记录。
"""

from fastapi import APIRouter, Depends, Request

from app.auth import beacon
from app.auth.rbac import require_role
from app.config import settings

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/beacon-token",
    dependencies=[Depends(require_role("admin", "developer", "viewer"))],
)
def issue_beacon_token(request: Request) -> dict:
    """为当前调用者签发一个短时、作用域限定的 beacon 令牌。"""
    role = getattr(request.state, "role", "viewer")
    token = beacon.issue_beacon_token(role=role, scope=settings.beacon_token_scope)
    return {
        "token": token,
        "expires_in": settings.beacon_token_ttl_seconds,
        "scope": settings.beacon_token_scope,
    }
