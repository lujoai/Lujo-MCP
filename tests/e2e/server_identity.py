"""W1（P0-TEST-1 / P2-TEST-3）：e2e 服务器身份判定与端口安全工具。

纯逻辑模块（无模块级副作用），供 ``tests/e2e/conftest.py`` 的
``e2e_server`` fixture 使用；``tests/e2e/test_conftest_server_identity.py``
对本模块做单元级断言。判定逻辑拆出 conftest 的原因：conftest 含
factory 清空等模块级副作用，直接重载会破坏运行中的 e2e session 状态，
测试无法安全引用。
"""
import json
import os
import socket
import sys
import urllib.request

from app import __version__ as _APP_VERSION
from app.config import settings

# 与既有 e2e 用例同源的 API key 口径（tests/e2e/test_*.py 的 API_KEY）
_API_KEY = settings.api_key or "test_secret_key_456"


def is_reusable_lujo_memory_server(port: int, timeout: float = 1.0) -> bool:
    """判定 127.0.0.1:port 是否为本仓库版本的 memory 后端 Lujo 实例。

    P0-TEST-1：``/demo`` 返回 200 不构成复用依据——异构服务或旧版 dev
    server（PG 时代）也能满足，那曾是 e2e 唯一可写真实数据库的路径。
    只有 ``/internal/health``（回环免鉴权的只读端点）返回的
    service/version/storage 三项都与本进程一致，才允许复用：

    - service  == settings.service_name（是 Lujo 本尊，不是异构服务）；
    - version  == app.__version__（是本仓库版本，不是旧版 dev server）；
    - storage  == "memory"（运行时后端真值，不是 .env 声明的其它后端）。

    探测请求带上与 e2e 用例同源的 X-API-Key：对端启用鉴权时也能完成
    校验；key 不一致 → 401/403 → 拒绝复用（保守方向失败安全）。
    任何异常（连接拒绝、超时、非 200、非 JSON、字段缺失）一律 False。
    """
    url = f"http://127.0.0.1:{port}/internal/health"
    request = urllib.request.Request(url, headers={"X-API-Key": _API_KEY})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return False
    try:
        payload = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    return (
        payload.get("service") == settings.service_name
        and payload.get("version") == _APP_VERSION
        and payload.get("storage") == "memory"
    )


def port_bindable(host: str = "127.0.0.1", port: int = 0) -> bool:
    """裸 socket bind 探测端口是否可绑定。

    有意不设 SO_REUSEADDR：Windows 上 SO_REUSEADDR 允许对已 LISTEN 端口
    双重绑定（"绑定成功但流量仍进旧服务"的假阳性），P2-TEST-3 的整体
    ERROR 正源于此；uvicorn 自身会设 SO_REUSEADDR，故端口占用检测必须
    在交给 uvicorn 之前用裸 socket 完成。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def find_free_port() -> int:
    """让系统分配一个空闲端口。

    先例：tests/integration/test_process_boundary.py 的同名函数
    （此处复制实现而非 import 测试模块，避免跨包导入测试文件的副作用）。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def iter_e2e_modules():
    """枚举已导入的 tests/e2e 测试模块（按 __file__ 所在目录匹配）。"""
    e2e_dir = os.path.dirname(os.path.abspath(__file__))
    for mod in list(sys.modules.values()):
        mod_file = getattr(mod, "__file__", None)
        if not mod_file or not isinstance(mod_file, str):
            continue
        if (
            os.path.normcase(os.path.dirname(os.path.abspath(mod_file)))
            != os.path.normcase(e2e_dir)
        ):
            continue
        yield mod


def sync_e2e_base_urls(base_url: str) -> list[str]:
    """把 tests/e2e 下已导入测试模块的 BASE_URL 常量重写为实际服务器地址。

    既有 e2e 用例以模块级 ``BASE_URL = "http://127.0.0.1:8000"`` 常量
    调用服务器（历史上 e2e 服务器恒在 8000）。当 8000 被异构服务占用、
    fixture 自起随机空闲端口时（P2-TEST-3），必须让这些常量指向实际
    端口，用例代码零改动。返回被改写的模块名列表（测试用它做完整恢复）。
    """
    rewritten = []
    for mod in iter_e2e_modules():
        if hasattr(mod, "BASE_URL"):
            mod.BASE_URL = base_url
            rewritten.append(mod.__name__)
    return rewritten
