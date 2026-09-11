"""能力探测（DESIGN_C3 §3；B15 / W3-4）：探一次、缓存；探测复用调用许可。

职责与边界（逐条对齐 CHECKLIST W3-4）：

- **标志置位 / 清除 / 通知在锁内**（``_cond``）；**创建 / Assign / 终止 /
  等待在锁外**（``_run_probe`` 不持锁）；
- 探测与探测重试各取新 ``attempt_id`` 且 ``attempt_kind="probe"``；
- **探测结束只销毁探测尝试资源**（探测子进程确认收割 + Job 关闭），**不得
  摘除调用条目或释放许可**——结构性保证：本模块不接触调用许可记账（许可从不被本模块结算或
  挂载，测试锁定）;
- **异常 / 取消 / 关闭路径都必须清除标志并唤醒等待方**（``ensure_probed``
  的 except BaseException 分支）；
- **等待方恢复后重新检查** closing / kill_due（经调用方注入的 ``gate``）与
  自身截止时间（``deadline`` 有界等待）；
- 探测缓存只作预判（启动期一次性 + stderr/stdout 安全的结构化通告）——每次
  业务尝试仍以实际 Assign 结果为准（backend.py 逐次记账，R14）。

Windows 探测：创建极短命探测子进程（最小 ``-c``，握手前即终止）→
Assign → 成功为 job；ACCESS_DENIED → 以 BREAKAWAY 再探一次 →
breakaway+job；否则 direct-child。POSIX：常量级探测（``start_new_session``
/ ``killpg`` 可用），无子进程。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time

if sys.platform == "win32":
    from app.mcp.protocol.termination._win32 import (
        CREATE_BREAKAWAY_FROM_JOB,
        ProcessTreeJob,
        console_present,
        is_access_denied,
        process_exists,
    )

    _WINDOWS = True
else:
    from app.mcp.protocol.termination import _posix as _plat

    _WINDOWS = False

logger = logging.getLogger("lujo-mcp.termination")

# 探测子进程收割宽限（秒）；探测进程必须走完整创建→终止→确认收割流程（C3 §3）
_PROBE_CHILD_GRACE = 2.0
# 等待点轮询步长（秒）
_WAIT_POLL = 0.05


class ProbeAborted(Exception):
    """等待方被 gate 拒绝（closing / kill_due 已成立）——调用方放弃本次。"""


class CapabilitySnapshot:
    """探测结果快照（只读；供结构化通告与矩阵归档）。"""

    def __init__(self, backend: str, console_present: bool, attempts: list[dict]):
        self.backend = backend
        self.console_present = console_present
        self.attempts = attempts  # [{"attempt_id", "attempt_kind": "probe", "pid"}]
        self.probed_at = time.time()


def _job_factory():
    """Windows：真实 Job 工厂（测试经 monkeypatch 替换）。"""
    return ProcessTreeJob()


def _spawn_probe_child(extra_creationflags: int = 0):
    """创建极短命探测子进程（最小 ``-c``，握手前即终止）。"""
    return subprocess.Popen(  # noqa: S603 —— 固定命令行
        [sys.executable, "-c", "pass"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=(0x00000200 | extra_creationflags) if _WINDOWS else 0,
        start_new_session=not _WINDOWS,
    )


def _reap_probe_child(child) -> None:
    """探测尝试资源销毁：terminate → 确认收割（kill 兜底），不留进程。"""
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=_PROBE_CHILD_GRACE)
        except subprocess.TimeoutExpired:
            child.kill()
            try:
                child.wait(timeout=_PROBE_CHILD_GRACE)
            except subprocess.TimeoutExpired:
                pass


class CapabilityProbe:
    """进程级能力探测（探一次缓存；``CAPABILITY_PROBE`` 为进程实例）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._in_progress = False
        self._snapshot: CapabilitySnapshot | None = None
        self._next_attempt_id = 0

    @property
    def in_progress(self) -> bool:
        with self._cond:
            return self._in_progress

    @property
    def snapshot(self) -> CapabilitySnapshot | None:
        with self._cond:
            return self._snapshot

    def _next_attempt_meta(self) -> dict:
        meta = {"attempt_id": self._next_attempt_id, "attempt_kind": "probe", "pid": None}
        self._next_attempt_id += 1
        return meta

    def ensure_probed(self, *, gate=None, deadline: float | None = None) -> CapabilitySnapshot:
        """懒触发探测并缓存；并发等待方在**可取消逻辑等待点**等待。

        - 等待拍内复查 ``gate``（closing / kill_due——kill_due 跨尝试不清零）
          与 ``deadline``（自身截止时间）；拒绝/超时即中止等待方（不 spawn）；
        - 等待方唤醒后若探测已成功直接取缓存；若探测失败则**成为新探测方**；
        - 本方法不接触调用许可记账（探测复用调用许可、不结算调用）。
        """
        with self._cond:
            if self._snapshot is not None:
                return self._snapshot
            if self._in_progress:
                while self._in_progress:
                    if gate is not None and not gate():
                        raise ProbeAborted(
                            "capability probe wait aborted by gate (closing/kill_due)"
                        )
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("capability probe wait deadline exceeded")
                    self._cond.wait(_WAIT_POLL)
                if self._snapshot is not None:
                    return self._snapshot
                # 前一探测失败：本等待方成为新探测方（落到下方继续）
            self._in_progress = True  # 置位在锁内

        try:
            snapshot = self._run_probe()  # 创建/Assign/终止/等待均在锁外
        except BaseException:
            # 异常 / 取消路径：清除标志并唤醒等待方
            with self._cond:
                self._in_progress = False
                self._cond.notify_all()
            raise
        with self._cond:
            self._snapshot = snapshot
            self._in_progress = False  # 清除在锁内
            self._cond.notify_all()  # 唤醒等待方
        logger.info(
            "heavy termination capability probe: backend=%s console_present=%s "
            "attempts=%s platform=%s",
            snapshot.backend, snapshot.console_present, snapshot.attempts,
            sys.platform,
        )
        return snapshot

    def _run_probe(self) -> CapabilitySnapshot:
        if not _WINDOWS:
            # POSIX：常量级探测（start_new_session / killpg 可用性）；无子进程
            assert os_killpg_available() and start_new_session_available()
            return CapabilitySnapshot(
                backend="posix-pgroup", console_present=True, attempts=[]
            )

        attempts: list[dict] = []
        console = console_present()

        # ① 普通 spawn + Assign
        meta = self._next_attempt_meta()
        child = _spawn_probe_child(0)
        meta["pid"] = child.pid
        attempts.append(meta)
        job = _job_factory()
        if job.assign(int(child._handle)):
            _reap_probe_child(child)
            job.close_once()
            return CapabilitySnapshot("job", console, attempts)
        err = job.last_assign_winerror
        job.close_once()
        _reap_probe_child(child)

        if is_access_denied(err):
            # ② BREAKAWAY 再探一次
            meta2 = self._next_attempt_meta()
            child2 = _spawn_probe_child(CREATE_BREAKAWAY_FROM_JOB)
            meta2["pid"] = child2.pid
            attempts.append(meta2)
            job2 = _job_factory()
            if job2.assign(int(child2._handle)):
                _reap_probe_child(child2)
                job2.close_once()
                return CapabilitySnapshot("breakaway+job", console, attempts)
            job2.close_once()
            _reap_probe_child(child2)

        return CapabilitySnapshot("direct-child", console, attempts)


def os_killpg_available() -> bool:
    return hasattr(os, "killpg")


def start_new_session_available() -> bool:
    return "start_new_session" in __import__("inspect").signature(
        subprocess.Popen.__init__
    ).parameters


# 进程级探测实例（每 Lujo 进程探一次；测试经 monkeypatch 替换为全新实例）
CAPABILITY_PROBE = CapabilityProbe()


def process_exists(pid: int) -> bool:
    """存在性转发（测试断言探测子进程已收割）。"""
    if _WINDOWS:
        from app.mcp.protocol.termination._win32 import (
            process_exists as _win32_process_exists,
        )

        return _win32_process_exists(pid)
    return _plat.process_exists(pid)


