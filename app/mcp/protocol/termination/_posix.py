"""POSIX 终止原语（DESIGN_C3 §1、§2、§3；B15 / W3-2）。

与 :mod:`._win32` 对称的平台件；**ctypes 仅本件内**（且仅 PDEATHSIG 可选
探测使用）。Windows 上导入本模块立即 ImportError——终止后端经
:mod:`app.mcp.protocol.termination` 包的 ``load_platform_module()`` 惰性加载。

纪律（C3 §1 / §5，R10 口径）：

- **killpg 两级**：SIGTERM → 宽限内轮询组空 → 仍存活 SIGKILL → 轮询；一律
  对**出生时保存的 pgid**（:func:`birth_pgid`，spawn 用 ``start_new_session``
  使 pid==pgid），**永不重查**——重查可能命中 pid 复用后的无关组；
- **ESRCH 不升错误**：组空 / 组不存在是终止的正常终态（ProcessLookupError
  静默）；EPERM 等其余错误保留上抛；
- **两判据分开**：直接子收割（``child_gone`` = ``proc.poll() is not None``）
  与组清理（``group_has_members``）是**两个独立判据**——首领先退出而孙进程
  存活时，前者为真后者为假，不得以直接子 exitcode 冒充组清理（C3 §5 R10）；
- **防误发**：拒绝 ``killpg(0)`` / ``killpg(-1)`` 语义（pgid<=1）与对宿主
  自身进程组（``os.getpgrp()``）的两级终止——误发会波及测试宿主/宿主全家；
- 组存在性探测用 ``killpg(pgid, 0)``；单进程存在性用 ``os.kill(pid, 0)``
  （POSIX 上 sig=0 是安全的存在性检查，与 Windows 语义不同）。
"""

from __future__ import annotations

import os
import signal
import sys
import time

if sys.platform == "win32":  # pragma: no cover —— Windows 上禁止导入本平台件
    raise ImportError(
        "app.mcp.protocol.termination._posix 仅可在 POSIX 导入；"
        "Windows 必须经 app.mcp.protocol.termination.load_platform_module() 惰性加载"
    )

# 与 C3 §4 矩阵环境名一致（字符串字面量防漂移；包级同样导出）
BACKEND_NAME = "posix-pgroup"

_POLL_INTERVAL = 0.05


def birth_pgid(proc) -> int:
    """出生时保存的 pgid：spawn 经 ``start_new_session=True`` 使 pid==pgid。

    终止一律对这里保存的值执行；**禁止**事后 ``os.getpgid()`` 重查（R10）。
    """
    return proc.pid


def signal_group(pgid: int, sig: int) -> None:
    """对组发信号；ESRCH（组空 / 不存在）静默；防误发拒绝 pgid<=1。"""
    if pgid <= 1:
        raise ValueError(f"refusing to signal pgid={pgid} (killpg(0)/killpg(-1) guard)")
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return  # ESRCH：组空 / 组不存在——终止的正常终态，不升错误
    except PermissionError:
        raise  # EPERM 属真实权限问题，保留上抛


def group_has_members(pgid: int) -> bool:
    """组清理判据：组内任一进程存活即 True（``killpg(pgid, 0)``）。

    与直接子收割判据（:func:`child_gone`）**分开**；防误发拒绝 pgid<=1。
    """
    if pgid <= 1:
        raise ValueError(f"refusing to probe pgid={pgid} (killpg(0)/killpg(-1) guard)")
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def child_gone(proc) -> bool:
    """直接子收割判据：``proc.poll() is not None``（与组清理判据分开）。"""
    return proc.poll() is not None


def process_exists(pid: int) -> bool:
    """单进程存在性检查（``os.kill(pid, 0)``；POSIX 上安全）。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权限查询
    return True


def terminate_group_two_tier(
    pgid: int, grace: float, *, leader=None, protect_own_group: bool = True
) -> bool:
    """killpg 两级终止：SIGTERM → grace 轮询组空 → SIGKILL → 轮询。

    返回组是否已清空。防误发：拒绝 pgid<=1；默认拒绝宿主自身进程组
    （``protect_own_group``）——两级终止对宿主组等于杀全家。

    ``leader``：该尝试的**直接子** Popen（可选）。轮询期间对其 ``poll()``
    做非阻塞收割——SIGKILL 后未收割的直接子是僵尸，僵尸仍占着进程组，
    ``killpg(pgid, 0)`` 会因此永远报非空（「直接子收割」与「组清理」并行
    推进，但两判据仍各自独立评估，见 :func:`child_gone` /
    :func:`group_has_members`）。
    """
    if pgid <= 1:
        raise ValueError(f"refusing to terminate pgid={pgid} (killpg(0)/killpg(-1) guard)")
    if protect_own_group and pgid == os.getpgrp():
        raise ValueError("refusing to terminate own host process group")
    if not group_has_members(pgid):
        return True  # 组已空：终态提前达成（ESRCH 不升错误）

    def _poll_and_reap() -> bool:
        if leader is not None:
            leader.poll()  # 非阻塞收割僵尸直接子
        return not group_has_members(pgid)

    signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if _poll_and_reap():
            return True
        time.sleep(_POLL_INTERVAL)

    signal_group(pgid, signal.SIGKILL)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if _poll_and_reap():
            return True
        time.sleep(_POLL_INTERVAL)
    if leader is not None:
        leader.poll()
    return not group_has_members(pgid)


def pdeathsig_supported() -> bool | None:
    """可选探测 PR_SET_PDEATHSIG（父死亡信号）；失败返回 None（C3 §3）。

    探测在**本进程**调用 ``prctl(PR_SET_PDEATHSIG, 0)``（值 0 = 清除设置，
    无副作用）；libc 不可用 / 调用失败一律 None，不抛异常。
    """
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        result = libc.prctl(PR_SET_PDEATHSIG, 0, 0, 0, 0)
        return result == 0
    except Exception:  # noqa: BLE001 —— 可选探测，失败即 None
        return None
