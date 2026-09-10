import os
import threading
import time
import urllib.request

import pytest
import uvicorn

# R8 存储隔离：必须在导入 app.main（其 lifespan 会读 storage_backend）之前
# 强制后端，否则 e2e 全链路会把测试数据写进开发者本机真实 PostgreSQL。
# 需要真库回归时显式设 LUJO_TEST_STORAGE_BACKEND=postgresql。
_FORCED_BACKEND = os.environ.get("LUJO_TEST_STORAGE_BACKEND", "memory").lower()
if _FORCED_BACKEND not in ("memory", "postgresql"):
    raise RuntimeError(
        f"LUJO_TEST_STORAGE_BACKEND 非法: {_FORCED_BACKEND!r}（可选 memory / postgresql）"
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
