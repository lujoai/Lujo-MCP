"""W14 / P1-ARC-1：写入侧 → 展示侧的缓存失效广播（app/runtime/core/invalidation.py）。

runtime 是下层，不得反向 import api。本文件验证替换掉「三处惰性
``from app.api.dashboard import invalidate_cache`` + ``except Exception: pass``」
之后的行为契约：广播确实到达订阅者、单个订阅者失败不穿透写入主链路也不再被
静默吞掉、按名重复注册是覆盖而不是叠加、生产注册点是晚绑定。
"""

from __future__ import annotations

import inspect
import logging
import re

import pytest

# 必须在 fixture 取快照**之前**导入：dashboard 在导入期注册失效监听器，若它是在
# 某个用例执行期间才第一次被导入，注册就发生在快照之后，teardown 还原快照时会把
# 它一并抹掉 —— 字母序排在本文件之后、又依赖该监听器的用例（如
# test_batch_writes 的「批量写入只失效一次」）会静默失去被测行为。
import app.api.dashboard  # noqa: F401
from app.runtime.core import invalidation


@pytest.fixture(autouse=True)
def _clean_listeners():
    """登记表是进程级全局：用例前清空、用例后原样还原（不得残留给后续用例）。"""
    saved = dict(invalidation._listeners)
    invalidation._listeners.clear()
    yield
    invalidation._listeners.clear()
    invalidation._listeners.update(saved)
    # 还原后必须仍包含生产监听器，否则本文件之后的用例会失去 dashboard 失效行为
    assert "dashboard" in invalidation._listeners, (
        "dashboard 的失效监听器在还原后丢失（导入期注册被 fixture 抹掉）"
    )


def test_notify_reaches_every_listener_once():
    calls: list[str] = []
    invalidation.register_invalidation_listener("a", lambda: calls.append("a"))
    invalidation.register_invalidation_listener("b", lambda: calls.append("b"))

    invalidation.notify_data_written()

    assert sorted(calls) == ["a", "b"]


def test_same_name_registration_overrides_not_stacks():
    """模块可能被重复导入、测试可能重复注册：叠加会让一次写入触发 N 次失效。"""
    calls: list[int] = []
    invalidation.register_invalidation_listener("dash", lambda: calls.append(1))
    invalidation.register_invalidation_listener("dash", lambda: calls.append(2))

    invalidation.notify_data_written()

    assert calls == [2]
    assert invalidation.listener_names() == ["dash"]


def test_listener_failure_does_not_propagate_and_is_not_silent(caplog):
    """旧行为是 ``except Exception: pass`` —— 缓存失效故障无人知晓。"""

    def _boom():
        raise RuntimeError("listener exploded")

    reached: list[str] = []
    invalidation.register_invalidation_listener("bad", _boom)
    invalidation.register_invalidation_listener("good", lambda: reached.append("good"))

    with caplog.at_level(logging.WARNING, logger="lujo-mcp.runtime.invalidation"):
        invalidation.notify_data_written()  # 不得抛

    assert reached == ["good"], "一个监听器失败不得影响其余监听器"
    assert "缓存失效监听器" in caplog.text or "bad" in caplog.text, (
        "失败必须留痕，不能像旧实现那样静默吞掉"
    )


def test_unregister_is_idempotent():
    calls: list[int] = []
    invalidation.register_invalidation_listener("x", lambda: calls.append(1))
    invalidation.unregister_invalidation_listener("x")
    invalidation.unregister_invalidation_listener("x")  # 未登记也不报错

    invalidation.notify_data_written()

    assert calls == []
    assert invalidation.listener_names() == []


def test_notify_without_listeners_is_noop():
    """纯 stdio 模式不导入 dashboard → 没有订阅者，广播必须是空操作。"""
    assert invalidation.listener_names() == []
    invalidation.notify_data_written()


def test_late_binding_wrapper_follows_module_attribute(monkeypatch):
    """晚绑定包装才能被 monkeypatch 换掉（注册函数对象本身则换不掉）。"""
    import app.api.dashboard as dashboard_module

    calls: list[str] = []
    monkeypatch.setattr(
        dashboard_module, "invalidate_cache", lambda *a, **kw: calls.append("patched")
    )
    invalidation.register_invalidation_listener(
        "dashboard", lambda: dashboard_module.invalidate_cache()
    )

    invalidation.notify_data_written()

    assert calls == ["patched"]


def test_dashboard_registers_late_binding_listener_in_production():
    """生产注册点必须是晚绑定 lambda。

    ``tests/unit/test_batch_writes.py`` 用
    ``monkeypatch.setattr("app.api.dashboard.invalidate_cache", mock)`` 断言
    「批量写入只触发一次 invalidate_cache」；若注册的是函数对象本身，那条既有
    用例会静默失效（mock 永远不被调用），断言变成恒真。
    """
    import app.api.dashboard as dashboard_module

    src = inspect.getsource(dashboard_module)
    assert "register_invalidation_listener" in src, "dashboard 未登记失效监听器"
    assert re.search(
        r"register_invalidation_listener\(\s*[\"'][^\"']+[\"']\s*,\s*lambda", src
    ), "注册的不是晚绑定包装，monkeypatch 换不掉 invalidate_cache"
