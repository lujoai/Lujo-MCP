"""heavy worker 终止后端编排（DESIGN_C3 §2.1、§2.2、§3；B15 / W3-3）。

职责边界（结构性保证）：本模块只负责「spawn + Job 收编 + 降级链 + 终止序列」，
**不接触 handshake / go / 业务执行**——「go 已提交的尝试不允许重试执行业务」
由链路仅在 go 前运行这一形态保证。

①②③ 降级链（C3 §2.1，Windows）：

1. 普通 spawn → ``ProcessTreeJob.create()`` → ``assign`` 成功 → **job**
   （整树 + KILL_ON_JOB_CLOSE 兜底）；
2. assign 失败且 ``ERROR_ACCESS_DENIED(5)`` → **换胎重试一次**：旧尝试必须有
   实际进程退出证据且资源收尾完（``_closeout_old_attempt`` 有界确认），新
   尝试 ``attempt_id`` 递增并带 ``CREATE_BREAKAWAY_FROM_JOB`` → 成功为
   **breakaway+job**；
3. 仍失败 / 清理超时 → **direct-child**（warning + 结构化标注）。清理超时则
   **停止重试**——当前（未收编）尝试本身就是 direct-child，继续用它，不得用
   superseded 标记冒充退出。

⚠️ **direct-child 的已知边界（W12 / P3-HEAVY-3；无干净修法，勿当待办重开）**：
没有 Job 就只能对**直接子进程**发 terminate/kill，孙进程（Playwright 拉起的
浏览器树）必然孤儿化，``close_all_jobs`` 对 direct-child 条目也是空操作。
Windows 上缺了 Job 就没有整树终止的原生手段——``taskkill /T`` 要额外起进程、
``psutil`` 是新增依赖（须作者批准），两者都属**新增终止机制**而不是修复本项。
故按「已知边界 + 可见告警」处置：三条降级路径均打 warning，``backend`` 字段随
结构化通告对外可见（``test_u09_playwright_tree.py`` 即断言它）。

防误发闸门：换胎前经 ``gate`` 复查 closing / kill_due（C1 §1.2：kill_due 跨
尝试不清零；不允许继续则返回 ``aborted=True`` 的当前尝试，调用方不得写 go）。
**该标记自 W12 起有生产消费者**：``heavy_process.run_heavy_tool_blocking``
在 spawn 后短路 ``aborted`` 的尝试，并把它同时喂给 GO 提交闸门
（``allow_commit``）——此前两者都不消费，闸门是空壳（P2-HEAVY-1）。

协作级（CTRL_BREAK）按**条目能力快照** ``console_reachable`` 判定（C3 §2.2
R14）——不按宿主实时控制台推断；快照为 False（如 CREATE_NO_WINDOW 子进程）
时终止序列直接跳过协作级。POSIX 路径（``posix-pgroup``）：spawn 用
``start_new_session``（pgid==pid 出生保存），终止走两级 killpg（W3-2）。
"""

from __future__ import annotations

import logging
import subprocess
import sys

from app.mcp.protocol.heavy_spawn import spawn_attempt
from app.mcp.protocol.termination import (
    BACKEND_BREAKAWAY_JOB,
    BACKEND_DIRECT_CHILD,
    BACKEND_JOB,
    BACKEND_POSIX_PGROUP,
)

if sys.platform == "win32":
    from app.mcp.protocol.termination._win32 import (
        CREATE_BREAKAWAY_FROM_JOB,
        ProcessTreeJob,
        console_present,
        generate_console_break,
        is_access_denied,
    )

    _WINDOWS = True
else:
    from app.mcp.protocol.termination import _posix as _plat

    _WINDOWS = False

logger = logging.getLogger("lujo-mcp.termination")

# 旧尝试清理确认的默认有界宽限（秒）；超时即停止重试（不冒充退出）
_CLOSEOUT_GRACE = 3.0


class AttemptBackend:
    """一次尝试的终止后端事实（条目能力快照，C3 §3 R14）。"""

    def __init__(
        self,
        backend: str,
        job=None,
        console_reachable: bool = True,
        aborted: bool = False,
    ):
        self.backend = backend
        self.job = job
        self.console_reachable = console_reachable
        self.aborted = aborted


def spawn_with_backend(
    attempt_id: int,
    command: list[str],
    *,
    env: dict | None = None,
    cwd: str | None = None,
    gate=None,
    closeout_grace: float = _CLOSEOUT_GRACE,
):
    """spawn + Job 收编 + ①②③ 降级链；返回 (attempt, AttemptBackend)。

    ``gate``：换胎前复查（closing / kill_due——kill_due 跨尝试不清零）；
    拒绝则返回 ``aborted=True`` 的当前尝试，调用方不得写 go。
    """
    attempt_id = int(attempt_id)

    if not _WINDOWS:
        attempt = spawn_attempt(
            attempt_id, command, env=env, cwd=cwd, start_new_session=True
        )
        return attempt, AttemptBackend(BACKEND_POSIX_PGROUP, None, console_reachable=True)

    # ① 普通 spawn + Assign
    attempt = spawn_attempt(attempt_id, command, env=env, cwd=cwd)
    job = ProcessTreeJob.create()
    if job.assign(int(attempt.proc._handle)):
        return attempt, AttemptBackend(BACKEND_JOB, job, console_reachable=console_present())
    err = job.last_assign_winerror
    job.close_once()  # 收编失败的 Job 立即收口（空 Job，KILL 无副作用）

    if not is_access_denied(err):
        logger.warning(
            "heavy worker Assign 失败（winerror=%s），降级 direct-child（attempt_id=%s）",
            err, attempt_id,
        )
        return attempt, AttemptBackend(BACKEND_DIRECT_CHILD, None, console_reachable=console_present())

    # ② ACCESS_DENIED：breakaway 重试一次——先复查 gate，再收口旧尝试
    if gate is not None and not gate():
        logger.warning("heavy worker 换胎被 gate 拒绝（closing/kill_due），不发布新尝试")
        return attempt, AttemptBackend(
            BACKEND_DIRECT_CHILD, None, console_reachable=console_present(), aborted=True
        )
    if not _closeout_old_attempt(attempt, closeout_grace):
        logger.warning(
            "heavy worker 旧尝试清理超时（attempt_id=%s），停止 breakaway 重试，"
            "当前尝试按 direct-child 继续",
            attempt_id,
        )
        return attempt, AttemptBackend(BACKEND_DIRECT_CHILD, None, console_reachable=console_present())

    attempt_id += 1  # 换胎即新尝试：attempt_id 递增
    attempt = spawn_attempt(
        attempt_id, command, env=env, cwd=cwd,
        extra_creationflags=CREATE_BREAKAWAY_FROM_JOB,
    )
    job = ProcessTreeJob.create()
    if job.assign(int(attempt.proc._handle)):
        return attempt, AttemptBackend(
            BACKEND_BREAKAWAY_JOB, job, console_reachable=console_present()
        )
    err = job.last_assign_winerror
    job.close_once()
    logger.warning(
        "heavy worker breakaway 重试仍失败（winerror=%s），降级 direct-child"
        "（backend=direct-child，attempt_id=%s）",
        err, attempt_id,
    )
    return attempt, AttemptBackend(BACKEND_DIRECT_CHILD, None, console_reachable=console_present())


def wrap_existing_attempt(proc, attempt_id: int = 0):
    """包装外部创建的进程为 (attempt, AttemptBackend)（测试/迁移辅助）。

    仅 POSIX 路径使用（外部进程须已 start_new_session）。
    """
    attempt = _ExternalAttempt(proc, attempt_id)
    if _WINDOWS:
        return attempt, AttemptBackend(BACKEND_DIRECT_CHILD, None, console_reachable=False)
    return attempt, AttemptBackend(BACKEND_POSIX_PGROUP, None, console_reachable=True)


class _ExternalAttempt:
    """外部进程的最小尝试适配（无结果通道/stdin 所有权）。"""

    def __init__(self, proc, attempt_id):
        self.proc = proc
        self.attempt_id = attempt_id
        self.stdin = None
        self.result = None


def _close_attempt_parent_handles(attempt) -> None:
    """关闭父侧 stdin 写端与结果通道读端（幂等；缺失属性容忍）。

    ``SpawnedAttempt`` 的 stdin 藏在 ``_stdin``、公开面是 ``proc.stdin``，而
    ``_ExternalAttempt`` 两者都可能为 None —— 一律按 getattr 取，取不到就跳过。
    """
    stdin = getattr(attempt, "stdin", None)
    if stdin is None:
        stdin = getattr(getattr(attempt, "proc", None), "stdin", None)
    if stdin is not None and not getattr(stdin, "closed", True):
        try:
            stdin.close()
        except OSError:
            pass
    result = getattr(attempt, "result", None)
    if result is not None:
        result.close()


def _closeout_old_attempt(attempt, grace: float) -> bool:
    """旧尝试收口：terminate → 有界确认退出（实际退出证据）→ 仍未退出则
    kill → 确认 → 关父侧句柄。返回是否取得退出证据。"""
    proc = attempt.proc
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    return False  # 无法确认退出：调用方停止重试
        return proc.poll() is not None
    finally:
        # W12 / P3-HEAVY-1：无论是否取得退出证据，父侧句柄都必须关闭。此前只
        # terminate + wait，Windows 上每次换胎泄漏 2 个父侧句柄（stdin 写端 +
        # 结果管道读端），且读端不关会让结果读取器线程长挂。旧尝试在本函数
        # 返回后即被丢弃，其父侧资源由这里唯一收口。
        _close_attempt_parent_handles(attempt)


def terminate_attempt(attempt, decision, *, grace: float = 5.0, cooperative_grace: float = 2.0):
    """终止一次尝试：协作级（按快照）→ terminate → kill → Job 关闭（幂等）。

    - Windows 协作级：快照 ``console_reachable`` 为真才投递 CTRL_BREAK（2s
      协作宽限，秒退即提前收口）；投递失败记 warning 降级直杀；
    - POSIX：两级 killpg（SIGTERM → SIGKILL，对出生 pgid）；
    - ``decision.job`` 非空时最后 ``close_once()``——整树兜底；关闭 Job
      **不等于**调用已结算（C1 §4.3，记账在注册表层）。
    """
    proc = attempt.proc
    if _WINDOWS and proc.poll() is None and decision.console_reachable:
        delivered, err = generate_console_break(proc.pid)
        if delivered:
            try:
                proc.wait(timeout=cooperative_grace)
            except subprocess.TimeoutExpired:
                pass
        else:
            logger.warning(
                "heavy worker CTRL_BREAK 投递失败（pid=%s, winerror=%s），降级直杀",
                proc.pid, err,
            )

    if not _WINDOWS and proc.poll() is None:
        pgid = _plat.birth_pgid(proc)
        if not _plat.terminate_group_two_tier(pgid, grace, leader=proc):
            logger.warning("heavy worker posix 组清理未确认（pgid=%s）", pgid)

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            pass

    _close_attempt_parent_handles(attempt)

    if decision.job is not None:
        decision.job.close_once()
    return proc.returncode
