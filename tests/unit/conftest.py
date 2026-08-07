"""单元测试公共 fixture：每个用例前后清空全局 errors 近期缓冲，避免指纹去重导致的跨用例污染。"""
import pytest

from app.runtime.core import errors


@pytest.fixture(autouse=True)
def _isolate_errors_store():
    errors._recent.clear()
    yield
    errors._recent.clear()
