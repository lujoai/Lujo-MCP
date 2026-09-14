import os
import threading
import time
import urllib.request

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


@pytest.fixture(scope="session", autouse=True)
def e2e_server():
    """Ensure a local test server is running for e2e tests."""
    url = "http://127.0.0.1:8000/demo"
    # Check if server is already running
    try:
        with urllib.request.urlopen(url, timeout=1) as resp:
            if resp.status == 200:
                yield
                return
    except Exception:
        pass

    config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to start
    started = False
    for _ in range(30):
        time.sleep(0.1)
        try:
            with urllib.request.urlopen(url, timeout=0.5) as resp:
                if resp.status == 200:
                    started = True
                    break
        except Exception:
            pass

    if not started:
        raise RuntimeError("Failed to start e2e test uvicorn server")

    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=3)
