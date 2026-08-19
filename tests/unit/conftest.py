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
_storage_factory._trace_store = None
_storage_factory._session_store = None
_storage_factory._error_store = None
_storage_factory._spec_store = None
_storage_factory._knowledge_store = None


@pytest.fixture(autouse=True)
def _isolate_errors_store():
    errors._recent.clear()
    yield
    errors._recent.clear()
