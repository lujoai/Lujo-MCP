"""重型 MCP 工具的进程隔离执行（FIX: C2 —— 僵尸线程根治）。

背景：重活（如 verify_ui 的 Playwright 自动化）此前跑在重型**线程池**里，超时后
``future.cancel()`` 无法中断已运行的线程 → 线程被卡死任务永久占用，heavy 池仅 2
个 worker，两次超时即被打满、后续全部恒 TOOL_BUSY/TOOL_TIMEOUT，无自愈。**线程
杀不死**是根因。

方案：重活改为**每次调用单独起一个子进程**（spawn）。父进程侧在独立线程里
``proc.join(timeout)`` 等待；超时则 ``proc.terminate()`` 强杀——进程可杀（已实测
``exitcode=-15``、``is_alive()=False``），子进程资源即刻回收，父侧等待线程也随之
释放。不再有僵尸，也不会打满任何池；将来任何同步重活都自动受这层保护。

子进程入口 :func:`_heavy_subprocess_entry` 保持轻量（仅 ``importlib`` 动态导入
handler），避免 spawn 重导入 ``server`` 时触发其模块级线程池/信号量等副作用。
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import multiprocessing as mp

logger = logging.getLogger("lujo-mcp.mcp.heavy")

# 强杀后等待子进程真正退出的宽限（秒）
_KILL_JOIN_GRACE = 5.0
# 子进程已退出后读取结果管道的轮询上限（秒）
_RESULT_POLL = 1.0


def _heavy_subprocess_entry(handler_module: str, handler_name: str, arguments: dict, conn) -> None:
    """子进程入口：动态导入并执行 handler，把 ``(状态, 载荷)`` 写回管道。

    用 ``Pipe`` 而非 ``Queue``：Queue 依赖后台 feeder 线程，子进程 ``put`` 后立即
    退出可能来不及冲刷导致父进程读空；Pipe 单条消息无此问题。
    """
    try:
        module = importlib.import_module(handler_module)
        handler = getattr(module, handler_name)
        result = handler(arguments)
        try:
            conn.send(("ok", result))
        except Exception:
            # 结果不可 pickle 等：退化为错误，避免父进程读不到任何信息
            conn.send(("error", "heavy tool result not serializable"))
    except Exception as exc:  # noqa: BLE001 —— 子进程内兜底，转成结构化错误回传
        try:
            conn.send(("error", f"{type(exc).__name__}: {exc}"))
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_heavy_tool_blocking(
    handler_module: str, handler_name: str, arguments: dict, timeout: float
):
    """在子进程执行重型工具并同步等待；超时强杀。本函数运行在一个工作线程内。

    Returns:
        handler 的返回值（成功时）。

    Raises:
        asyncio.TimeoutError: 超时（子进程已被 ``terminate`` 回收）。
        RuntimeError: 子进程异常退出或未返回结果。
    """
    ctx = mp.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_heavy_subprocess_entry,
        args=(handler_module, handler_name, arguments, child_conn),
    )
    proc.start()
    try:
        child_conn.close()  # 父进程只读
        proc.join(timeout)
        if proc.is_alive():
            # 超时：强杀子进程（进程可杀），回收资源；父侧等待线程随即释放
            proc.terminate()
            proc.join(timeout=_KILL_JOIN_GRACE)
            if proc.is_alive():
                # SIGTERM 级 terminate 未生效（极端钉死）：升级 SIGKILL，杜绝孤儿进程
                proc.kill()
                proc.join(timeout=_KILL_JOIN_GRACE)
            logger.warning(
                "重型工具 %s.%s 超时(>%ss)，子进程(pid=%s)已强杀回收",
                handler_module, handler_name, timeout, proc.pid,
            )
            raise asyncio.TimeoutError(
                f"heavy tool {handler_name} timed out after {timeout}s (subprocess killed)"
            )
        # 子进程已在时限内退出，读取结果
        try:
            if parent_conn.poll(_RESULT_POLL):
                status, payload = parent_conn.recv()
                if status == "ok":
                    return payload
                raise RuntimeError(f"heavy tool {handler_name} failed: {payload}")
        except EOFError:
            pass
        raise RuntimeError(
            f"heavy tool {handler_name} exited without result (exitcode={proc.exitcode})"
        )
    finally:
        try:
            parent_conn.close()
        except Exception:
            pass
