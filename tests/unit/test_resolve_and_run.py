"""W2-3（R11）resolve_and_run 共享单函数验收测试。

四态用例（CHECKLIST W2-3 先红后绿）：async def 正常 / def 返回 coroutine 且
副作用计数 == 1 / handler 抛异常 / 执行前放弃（终止先到）。核心不变量：
协程**同对象只执行一次**；仅在放弃执行时 close()；**禁止为取第二个协程而
重复调用 handler**；async loop 关闭前先 run_until_complete(shutdown_asyncgens)。

B 类·新机制验收：断言新构件不变量；「函数不存在」不构成红灯证据（C1 §7）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import warnings

import pytest

from app.mcp.protocol.heavy_process import resolve_and_run

_MODULE_SOURCE = '''\
import asyncio
SYNC_CALLS = {"n": 0}
CORO_BODY_RAN = {"n": 0}


def handler(arguments):
    """def 返回 coroutine（老式 handler）：同一协程对象只执行一次。"""
    SYNC_CALLS["n"] += 1
    return _coro()


async def _coro():
    CORO_BODY_RAN["n"] += 1
    await asyncio.sleep(0)
    return {"done": True}


def async_handler(arguments):
    """async def：直接返回协程。"""
    return _coro()


def boom_handler(arguments):
    raise ValueError("r11 boom")


def asyncgen_handler(arguments):
    """故意不耗尽 async gen：验证 shutdown_asyncgens 先于 loop.close()。"""
    async def _inner():
        async def _gen():
            yield 1
            yield 2
        g = _gen()
        async for _ in g:
            break
        await asyncio.sleep(0)
        return {"ok": True}
    return _inner()
'''


def _load_module(name: str):
    import os

    path = os.path.join(tempfile.gettempdir(), f"{name}.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_MODULE_SOURCE)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _unload_module(name: str):
    sys.modules.pop(name, None)


def test_async_def_handler_runs_once_and_returns():
    """async def 正常：经 module/name 走 resolve_and_run，协程执行一次。"""
    name = "_r11_async_mod"
    module = _load_module(name)
    try:
        result = resolve_and_run(name, "async_handler", {"x": 21})
        assert result == {"done": True}
        assert module.CORO_BODY_RAN["n"] == 1
        assert module.SYNC_CALLS["n"] == 0  # async def 路径不走同步 handler 计数
    finally:
        _unload_module(name)


def test_def_returning_coroutine_executes_same_object_once():
    """def 返回 coroutine：handler 只被调用一次（副作用计数 == 1），返回的
    同一协程对象被执行、结果透传（R11：禁止为取第二个协程重复调用 handler）。"""
    name = "_r11_def_coroutine_mod"
    module = _load_module(name)
    try:
        result = resolve_and_run(name, "handler", {})
        assert result == {"done": True}
        assert module.SYNC_CALLS["n"] == 1  # handler（含副作用）只执行一次
        assert module.CORO_BODY_RAN["n"] == 1
    finally:
        _unload_module(name)


def test_handler_exception_propagates():
    """handler 抛异常：resolve_and_run 原样透传（由入口层结构化兜底）。"""
    name = "_r11_boom_mod"
    _load_module(name)
    try:
        with pytest.raises(ValueError, match="r11 boom"):
            resolve_and_run(name, "boom_handler", {})
    finally:
        _unload_module(name)


def test_abandoned_before_execution_closes_coroutine_without_running():
    """执行前放弃（abort_event 已置位）：协程被 close() 而非执行——协程体内
    副作用不发生；handler 只被调用一次；不产生「coroutine never awaited」
    RuntimeWarning（未 close 的协程被销毁时必触发，simplefilter('error')
    使其变为测试失败）。"""
    name = "_r11_abort_mod"
    module = _load_module(name)
    try:
        abort = asyncio.Event()
        abort.set()
        with pytest.raises(asyncio.CancelledError):
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                resolve_and_run(name, "handler", {}, abort_event=abort)
        assert module.CORO_BODY_RAN["n"] == 0  # 协程体未执行
        assert module.SYNC_CALLS["n"] == 1  # handler 只调用一次
    finally:
        _unload_module(name)


def test_asyncgen_shutdown_before_loop_close():
    """loop 关闭前必须 run_until_complete(shutdown_asyncgens)（R11）：handler
    内未耗尽的 async gen 在返回后不产生「coroutine/asyncgen ignored」告警。"""
    name = "_r11_asyncgen_mod"
    _load_module(name)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            result = resolve_and_run(name, "asyncgen_handler", {})
        assert result == {"ok": True}
    finally:
        _unload_module(name)
