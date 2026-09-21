"""启动安全不变量的单一判定来源 —— 通配监听 + 无凭据必须 fail-closed。

背景（U06）：``app/main.py`` 的 ``validate_startup_configuration()`` 只在 **lifespan 内**
调用。外部 ASGI 宿主以 ``lifespan="off"`` 挂载 app（``uvicorn --lifespan off``、部分嵌入式
/反代部署）时该校验根本不执行，而 ``AuthMiddleware`` 因 ``auth_enabled() is False`` 直接放行
→ 危险组合静默对外提供服务。

安全不变量不能依赖生命周期是否运行，因此判定收敛到本模块，由两处共同复用：

- ``app/main.py.validate_startup_configuration()``：启动期硬拒绝（既有行为，不变）。
- ``app/middleware.py.AuthMiddleware.dispatch()``：按请求 fail-closed（U06 新增消费方）。

刻意只覆盖「未指定地址」（``0.0.0.0`` / ``::`` / 空串，空串在 syscall 层等价
INADDR_ANY）这一硬拒绝口径：绑定到具体非回环地址（如 10.0.0.0）在既有语义里
只 WARNING，不在请求层收紧，避免擅自改变公开认证契约。
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

    S2-F3：空串 / 纯空白串同样判为通配。``host=''`` 在 syscall 层等价
    INADDR_ANY（CPython ``bind(("", port))`` 语义；uvicorn 0.49 的
    ``config.py`` 直接 ``sock.bind((self.host, self.port))`` 透传），
    语义上就是"绑全部接口"；此前 ``ValueError → False`` 使启动期与请求期
    两道守卫同时沉默（启动期只打一条 WARNING 且日志里地址为空）。注：
    pydantic-settings 对未加引号的 ``HOST=``（空值）实测不回落默认而是
    得到 ``''``；对引号包裹的空白 ``HOST="   "`` 不 strip，故此处先 strip
    再判空，两类取值一并覆盖。
    """
    text = str(bind_host).strip()
    if not text:
        return True
    try:
        return ipaddress.ip_address(text).is_unspecified
    except ValueError:
        return False


def resolve_bind_host(real_host: Optional[str] = None) -> str:
    """配置口径下生效的监听地址归一（与 settings.host 同语义）。

    注意：ASGI ``scope["server"]`` 是**已建立连接**的本地 socket 地址
    （uvicorn ``transport.get_extra_info("sockname")``），绑 0.0.0.0 时经
    回环进来的连接报的是 ``127.0.0.1:ephemeral``，永远看不到通配 —— 它不能
    当"真实 bind"用，只能作为**暴露证据**（见 :func:`_routable_exposure`）。

    S2 注（刻意取舍，勿改成保守拒绝）：``real_host`` 不可解析时回落
    ``settings.host`` 是**有意**的——唯一受支持宿主 uvicorn 的
    ``scope["server"]`` 来自 ``socket.getsockname()``，对 TCP 恒为数值 IP
    （不可能是主机名），而测试栈（Starlette TestClient）填的是主机名
    ``testserver``；整套件大量 TestClient 用例依赖该回落在无鉴权状态下放行。
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


def bind_is_local_only(bind_host: object = None) -> bool:
    """**配置口径**下服务是否只监听回环（W10 / P2-SEC-2 的判据来源）。

    与 :func:`unauthenticated_public_bind` 共用 :func:`_host_claims_local_only`，
    保证"什么算本地绑定"全仓只有一个定义。默认读 ``settings.host``。

    为什么按**配置绑定地址**判、而不是按 ``request.client.host``：反代部署下
    对端恒为代理（常常就是回环），按对端判等于对所有经代理的请求 fail-open
    —— 与 ``internal_health`` 遇转发头即 fail-closed 是同一教训（P3-13）。
    """
    return _host_claims_local_only(
        settings.host if bind_host is None else bind_host
    )


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


def _endpoint_is_loopback(real_host: Optional[str]) -> bool:
    """连接本地端点是否为回环（含 ``::1`` 与 IPv4-mapped ``::ffff:127.0.0.1``，
    Python 3.12 中后者的 ``is_loopback`` 为 True）。

    - ``None`` → False：无证据即 fail-closed（保持既有 H 场景拒绝行为）。
    - 不可解析 → False：与 :func:`resolve_bind_host` 的回落取舍配合——
      配置口径为通配的分支下因此**拒绝**（与既有 H/J 场景一致），回环/显式
      配置分支下因此放行（既有回落格，见该函数 docstring 的取舍说明）。
    """
    if real_host is None:
        return False
    try:
        ip = ipaddress.ip_address(str(real_host))
    except ValueError:
        return False
    return ip.is_loopback


def unauthenticated_public_bind(real_host: Optional[str] = None) -> bool:
    """无凭据时哪些情况必须 fail-closed（三条独立证据，均不猜测）。

    S2-F1/F2 修复：证据路径 1（配置口径为通配）必须**直接查询
    ``settings.host``**，不得先经 ``resolve_bind_host`` 归一——后者让连接
    端点 ``real_host`` 优先于配置，恰使显式 ``0.0.0.0`` / ``::`` 被具体
    NIC 地址顶掉、两条证据同时沉默（守卫在"掌握正向暴露证据"时反而
    放行，方向是反的）。

    1. 配置声明通配（``0.0.0.0`` / ``::`` / 空串）：服务实际已绑在所有
       接口上，"本次连接来自回环"只是当下事实而非绑定保证——但按单用户
       本地定位，回环调用方处于信任边界内（本机进程已能读 .env 与 KB
       文件），予以放行（见既有
       test_settings_wildcard_but_real_bind_loopback_serves）。
    2. 若 ASGI 服务器给出的连接本地端点本身就是通配（规范允许的服务器
       可实现如此填报），同样按通配处理。
    3. 配置声明回环本地模式，但本连接实际落在可路由 NIC 地址上
       （外部宿主绑了非回环地址而未设置 HOST —— 用户从未授权对外服务）。

    显式非回环 HOST（如 10.x）不新增拒绝：保持启动期 WARNING-only 的既有契约。
    """
    if auth_enabled():
        return False
    if is_unspecified_bind(settings.host):
        return not _endpoint_is_loopback(real_host)
    if is_unspecified_bind(resolve_bind_host(real_host)):
        return True
    return _routable_exposure(real_host)
