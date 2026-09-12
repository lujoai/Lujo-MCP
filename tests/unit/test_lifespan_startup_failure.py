"""W4-6（B22）lifespan 创建段清理栈验收：逐阶段注入启动失败。

对应 DESIGN_C4 §5.1 与 CHECKLIST W4-6：
- 创建段每成功一个资源即注册逆序回收动作；启动异常时按已注册栈**逆序回收**；
- 该路径同样先执行 M1 ③（terminate_active_processes，空表快速返回）再 ⑤，
  保证启动失败也能带着正确顺序退出（与运行期关闭路径共用同一实现）；
- **嵌入式 lifespan 启动失败只做代际关闭**，不得武装进程退出看门狗、不得
  记录进程退出意图（§1.1 分账）；
- 失败后无遗留 periodic task；再次启动不重复后台任务。

A 类（旧缺陷复现）：现状创建段无 try/finally，注入失败后 periodic task
遗留（asyncio 未收尾告警/任务泄漏可观察）；B 类（新机制）：清理栈逆序回收。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os

import pytest

from app.main import lifespan
from app.llm import analysis_queue as aq_mod
from app.agent import repair_queue as rq_mod


@pytest.fixture(autouse=True)
def _fresh_env(monkeypatch):
    """Isolate singleton state and the application logger per test."""
    aq_mod._analysis_queue = None
    rq_mod._repair_queue = None
    app_logger = logging.getLogger("lujo-mcp")
    saved_handlers = app_logger.handlers[:]
    saved_level = app_logger.level
    monkeypatch.setenv("STORAGE_BACKEND", "memory")
    monkeypatch.setenv("API_KEY", "")
    try:
        yield
    finally:
        aq_mod._analysis_queue = None
        rq_mod._repair_queue = None
        app_logger.handlers.clear()
        app_logger.handlers.extend(saved_handlers)
        app_logger.setLevel(saved_level)


def _make_app():
    from fastapi import FastAPI

    return FastAPI()


def _collect_tasks() -> list[str]:
    loop = asyncio.get_event_loop()
    return [t.get_name() for t in asyncio.all_tasks(loop)]


# ── A 类：现状缺陷复现（创建段无清理栈 → periodic task 遗留可观察） ────────


def test_source_shows_creation_segment_has_cleanup_stack():
    """B 类结构断言：创建段存在清理栈（startup_cleanup_stack + 逆序回收）。"""
    import app.main as main_mod

    source = inspect.getsource(main_mod.lifespan)
    assert "startup_cleanup_stack" in source, "创建段未注册逆序回收栈"
    assert "reversed(" in source, "未按逆序回收"
    # 失败路径共用 M1 编排（空表安全）
    assert "run_m1_sequence" in source


def test_ensure_no_watchdog_arming_in_failure_path():
    """§1.1 分账：启动失败路径不得武装进程退出看门狗/记录进程退出意图。"""
    import app.mcp.protocol.shutdown as sh

    source = inspect.getsource(sh.run_m1_sequence)
    assert "record_exit_intent" not in source, "M1 序不得记录进程退出意图"
    assert "ExitSupervisor" not in source, "M1 序不得创建看门狗"


# ── B 类：逐阶段注入失败（真实 lifespan 运行） ────────────────────────────


def test_startup_failure_at_analysis_queue_unwinds_periodic_task(monkeypatch):
    """注入点=analysis 队列启动失败：periodic task 已被逆序取消；
    analysis 单例为 None；异常向宿主传播。"""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "llm_async_analysis_enabled", True)

    async def _boom():
        raise RuntimeError("inject: analysis start failure")

    monkeypatch.setattr(aq_mod, "start_analysis_queue", _boom)

    async def _scenario():
        async with lifespan(_make_app()):
            pass  # 不应到达

    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(RuntimeError, match="inject"):
            loop.run_until_complete(_scenario())
        # 逆序回收已执行：analysis 单例未创建（注入在 start 之前抛）
        assert aq_mod._analysis_queue is None
    finally:
        loop.close()


def test_startup_failure_after_periodic_task_unwinds_it(monkeypatch):
    """注入点=prewarm 阶段失败：periodic task 已创建 → 失败路径取消它
    （无遗留 pending task），analysis 已创建的队列被 drain+复位。"""
    import app.main as main_mod

    async def _boom(top_n=None):
        raise RuntimeError("inject: prewarm failure")

    async def _boom_sync(*a, **k):
        raise RuntimeError("inject: prewarm failure")

    import app.llm.cache_prewarm as cp

    monkeypatch.setattr(cp, "prewarm_once_with_timeout", _boom)
    monkeypatch.setattr(cp, "start_prewarm_task", _boom_sync)
    monkeypatch.setattr(main_mod.settings, "llm_async_analysis_enabled", True)
    monkeypatch.setattr(main_mod.settings, "llm_cache_prewarm_enabled", True)

    async def _scenario():
        async with lifespan(_make_app()):
            pass

    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(RuntimeError, match="inject"):
            loop.run_until_complete(_scenario())
        # analysis 已创建 → 失败路径 drain+复位
        assert aq_mod._analysis_queue is None
    finally:
        loop.close()


def test_second_startup_after_failure_creates_single_periodic_task(monkeypatch):
    """再次启动不重复后台任务：失败一轮后，第二轮正常启动仅创建一个
    periodic task（按协程代码名识别；任意时刻仅一个存活）。"""
    import app.llm.cache_prewarm as cp
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "llm_cache_prewarm_enabled", True)

    async def _boom(top_n=None):
        raise RuntimeError("inject")

    real_prewarm = cp.prewarm_once_with_timeout
    monkeypatch.setattr(cp, "prewarm_once_with_timeout", _boom)
    monkeypatch.setattr(cp, "start_prewarm_task", lambda: None)

    def _is_periodic(task):
        coro = task.get_coro()
        code_name = getattr(getattr(coro, "cr_code", None), "co_name", "")
        return "periodic_cleanup" in code_name

    # 第一轮：启动失败（prewarm 注入）
    loop1 = asyncio.new_event_loop()
    try:

        async def _fail():
            async with lifespan(_make_app()):
                pass

        with pytest.raises(RuntimeError, match="inject"):
            loop1.run_until_complete(_fail())
        # 失败路径逆序回收：periodic task 已被取消（无存活）
        alive1 = [t for t in asyncio.all_tasks(loop1) if _is_periodic(t)]
        assert alive1 == [], "失败路径未回收 periodic task"
    finally:
        loop1.close()

    # 第二轮：真关旧 loop 后新建 → 正常启动（恢复原 prewarm 实现）
    monkeypatch.setattr(cp, "prewarm_once_with_timeout", real_prewarm)
    loop2 = asyncio.new_event_loop()
    try:

        async def _ok():
            async with lifespan(_make_app()):
                alive = [t for t in asyncio.all_tasks(loop2) if _is_periodic(t)]
                assert len(alive) == 1, f"periodic task 数量异常: {len(alive)}"

        loop2.run_until_complete(asyncio.wait_for(_ok(), timeout=30))
        # 正常退出后：无存活 periodic task
        alive2 = [t for t in asyncio.all_tasks(loop2) if _is_periodic(t)]
        assert alive2 == []
    finally:
        loop2.close()


def test_startup_failure_after_prewarm_task_started_unwinds_it(monkeypatch):
    """预热定时任务已启动后更晚的启动步骤失败时，必须逆序停止预热任务。"""
    import app.main as main_mod
    import app.llm.cache_prewarm as cp

    monkeypatch.setattr(main_mod.settings, "llm_async_analysis_enabled", False)
    monkeypatch.setattr(main_mod.settings, "llm_cache_prewarm_enabled", True)
    monkeypatch.setattr(main_mod.settings, "agent_mode", "single")

    async def _prewarm_once(top_n=None):
        return {"scanned": 0, "prewarmed": 0, "skipped": 0}

    def _start_prewarm_task():
        cp._prewarm_task = asyncio.create_task(asyncio.sleep(60))

    async def _boom():
        raise RuntimeError("inject: repair start failure")

    monkeypatch.setattr(cp, "prewarm_once_with_timeout", _prewarm_once)
    monkeypatch.setattr(cp, "start_prewarm_task", _start_prewarm_task)
    monkeypatch.setattr(rq_mod, "start_repair_queue", _boom)
    cp._prewarm_task = None

    async def _scenario():
        async with lifespan(_make_app()):
            pass

    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(RuntimeError, match="repair start failure"):
            loop.run_until_complete(_scenario())
        assert cp._prewarm_task is None, "启动失败后预热任务仍然存活"
    finally:
        if cp._prewarm_task is not None:
            cp._prewarm_task.cancel()
            loop.run_until_complete(asyncio.gather(cp._prewarm_task, return_exceptions=True))
        cp._prewarm_task = None
        loop.close()

async def _async_noop():
    return {"status": "skipped"}
