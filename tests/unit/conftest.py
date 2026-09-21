"""单元测试公共 fixture：每个用例前后清空全局 errors 近期缓冲，避免指纹去重导致的跨用例污染。"""
import os

import pytest

from app.config import settings
from app.runtime.core import errors
from app.runtime.core.storage import factory as _storage_factory

# 单元测试强制 memory 存储后端（与 CI 一致），避免本机 .env 的
# STORAGE_BACKEND=postgresql 污染：
#  - 单测固定 request_id（如 "test-batch"）在 PG 上跨运行累积 → len 断言失真
#  - spec_store 测试用 _add_log 注入 traces 表，但 PG 后端恢复走专用 specs 表 → 数据不可见
#
# settings 单例可能在测试基建导入链上游已被实例化（读 .env 得 postgresql），
# 仅设 env 不足以生效：直接改写单例 + 重置 storage factory 缓存，
# 确保所有单元测试首次 get_*_store() 即拿到 memory 后端（与 CI 一致）。
# 需要真实 PG 行为的测试用 monkeypatch 显式覆盖（如 test_factory / test_storage）。
os.environ["STORAGE_BACKEND"] = "memory"
settings.storage_backend = "memory"
# v0.8.0 KB 持久化「笔记本」测试隔离：默认开启会往工作目录写 lujo-kb.sqlite3，
# 这里显式关闭，保持既有用例行为与 v0.7.x 完全一致（不写任何文件）。
# 同时写入 env：子进程类测试（spawn）继承 env，只改单例挡不住子进程。
# SQLite store 自身与其工厂分发由 tests/unit/test_sqlite_kb_store.py 用临时路径覆盖。
os.environ["KB_PERSIST_ENABLED"] = "false"
settings.kb_persist_enabled = False
_storage_factory._trace_store = None
_storage_factory._session_store = None
_storage_factory._error_store = None
_storage_factory._spec_store = None
_storage_factory._knowledge_store = None


@pytest.fixture(autouse=True)
def _isolate_storage():
    """逐用例隔离 storage factory 进程级单例 + errors 近期缓冲（P2-TEST-1）。

    memory 后端的 store 是进程级单例（``app/runtime/core/storage/factory.py``
    的五个引用：trace / session / error / spec / knowledge），此前仅在
    conftest 导入时重置一次，用例间数据累积、顺序敏感。正确范式已存在于
    tests/unit/test_factory.py 的 ``_reset_factory_cache``，这里上移为公共
    autouse fixture；重置项与 factory 单例清单一一对应，errors._recent 沿用
    既有清理，与 tests/integration/conftest.py 同口径。
    """
    errors._recent.clear()
    for _name in ("_trace_store", "_session_store", "_error_store", "_spec_store", "_knowledge_store"):
        setattr(_storage_factory, _name, None)
    yield
    errors._recent.clear()
    for _name in ("_trace_store", "_session_store", "_error_store", "_spec_store", "_knowledge_store"):
        setattr(_storage_factory, _name, None)
