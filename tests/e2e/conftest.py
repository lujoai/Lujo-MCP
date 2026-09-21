import os
import threading
import time

import pytest
import uvicorn

# R8 存储隔离：必须在导入 app.main（其 lifespan 会读 storage_backend）之前
# 强制后端，否则 e2e 全链路会把测试数据写进开发者本机真实 PostgreSQL。
# WP3（Step 3 Breaking #1）：运行时 _VALID_BACKENDS 已收窄为 {"memory"}，本测试
# 后端白名单同步收口为仅 memory。显式设 LUJO_TEST_STORAGE_BACKEND=postgresql 会在
# conftest 导入期明确失败（不静默改写为 memory、不降级为 skip）；真 PG 回归能力
# 随 WP5 删除 PG 驱动一并到期。
_FORCED_BACKEND = os.environ.get("LUJO_TEST_STORAGE_BACKEND", "memory").lower()
if _FORCED_BACKEND == "postgresql":
    raise RuntimeError(
        "LUJO_TEST_STORAGE_BACKEND=postgresql 已被拒绝：PostgreSQL 运行时后端已在 "
        "Step 3 正式移除，测试后端白名单同步收窄为仅 memory（不会静默改写）。"
        "请去掉该环境变量跑默认 memory；旧 PG kb_entries 数据的一次性迁移脚本"
        "（scripts/migrate_pg_kb_to_sqlite.py）不随当前版本分发，需要迁移请先在 "
        "v0.8.x 完成后再升级（详见 TROUBLESHOOTING.md L 节）。"
    )
if _FORCED_BACKEND not in ("memory",):
    raise RuntimeError(
        f"LUJO_TEST_STORAGE_BACKEND 非法: {_FORCED_BACKEND!r}（可选 memory）"
    )
os.environ["STORAGE_BACKEND"] = _FORCED_BACKEND

import pydantic_settings  # noqa: E402  与 unit conftest 相同的时序要求

pydantic_settings.BaseSettings.model_config["extra"] = "ignore"

from app.config import settings  # noqa: E402
from app.runtime.core.storage import factory as _storage_factory  # noqa: E402

settings.storage_backend = _FORCED_BACKEND
# v0.8.0 KB 持久化「笔记本」测试隔离：与 unit/integration 同口径显式关闭
# （含 env——子进程/子服务继承 env 时同样生效），避免 e2e 全链路往工作目录写
# lujo-kb.sqlite3。
os.environ["KB_PERSIST_ENABLED"] = "false"
settings.kb_persist_enabled = False
for _name in ("_trace_store", "_session_store", "_error_store", "_spec_store", "_knowledge_store"):
    setattr(_storage_factory, _name, None)

from app.main import app  # noqa: E402

# W1（P0-TEST-1 / P2-TEST-3）：e2e 服务器身份校验与端口安全。判定与端口
# 工具是纯逻辑，拆分到同目录 server_identity.py 供单元级断言引用（本
# conftest 含 factory 清空等模块级副作用，直接重载会破坏运行中状态）。
from server_identity import (  # noqa: E402
    find_free_port,
    is_reusable_lujo_memory_server,
    port_bindable,
    sync_e2e_base_urls,
)

_E2E_HOST = "127.0.0.1"
_E2E_PREFERRED_PORT = 8000
_SERVER_READY_TIMEOUT_S = 15.0


@pytest.fixture(scope="session", autouse=True)
def e2e_server():
    """Ensure a local Lujo memory server is running for e2e tests.

    W1（P0-TEST-1 / P2-TEST-3）：
    - 复用 127.0.0.1:8000 前必须证明对端是本仓库版本的 memory 后端实例
      （/internal/health 的 service/version/storage 三项指纹比对，见
      server_identity.is_reusable_lujo_memory_server）；/demo 返回 200 不再
      构成复用依据——异构服务或旧版 dev server 也能满足，那曾是 e2e
      唯一可写真实数据库的路径。
    - 8000 被异构服务占用时不再整体 ERROR：自起优先 8000（与历史行为
      等价），端口不可绑定或启动失败则退到随机空闲端口；已导入的 e2e
      测试模块的 BASE_URL 常量会被同步指向实际地址，既有用例零改动。
    """
    # 1) 复用校验（P0-TEST-1）：证明是本仓库 memory 实例才复用
    if is_reusable_lujo_memory_server(_E2E_PREFERRED_PORT):
        base_url = f"http://{_E2E_HOST}:{_E2E_PREFERRED_PORT}"
        sync_e2e_base_urls(base_url)
        yield base_url
        return

    # 2) 自起：优先 8000（与历史行为等价），不可绑定/启动失败退随机端口
    #    （P2-TEST-3：bind 冲突曾导致 30 次探测打到旧服务 → RuntimeError
    #    → e2e 整体 ERROR；占用检测在交给 uvicorn 前用裸 socket 完成）
    server = None
    thread = None
    base_url = None
    for port in (_E2E_PREFERRED_PORT, find_free_port()):
        if not port_bindable(_E2E_HOST, port):
            continue
        config = uvicorn.Config(app, host=_E2E_HOST, port=port, log_level="warning")
        candidate_server = uvicorn.Server(config)
        candidate_thread = threading.Thread(target=candidate_server.run, daemon=True)
        candidate_thread.start()
        deadline = time.time() + _SERVER_READY_TIMEOUT_S
        while time.time() < deadline:
            time.sleep(0.1)
            if is_reusable_lujo_memory_server(port, timeout=0.5):
                server, thread, base_url = (
                    candidate_server,
                    candidate_thread,
                    f"http://{_E2E_HOST}:{port}",
                )
                break
        if base_url:
            break
        # 启动失败（TOCTOU 端口被抢等）：尽力回收后换下一候选
        candidate_server.should_exit = True
        candidate_thread.join(timeout=3)

    if base_url is None:
        raise RuntimeError(
            "Failed to start e2e Lujo memory server: port 8000 is not a "
            "reusable Lujo memory instance (P0-TEST-1) and self-start on "
            "fallback ports failed (P2-TEST-3)."
        )

    sync_e2e_base_urls(base_url)
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=3)
