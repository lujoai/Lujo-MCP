"""重型工具进程隔离（C2）的自测辅助函数。

仅供测试 :func:`app.mcp.protocol.heavy_process.run_heavy_tool_blocking` 使用：
这些函数会被当作"重型工具"在 **子进程** 中执行，用于验证 spawn 派发、结果回传
与超时强杀。保持零第三方依赖、无副作用、可被 spawn 子进程按引用导入。
"""
from __future__ import annotations

import time


def quick_ok(arguments: dict) -> dict:
    """立即成功返回（回显入参），验证子进程执行 + 结果回传。"""
    return {"ok": True, "echo": arguments.get("x")}


def boom(arguments: dict) -> dict:
    """抛出异常，验证子进程内异常被结构化回传为错误。"""
    raise ValueError("intentional failure for heavy selftest")


def slow_hang(arguments: dict) -> dict:
    """长时间挂起，验证超时强杀（父进程应在 timeout 后 terminate 本子进程）。"""
    time.sleep(float(arguments.get("sleep", 30)))
    return {"ok": True}
