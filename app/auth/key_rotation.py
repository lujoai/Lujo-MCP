"""API key 轮换 —— 多 key 共存（轮换期）+ 单 key 向后兼容。

设计要点：
- ``settings.api_keys``（逗号分隔）优先；为空时回退 ``settings.api_key``（向后兼容）。
- ``verify_api_key`` 遍历所有 key 不短路，配合 ``hmac.compare_digest`` 恒定时间比较，
  避免时序侧信道泄漏"命中第几个 key"。
- 无 key 配置时 ``auth_enabled`` 返回 False，由 ``AuthMiddleware`` 通过 ``enabled``
  标志放行（鉴权关闭）；``verify_api_key`` 本身 fail-closed 返回 False。
"""

import hmac

from app.config import settings


def get_valid_keys() -> list[str]:
    """读取有效 key 列表。

    - 优先 ``settings.api_keys``（逗号分隔，去空白、丢弃空条目）。
    - ``api_keys`` 为空时回退 ``settings.api_key``（单 key 向后兼容）。
    - 两者均空 → 返回 ``[]``（鉴权关闭）。
    """
    raw = settings.api_keys or ""
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if keys:
        return keys
    if settings.api_key:
        return [settings.api_key]
    return []


def verify_api_key(candidate: str) -> bool:
    """恒定时间校验 candidate 是否命中任一有效 key。

    - 空 candidate → False（fail-closed）。
    - 无 key 配置 → False（由 middleware 的 ``enabled`` 标志决定是否放行）。
    - 遍历所有 key 不短路，累积匹配结果，避免时序泄漏。
    """
    if not candidate:
        return False
    valid_keys = get_valid_keys()
    if not valid_keys:
        return False
    matched = False
    for key in valid_keys:
        # str 版 compare_digest 仅支持 ASCII；畸形非 ASCII 头会抛 TypeError → 500。
        # 统一 encode 为 bytes（UTF-8），恒定时间语义不变。
        if hmac.compare_digest(candidate.encode("utf-8"), key.encode("utf-8")):
            matched = True
    return matched


def auth_enabled() -> bool:
    """是否启用鉴权（任一 key 配置即启用）。"""
    return len(get_valid_keys()) > 0
