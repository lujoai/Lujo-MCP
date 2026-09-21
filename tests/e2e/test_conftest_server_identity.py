"""W1（P0-TEST-1 / P2-TEST-3）：e2e 服务器身份校验与端口安全的单元级断言。

红证据路径说明（按工作包要求"对 fixture 的判定函数做单元级断言"）：
修复前 ``tests/e2e/conftest.py`` 的复用判定是内联的"/demo 返回 200 即
复用"且硬编码 127.0.0.1:8000——无法在不占用 8000 的前提下对旧逻辑注入
假服务器做行为级红证据；判定逻辑因此拆分到同目录 ``server_identity.py``
（纯逻辑、无模块级副作用——用 importlib 重载 conftest 会重放 factory
清空等模块级代码，破坏运行中的 e2e session 状态）。本文件对其做单元级
断言：红 = ``server_identity`` 符号缺失（缺陷"无身份校验"的直接体现），
绿 = 下列行为断言全部通过。
"""
import http.server
import json
import os
import sys
import threading
import types

from server_identity import (
    find_free_port,
    is_reusable_lujo_memory_server,
    iter_e2e_modules,
    port_bindable,
    sync_e2e_base_urls,
)


class _FakeServer:
    """标准库 HTTP 假服务器（线程内运行，不新增进程），按路径编排响应。"""

    def __init__(self, routes: dict[str, tuple[int, str]]):
        routes = dict(routes)
        self.port = find_free_port()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                status, body = routes.get(self.path, (404, "not found"))
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_FakeServer":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=3)


def test_reuse_probe_rejects_foreign_server_serving_demo_200():
    """P0-TEST-1 缺陷场景：/demo 200 但 /internal/health 缺失 → 必须拒绝复用。

    修复前的 conftest 恰好会在这种服务器上直接 yield 复用（无身份校验）。
    """
    with _FakeServer({"/demo": (200, "ok")}) as fake:
        assert is_reusable_lujo_memory_server(fake.port) is False


def test_reuse_probe_rejects_postgresql_backend_fingerprint():
    """PG 时代旧 dev server 指纹（storage=postgresql）→ 必须拒绝复用。"""
    from app import __version__
    from app.config import settings

    body = json.dumps(
        {
            "status": "ok",
            "service": settings.service_name,
            "version": __version__,
            "storage": "postgresql",
        }
    )
    with _FakeServer({"/internal/health": (200, body)}) as fake:
        assert is_reusable_lujo_memory_server(fake.port) is False


def test_reuse_probe_rejects_version_mismatch():
    """同 service/storage 但版本不一致 → 必须拒绝复用（非本仓库版本实例）。"""
    from app.config import settings

    body = json.dumps(
        {
            "status": "ok",
            "service": settings.service_name,
            "version": "0.0.0-not-this-repo",
            "storage": "memory",
        }
    )
    with _FakeServer({"/internal/health": (200, body)}) as fake:
        assert is_reusable_lujo_memory_server(fake.port) is False


def test_reuse_probe_rejects_connection_refused():
    """无监听端口（连接拒绝）→ 不复用（走自起路径）。"""
    port = find_free_port()
    assert is_reusable_lujo_memory_server(port) is False


def test_reuse_probe_accepts_real_lujo_memory_server(e2e_server):
    """正向：e2e_server fixture 实际使用的服务器必须通过复用判定。"""
    base_url = e2e_server
    assert isinstance(base_url, str) and base_url.startswith("http://127.0.0.1:")
    port = int(base_url.rsplit(":", 1)[1])
    assert is_reusable_lujo_memory_server(port) is True


def test_port_bindable_true_for_free_port():
    """port_bindable 对系统分配的空闲端口必须为真（自起端口选择依据）。"""
    port = find_free_port()
    assert port_bindable("127.0.0.1", port) is True


def test_port_bindable_false_when_occupied():
    """P2-TEST-3 核心：端口已被其它服务 LISTEN 时必须探测为不可绑定。

    （裸 socket bind 探测、不设 SO_REUSEADDR——避免 Windows 下
    SO_REUSEADDR 双绑假阳性导致的"绑定成功但流量仍进旧服务"。）
    """
    with _FakeServer({"/demo": (200, "ok")}) as fake:
        assert port_bindable("127.0.0.1", fake.port) is False


def test_sync_e2e_base_urls_rewrites_only_e2e_modules():
    """BASE_URL 重写只命中 tests/e2e 目录下已导入的模块，且可完整恢复。

    既有 e2e 用例以模块级 BASE_URL 常量（历史恒为 8000）调用服务器；
    fixture 自起随机端口时靠该函数把常量指向实际地址，用例零改动。
    """
    # 备份将被改写的真实模块的 BASE_URL 现值（fixture 可能已改为非 8000）
    backup = {
        id(mod): getattr(mod, "BASE_URL")
        for mod in iter_e2e_modules()
        if hasattr(mod, "BASE_URL")
    }

    e2e_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(e2e_dir)
    e2e_dir_fake = types.ModuleType("_e2e_sync_fake_inside")
    e2e_dir_fake.__file__ = os.path.join(e2e_dir, "_e2e_sync_fake_inside_mod.py")
    e2e_dir_fake.BASE_URL = "http://127.0.0.1:8000"
    outside = types.ModuleType("_e2e_sync_fake_outside")
    outside.__file__ = os.path.join(parent_dir, "_e2e_sync_fake_outside_mod.py")
    outside.BASE_URL = "http://127.0.0.1:8000"
    sys.modules[e2e_dir_fake.__name__] = e2e_dir_fake
    sys.modules[outside.__name__] = outside
    try:
        rewritten = sync_e2e_base_urls("http://127.0.0.1:39123")
        assert e2e_dir_fake.__name__ in rewritten, rewritten
        assert outside.__name__ not in rewritten, rewritten
        assert e2e_dir_fake.BASE_URL == "http://127.0.0.1:39123"
        assert outside.BASE_URL == "http://127.0.0.1:8000"
    finally:
        for mod in iter_e2e_modules():
            if id(mod) in backup:
                mod.BASE_URL = backup[id(mod)]
        sys.modules.pop(e2e_dir_fake.__name__, None)
        sys.modules.pop(outside.__name__, None)
