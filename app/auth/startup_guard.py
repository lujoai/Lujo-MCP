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


def unauthenticated_public_bind() -> bool:
    """通配监听 + 未配置任何 API Key —— 必须由中间件 fail-closed。

    实时读取 ``settings`` 单例（``AuthMiddleware.__init__`` 的 ``enabled`` 是构造期快照，
    不能反映运行期配置变化；测试也依赖逐请求重读）。
    """
    return is_unspecified_bind(settings.host) and not auth_enabled()
