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
import multiprocessing as mp
import time

logger = logging.getLogger("lujo-mcp.mcp.heavy")

# 强杀后等待子进程真正退出的宽限（秒）
_KILL_JOIN_GRACE = 5.0
# 结果管道轮询步长（秒）：细粒度检查"子进程已退出且无数据"，保证超时精度
_RESULT_POLL = 0.05
# 收到结果后等子进程自行退出的宽限（秒）；超时仍存活则 terminate（防残留）
_POST_RECV_JOIN_GRACE = 5.0


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


def _terminate_subprocess(proc: mp.Process, handler_name: str, reason: str) -> None:
    """强杀子进程并确保退出（terminate → kill 两级升级），杜绝残留进程。"""
    proc.terminate()
    proc.join(timeout=_KILL_JOIN_GRACE)
    if proc.is_alive():
        # SIGTERM 级 terminate 未生效（极端钉死）：升级 SIGKILL
        proc.kill()
        proc.join(timeout=_KILL_JOIN_GRACE)
    logger.warning(
        "重型工具 %s 子进程(pid=%s)已强制回收（%s）",
        handler_name, proc.pid, reason,
    )


def run_heavy_tool_blocking(
    handler_module: str, handler_name: str, arguments: dict, timeout: float
):
    """在子进程执行重型工具并同步等待；超时强杀。本函数运行在一个工作线程内。

    FIX: R6 —— 等待/读取共用同一个截止时间：子进程运行期间即开始读结果
    （poll 轮询 + recv），大结果不再因"先 join 后 recv"的相互等待被误判
    超时强杀。

    Returns:
        handler 的返回值（成功时）。

    Raises:
        asyncio.TimeoutError: 超时（子进程已被 ``terminate`` 回收）。
        RuntimeError: 子进程异常退出、结果不可序列化或未返回结果。
    """
    ctx = mp.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_heavy_subprocess_entry,
        args=(handler_module, handler_name, arguments, child_conn),
    )
    proc.start()
    deadline = time.monotonic() + float(timeout)
    try:
        child_conn.close()  # 父进程只读

        # ── R6：子进程运行期间读结果（截止时间控制轮询，不再先 join）──
        received = False
        status: str | None = None
        payload = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # 超时：强杀子进程（进程可杀），回收资源；父侧等待线程随即释放
                _terminate_subprocess(proc, handler_name, f"timed out(>{timeout}s)")
                raise asyncio.TimeoutError(
                    f"heavy tool {handler_name} timed out after {timeout}s (subprocess killed)"
                )
            if parent_conn.poll(min(remaining, _RESULT_POLL)):
                try:
                    status, payload = parent_conn.recv()
                    received = True
                except EOFError:
                    # 写端关闭但无完整消息：子进程未返回结果
                    received = False
                break
            if not proc.is_alive() and not parent_conn.poll(0):
                # 子进程已退出且管道无数据：避免白等到截止时间
                break

        if received:
            # 收割子进程：正常应已退出；短宽限后仍存活则强杀，杜绝残留
            proc.join(timeout=min(_POST_RECV_JOIN_GRACE, _KILL_JOIN_GRACE))
            if proc.is_alive():
                _terminate_subprocess(proc, handler_name, "lingering after sending result")
            if status == "ok":
                return payload
            raise RuntimeError(f"heavy tool {handler_name} failed: {payload}")

        # 未收到结果：区分"子进程异常退出"与"已到截止时间"
        if proc.is_alive():
            _terminate_subprocess(proc, handler_name, f"timed out(>{timeout}s)")
            raise asyncio.TimeoutError(
                f"heavy tool {handler_name} timed out after {timeout}s (subprocess killed)"
            )
        try:
            parent_conn.poll(_RESULT_POLL)  # 兜底：退出竞态下最后读一次
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
        # 兜底清理：任何异常路径都不留存活子进程
        try:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=_KILL_JOIN_GRACE)
                if proc.is_alive():
                    proc.kill()
                    proc.join(timeout=_KILL_JOIN_GRACE)
        except Exception:
            pass
        try:
            parent_conn.close()
        except Exception:
            pass
