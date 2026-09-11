"""重型 MCP 工具的进程隔离执行（FIX: C2 —— 僵尸线程根治）。

背景：重活（如 verify_ui 的 Playwright 自动化）此前跑在重型**线程池**里，超时后
``future.cancel()`` 无法中断已运行的线程 → 线程被卡死任务永久占用，heavy 池仅 2
个 worker，两次超时即被打满、后续全部恒 TOOL_BUSY/TOOL_TIMEOUT，无自愈。**线程
杀不死**是根因。

方案：重活改为**每次调用单独起一个子进程**（spawn），超时则 ``proc.terminate()``
强杀——进程可杀，子进程资源即刻回收。不再有僵尸，也不会打满任何池；将来任何
同步重活都自动受这层保护。

FIX: R6 —— 结果读取必须与子进程运行期重叠（先 join 后 recv 会死锁）：
子进程经 ``conn.send()`` 写回结果，结果大于管道缓冲时 send 阻塞等待父进程
读取；旧实现父进程却先 ``proc.join(timeout)`` 等子进程退出再 ``recv()``，
双方互相等待 → 业务早已完成仍被判超时强杀、结果被丢弃。现改为在子进程
运行期间按截止时间轮询读取结果；收到结果后再收割子进程。

子进程入口 :func:`_heavy_subprocess_entry` 保持轻量（仅 ``importlib`` 动态导入
handler），避免 spawn 重导入 ``server`` 时触发其模块级线程池/信号量等副作用。
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import pickle
import sys
import time

from app.mcp.protocol import heavy_spawn

logger = logging.getLogger("lujo-mcp.mcp.heavy")

# 强杀后等待子进程真正退出的宽限（秒）
_KILL_JOIN_GRACE = 5.0

# PyInstaller 的 Windows spawn 子进程会重新启动冻结的可执行文件。对单文件
# console 程序，multiprocessing 的内部 ``--multiprocessing-fork`` 启动路径
# 可能在 stdio 已被 bootloader 关闭后落入 ``ValueError: I/O operation on
# closed file``，导致重型工具没有任何结果。冻结版改用本项目自己的 worker
# 参数和 pickle 标准流协议；源码运行仍使用 multiprocessing Pipe。
_FROZEN_WORKER_FLAG = "--lujo-heavy-worker"


def _run_async_in_fresh_loop(coro):
    """在全新事件循环中执行协程并收尾（R11）。

    收尾顺序（R11 硬约束）：执行 → ``run_until_complete(shutdown_asyncgens)``
    → ``loop.close()``。shutdown_asyncgens 是协程，必须经 run_until_complete
    await 后再关 loop；不使用 asyncio.run（避免其信号安装/默认 executor
    关闭在 worker 语境的副作用）。
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()


def resolve_and_run(handler_module: str, handler_name: str, arguments, *, abort_event=None):
    """共享单函数：解析并执行 handler（C2 §4；源码/冻结/引导三入口唯一路径）。

    R11 协程单次执行规则：
    - ``async def`` handler：``handler(arguments)`` 返回的**同一协程对象**立即
      交给 :func:`_run_async_in_fresh_loop` 执行，执行后绝不关闭；
    - 同步 handler 返回 coroutine：同一对象交给同一驱动执行（兼容老式
      handler）；**禁止为取第二个协程而重复调用 handler**（同步副作用会执行
      两次）；
    - 仅当执行前已决定放弃（``abort_event`` 已置位，终止/取消先到）才
      ``coro.close()``，并以 :class:`asyncio.CancelledError` 表达放弃；
    - async 分支内对已返回非 coroutine 值直接透传。
    """
    import inspect

    module = importlib.import_module(handler_module)
    handler = getattr(module, handler_name)

    def _drive(coro):
        if abort_event is not None and abort_event.is_set():
            coro.close()  # 仅放弃执行时关闭；已执行的协程绝不 close
            raise asyncio.CancelledError(
                f"heavy handler {handler_name} abandoned before execution"
            )
        return _run_async_in_fresh_loop(coro)

    if inspect.iscoroutinefunction(handler):
        return _drive(handler(arguments))
    result = handler(arguments)
    if inspect.iscoroutine(result):
        return _drive(result)
    return result


def run_frozen_worker_entry(argv: list[str] | None = None) -> int:
    """冻结版入口的最小 worker 分流（由 :mod:`packaging.entry_stdio` 调用）。

    C2 §4：源码/冻结共用同一份 bootstrap——委托 :mod:`app.mcp.protocol.
    heavy_worker_entry`（通道规则、握手时序、结构化兜底只有这一份）。
    惰性导入：entry_stdio 静态导入链保持只有 heavy_process；PyInstaller
    对函数内 import 同样做模块分析（W2-5 冻结 smoke 复核）。
    """
    from app.mcp.protocol.heavy_worker_entry import main as _entry_main

    args = list(sys.argv[1:] if argv is None else argv)
    return _entry_main(args)


def run_heavy_tool_blocking(
    handler_module: str, handler_name: str, arguments: dict, timeout: float
):
    """在子进程执行重型工具并同步等待；截止强杀。本函数运行在一个工作线程内。

    C2 §1/§4：源码与冻结统一走 :mod:`heavy_spawn`（每尝试独立结果通道 + go
    握手 + 单一读取器），**不再有「源码用 Pipe conn / 冻结把 stdout 当结果」
    的分叉**；worker 侧执行统一经 :func:`resolve_and_run`（R11 协程单次执行）。

    Returns:
        handler 的返回值（成功时）。

    Raises:
        asyncio.TimeoutError: 调用截止（子进程已被 terminate 回收）。
        RuntimeError: 握手断裂（TOOL_INTERNAL 口径）、子进程异常退出、
            结果不可序列化或未返回结果。
    """
    if getattr(sys, "frozen", False):
        command = [sys.executable, _FROZEN_WORKER_FLAG, handler_module, handler_name]
    else:
        command = [
            sys.executable, "-m", "app.mcp.protocol.heavy_worker_entry",
            _FROZEN_WORKER_FLAG, handler_module, handler_name,
        ]
    try:
        request = pickle.dumps(arguments, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as exc:
        raise RuntimeError("heavy tool arguments not serializable") from exc

    attempt = heavy_spawn.spawn_attempt(0, command)
    try:
        payload = heavy_spawn.handshake(
            attempt, request, deadline=time.monotonic() + float(timeout),
            allow_commit=lambda: True,  # 注册表四条件闸门自 W3/W4 接入
        )
        status, detail = pickle.loads(payload)
        if status == "ok":
            return detail
        raise RuntimeError(f"heavy tool {handler_name} failed: {detail}")
    except heavy_spawn.HeavyHandshakeTimeout as exc:
        raise asyncio.TimeoutError(
            f"heavy tool {handler_name} timed out after {timeout}s (subprocess killed)"
        ) from exc
    except heavy_spawn.WorkerExitedBeforeReady as exc:
        raise RuntimeError(
            f"heavy tool {handler_name} worker exited before ready "
            f"(exitcode={exc.exitcode})"
        ) from exc
    except heavy_spawn.WorkerExitedWithoutResult as exc:
        raise RuntimeError(
            f"heavy tool {handler_name} exited without result (exitcode={exc.exitcode})"
        ) from exc
    except heavy_spawn.HeavySpawnBroken as exc:
        raise RuntimeError(
            f"heavy tool {handler_name} worker handshake broken: {exc}"
        ) from exc
    finally:
        heavy_spawn.terminate_and_reap(attempt, grace=_KILL_JOIN_GRACE)
