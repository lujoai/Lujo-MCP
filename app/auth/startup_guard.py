"""启动安全不变量的单一判定来源 —— 通配监听 + 无凭据必须 fail-closed。

背景（U06）：``app/main.py`` 的 ``validate_startup_configuration()`` 只在 **lifespan 内**
调用。外部 ASGI 宿主以 ``lifespan="off"`` 挂载 app（``uvicorn --lifespan off``、部分嵌入式
/反代部署）时该校验根本不执行，而 ``AuthMiddleware`` 因 ``auth_enabled() is False`` 直接放行
→ 危险组合静默对外提供服务。

安全不变量不能依赖生命周期是否运行，因此判定收敛到本模块，由两处共同复用：

- ``app/main.py.validate_startup_configuration()``：启动期硬拒绝（既有行为，不变）。
- ``app/middleware.py.AuthMiddleware.dispatch()``：按请求 fail-closed（U06 新增消费方）。

刻意只覆盖「未指定地址」（``0.0.0.0`` / ``::``）这一硬拒绝口径：绑定到具体非回环地址
（如 10.0.0.0）在既有语义里只 WARNING，不在请求层收紧，避免擅自改变公开认证契约。
"""

import ipaddress
from typing import Optional

from app.auth.key_rotation import auth_enabled
from app.config import settings


def is_unspecified_bind(bind_host: object) -> bool:
    """FIX: R7-A1 —— 是否 IPv4 0.0.0.0 / IPv6 :: 通配绑定。

    此前用子串匹配 ``"0.0.0.0" in str(bind_host)``：漏掉 IPv6 通配 ``::``
    （SEC-03 对 IPv6 失效），且误杀含子串的合法地址（10.0.0.0 / 100.0.0.0）。
    ``ipaddress.ip_address(h).is_unspecified`` 对两类通配均成立；主机名
    解析失败返回 False（走非回环 warning 路径，不阻断）。
    """
    try:
        return ipaddress.ip_address(str(bind_host)).is_unspecified
    except ValueError:
        return False


def resolve_bind_host(real_host: Optional[str] = None) -> str:
    """配置口径下生效的监听地址归一（与 settings.host 同语义）。

    注意：ASGI ``scope["server"]`` 是**已建立连接**的本地 socket 地址
    （uvicorn ``transport.get_extra_info("sockname")``），绑 0.0.0.0 时经
    回环进来的连接报的是 ``127.0.0.1:ephemeral``，永远看不到通配 —— 它不能
    当"真实 bind"用，只能作为**暴露证据**（见 :func:`_routable_exposure`）。
    """
    if real_host is not None:
        try:
            ipaddress.ip_address(str(real_host))
        except ValueError:
            pass
        else:
            return str(real_host)
    return str(settings.host)


def _host_claims_local_only(bind_host: object) -> bool:
    """配置是否声明"只服务本地"（回环/localhost）。无法解析的主机名按非声明处理。"""
    text = str(bind_host)
    if text.strip().lower() in {"localhost", ""}:
        return text.strip().lower() == "localhost"
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    return ip.is_loopback


def _routable_exposure(real_host: Optional[str]) -> bool:
    """本连接实际落在可路由 NIC 地址上，而配置却声明只绑本地 → 配置与真实绑定不一致。

    这是服务器提供的权威事实（Accepted 连接的本地端点），不是从客户端头猜测。
    显式声明 LAN 绑定（HOST=10.x）的情况不在这里收紧（保持既有 WARNING-only 契约）。
    """
    if real_host is None or _host_claims_local_only(settings.host) is False:
        return False
    try:
        ip = ipaddress.ip_address(str(real_host))
    except ValueError:
        return False
    return not ip.is_loopback


def unauthenticated_public_bind(real_host: Optional[str] = None) -> bool:
    """无凭据时哪些情况必须 fail-closed（两条独立证据，均不猜测）。

    1. 生效监听地址为通配（``0.0.0.0`` / ``::``）：沿用 U06 口径，实时读 settings
       （构造期快照不可靠）；若 ASGI 服务器给出的连接本地端点本身就是通配
       （规范允许的服务器可实现如此填报），同样按通配处理。
    2. 配置声明回环本地模式，但本连接实际落在可路由 NIC 地址上
       （外部宿主绑了非回环地址而未设置 HOST —— 用户从未授权对外服务）。

    显式非回环 HOST（如 10.x）不新增拒绝：保持启动期 WARNING-only 的既有契约。
    """
    if auth_enabled():
        return False
    if is_unspecified_bind(resolve_bind_host(real_host)):
        return True
    return _routable_exposure(real_host)
