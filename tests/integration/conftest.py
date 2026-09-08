"""集成测试存储隔离 —— 与 tests/unit/conftest.py 同一口径（R8）。

背景（2026-09-08 发现）：集成测试此前没有强制存储后端，跟随 .env 的
STORAGE_BACKEND=postgresql 连到了开发者本机自启的真实 PostgreSQL：

1. 每次跑测试都会把测试数据写进真实库（traces / errors / specs 等表）；
2. 同一条用例在两种后端上行为不同：PG 的 ``data JSONB`` 列读回来自动是
   dict，memory 原样存取（字符串仍是字符串）——
   ``test_redaction_integration`` 的 dict 断言因此"单独跑绿、混跑红"。

现与 unit 一致：默认强制 memory 并重置存储工厂单例，跑测试不再碰真库。
需要真库回归时显式设 ``LUJO_TEST_STORAGE_BACKEND=postgresql``；pg 标记用例
自身有 ``_require_pg`` 守卫，后端不是 postgresql 时自动 skip（与 CI 口径一致）。
"""
import os

# 必须在 app.config 被本 conftest 导入前设置好 env：与 unit conftest 相同的
# 时序约束（settings 单例可能已被 tests/__init__ 导入链提前创建）。
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

# 工厂是模块级单例缓存：集成测试进程内可能已有别的测试建过 PG store，
# 这里统一重置，保证首个 get_*_store() 按 _FORCED_BACKEND 重新初始化。
for _name in ("_trace_store", "_session_store", "_error_store", "_spec_store", "_knowledge_store"):
    setattr(_storage_factory, _name, None)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_storage():
    """每条用例前后重置存储单例，避免用例间通过缓存的 store 互相污染。"""
    from app.runtime.core import errors

    errors._recent.clear()
    yield
    errors._recent.clear()
