"""W3-3（B15）Job 降级链 + 逐尝试能力快照验收。

对应 DESIGN_C3 §2.1（①Assign 成功 → job；②ACCESS_DENIED → BREAKAWAY 重试
一次 → breakaway+job；③仍失败 → direct-child + warning）与 CHECKLIST W3-3
约束：换胎即新尝试（attempt_id 递增，旧尝试实际退出证据 + 资源收尾完才发布
新尝试）；清理超时停止重试（不得用 superseded 冒充退出）；kill_due 跨尝试
不清零（经 gate 复查承载）；go 已提交的尝试不允许重试执行业务（链路仅在
go 前运行）；协作级可用性按条目能力快照判定（R14），不按宿主推断。

注入方式：monkeypatch 替换 spawn/Job 工厂（单元层确定性）；真子进程用例
验证 Windows 实链路。POSIX 路径经 WSL 实测（Windows skip 带环境理由）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from app.mcp.protocol.termination import backend as backend_mod

_WINDOWS = sys.platform == "win32"


class _FakeProc:
    """可控生命周期假进程：脚本化 terminate/wait/poll 行为。"""

    def __init__(self, *, dies_on_terminate: bool = True, pid: int = 40000):
        self.pid = pid
        self.returncode = None
        self.dies_on_terminate = dies_on_terminate
        self.terminate_calls = 0
        self.kill_calls = 0
        self._handle = 0x1000 + pid  # 假句柄值（fake job 只记录不校验）

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        if self.dies_on_terminate:
            self.returncode = 1

    def kill(self):
        self.kill_calls += 1
        self.returncode = 1

    def wait(self, timeout=None):
        if self.returncode is None and timeout is not None:
            time.sleep(timeout)
        return self.returncode


class _FakeAttempt:
    def __init__(self, proc, attempt_id):
        self.proc = proc
        self.attempt_id = attempt_id
        self.stdin = None


class _FakeJob:
    """脚本化 Job：assign 按预定序列返回 (成功?, winerror)。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.assigned_handles = []
        self.close_calls = 0
        self.closed = False
        self.last_assign_winerror = None

    def assign(self, proc_handle):
        ok, err = self.outcomes.pop(0)
        self.assigned_handles.append(proc_handle)
        self.last_assign_winerror = err
        return ok

    def is_process_in_job(self, proc_handle):
        return bool(self.assigned_handles)

    def close_once(self):
        self.close_calls += 1
        self.closed = True


@pytest.fixture()
def backend_env(monkeypatch):
    """替换平台 Job 工厂与 spawn：返回记录器（fake 注入，确定性）。"""
    calls = {"spawns": [], "jobs": []}

    class _Recorder:
        pass

    rec = _Recorder()
    rec.calls = calls
    rec.spawn_script = []  # 每次 spawn 弹出一个 _FakeProc 工厂

    def _fake_spawn(attempt_id, command, *, env=None, cwd=None,
                    extra_creationflags=0, start_new_session=False):
        proc = rec.spawn_script.pop(0)()
        proc.last_extra_creationflags = extra_creationflags
        calls["spawns"].append((attempt_id, extra_creationflags))
        return _FakeAttempt(proc, attempt_id)

    monkeypatch.setattr(backend_mod, "spawn_attempt", _fake_spawn)

    def _job_factory(outcomes):
        job = _FakeJob(outcomes)

        class _FakeJobClass:
            @staticmethod
            def create():
                calls["jobs"].append(job)
                return job

        return _FakeJobClass

    rec.job_factory = _job_factory
    return rec


# ── ① Assign 成功 → job ──────────────────────────────────────────────────


@pytest.mark.skipif(not _WINDOWS, reason="Job 语义 Windows 专属（POSIX skip：环境理由）")
def test_assign_success_returns_job_backend(backend_env, monkeypatch):
    backend_env.spawn_script.append(lambda: _FakeProc())
    monkeypatch.setattr(
        backend_mod, "ProcessTreeJob", backend_env.job_factory([(True, None)])
    )
    attempt, decision = backend_mod.spawn_with_backend(0, ["cmd"])
    try:
        assert decision.backend == "job"
        assert decision.job is not None
        assert decision.aborted is False
        assert len(backend_env.calls["spawns"]) == 1  # 无换胎
    finally:
        backend_mod.terminate_attempt(attempt, decision, grace=1.0)
    assert decision.job.close_calls == 1


# ── ② ACCESS_DENIED → breakaway 重试一次（换胎即新尝试） ──────────────────


@pytest.mark.skipif(not _WINDOWS, reason="Job 语义 Windows 专属（POSIX skip：环境理由）")
def test_access_denied_triggers_breakaway_retry_with_new_attempt(backend_env, monkeypatch):
    backend_env.spawn_script.append(lambda: _FakeProc())  # 旧尝试：可确认退出
    backend_env.spawn_script.append(lambda: _FakeProc())  # 新尝试（breakaway）
    monkeypatch.setattr(
        backend_mod, "ProcessTreeJob",
        backend_env.job_factory([(False, 5), (True, None)]),
    )
    attempt, decision = backend_mod.spawn_with_backend(0, ["cmd"])
    try:
        assert decision.backend == "breakaway+job"  # ② 生效后端：breakaway+job
        assert attempt.attempt_id == 1  # 换胎即新尝试：attempt_id 递增
        spawns = backend_env.calls["spawns"]
        assert [s[0] for s in spawns] == [0, 1]
        assert spawns[1][1] & 0x01000000  # 新尝试带 CREATE_BREAKAWAY_FROM_JOB
    finally:
        backend_mod.terminate_attempt(attempt, decision, grace=1.0)


@pytest.mark.skipif(not _WINDOWS, reason="Job 语义 Windows 专属（POSIX skip：环境理由）")
def test_double_denial_falls_back_to_direct_child_with_warning(backend_env, monkeypatch, caplog):
    backend_env.spawn_script.append(lambda: _FakeProc())
    backend_env.spawn_script.append(lambda: _FakeProc())
    monkeypatch.setattr(
        backend_mod, "ProcessTreeJob",
        backend_env.job_factory([(False, 5), (False, 5)]),
    )
    import logging

    with caplog.at_level(logging.WARNING, logger="lujo-mcp.termination"):
        attempt, decision = backend_mod.spawn_with_backend(0, ["cmd"])
    try:
        assert decision.backend == "direct-child"
        assert decision.job is None
        assert any("direct-child" in r.getMessage() for r in caplog.records)
    finally:
        backend_mod.terminate_attempt(attempt, decision, grace=1.0)


@pytest.mark.skipif(not _WINDOWS, reason="Job 语义 Windows 专属（POSIX skip：环境理由）")
def test_cleanup_timeout_stops_retry_without_superseded_faking(backend_env, monkeypatch):
    """旧尝试清理超时（无法确认退出）→ **停止重试**：不发布新尝试，当前
    尝试按 direct-child 继续（不得用 superseded 标记冒充退出）。"""
    hanging = _FakeProc(dies_on_terminate=False)  # terminate/wait 无法确认退出
    backend_env.spawn_script.append(lambda: hanging)
    monkeypatch.setattr(
        backend_mod, "ProcessTreeJob",
        backend_env.job_factory([(False, 5)]),
    )
    attempt, decision = backend_mod.spawn_with_backend(0, ["cmd"], closeout_grace=0.2)
    try:
        assert len(backend_env.calls["spawns"]) == 1  # 未换胎
        assert attempt.attempt_id == 0
        assert decision.backend == "direct-child"
        assert decision.job is None
    finally:
        hanging.kill()  # 兜底回收假进程场景
        backend_mod.terminate_attempt(attempt, decision, grace=0.5)


@pytest.mark.skipif(not _WINDOWS, reason="Job 语义 Windows 专属（POSIX skip：环境理由）")
def test_gate_refusal_stops_retry_and_marks_aborted(backend_env, monkeypatch):
    """gate（closing/kill_due 复查）拒绝 → 不换胎、aborted 标记——调用方
    不得对该尝试写 go（kill_due 跨尝试语义由 gate 承载）。"""
    backend_env.spawn_script.append(lambda: _FakeProc())
    monkeypatch.setattr(
        backend_mod, "ProcessTreeJob",
        backend_env.job_factory([(False, 5)]),
    )
    attempt, decision = backend_mod.spawn_with_backend(0, ["cmd"], gate=lambda: False)
    try:
        assert len(backend_env.calls["spawns"]) == 1
        assert decision.aborted is True
    finally:
        backend_mod.terminate_attempt(attempt, decision, grace=1.0)


@pytest.mark.skipif(not _WINDOWS, reason="Job 语义 Windows 专属（POSIX skip：环境理由）")
def test_go_committed_attempt_never_retries_business():
    """go 已提交的尝试不允许重试执行业务：链路仅在 go 前（spawn/assign 段）
    运行——结构性保证，锁定 API 形态（本模块不接触 handshake/go）。"""
    import inspect

    source = inspect.getsource(backend_mod)
    assert "handshake(" not in source, "降级链不得接触握手/业务执行段"


# ── 协作级按条目能力快照判定（R14） ──────────────────────────────────────


@pytest.mark.skipif(not _WINDOWS, reason="协作级 Windows 专属（POSIX skip：环境理由）")
def test_cooperative_tier_follows_capability_snapshot(backend_env, monkeypatch):
    """协作级是否投递 CTRL_BREAK 由**条目能力快照**（console_reachable）决定，
    不按宿主实时控制台推断。"""
    break_calls = []
    monkeypatch.setattr(
        backend_mod, "generate_console_break",
        lambda pid: break_calls.append(pid) or (True, None),
    )

    proc = _FakeProc()
    attempt = _FakeAttempt(proc, 0)
    # 快照 console_reachable=False（如 CREATE_NO_WINDOW 子进程）→ 不投递
    decision = backend_mod.AttemptBackend(
        backend="direct-child", job=None, console_reachable=False
    )
    backend_mod.terminate_attempt(attempt, decision, grace=0.5, cooperative_grace=0.1)
    assert break_calls == []

    # 快照 True → 投递 CTRL_BREAK
    proc2 = _FakeProc(dies_on_terminate=False)
    attempt2 = _FakeAttempt(proc2, 0)
    decision2 = backend_mod.AttemptBackend(
        backend="direct-child", job=None, console_reachable=True
    )
    backend_mod.terminate_attempt(attempt2, decision2, grace=0.5, cooperative_grace=0.1)
    assert break_calls == [proc2.pid]


# ── 真子进程整树（Windows 实链路：降级链 + Job + 三级终止） ────────────────


@pytest.mark.skipif(not _WINDOWS, reason="Job 语义 Windows 专属（POSIX skip：环境理由）")
def test_real_child_job_backend_tree_reaped_on_terminate(tmp_path):
    """真子进程 + 真降级链：backend=job；子进程内拉孙进程；
    terminate_attempt 后**含孙进程整树回收**。"""
    import app.mcp.protocol.heavy_spawn as hs
    from app.mcp.protocol.termination import _win32

    start_file = os.path.join(str(tmp_path), "start.flag")
    report = os.path.join(str(tmp_path), "gc_pid.txt")
    child_code = (
        "import os, subprocess, sys, time\n"
        "start_file, report_file = sys.argv[1], sys.argv[2]\n"
        "while not os.path.exists(start_file):\n"
        "    time.sleep(0.02)\n"
        "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        "                      stdin=subprocess.DEVNULL)\n"
        "open(report_file, 'w').write(str(gc.pid))\n"
        "time.sleep(60)\n"
    )
    attempt, decision = backend_mod.spawn_with_backend(0, [
        sys.executable, "-c", child_code, start_file, report,
    ])
    try:
        assert decision.backend in ("job", "breakaway+job", "direct-child")
        with open(start_file, "w", encoding="utf-8") as fh:
            fh.write("go")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not os.path.exists(report):
            time.sleep(0.05)
        with open(report, encoding="utf-8") as fh:
            gc_pid = int(fh.read().strip())

        backend_mod.terminate_attempt(attempt, decision, grace=5.0)
        assert attempt.proc.poll() is not None
        assert not _win32.process_exists(gc_pid), "孙进程未随终止回收"
    finally:
        backend_mod.terminate_attempt(attempt, decision, grace=5.0)  # 幂等收口
        if os.path.exists(report):
            with open(report, encoding="utf-8") as fh:
                subprocess.run(["taskkill", "/F", "/PID", fh.read().strip()],
                               capture_output=True, timeout=10)


# ── POSIX 路径（WSL 实测口径） ───────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 路径用例（Windows skip：环境理由；WSL 实测）")
def test_posix_backend_is_pgroup_and_two_tier(tmp_path):
    from app.mcp.protocol.termination import _posix

    report = tmp_path / "gc_pid.txt"
    child_code = (
        "import subprocess, sys, time\n"
        "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        "                      stdin=subprocess.DEVNULL)\n"
        "open(sys.argv[1], 'w').write(str(gc.pid))\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", child_code, str(report)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True, cwd="/tmp",
    )
    attempt, decision = backend_mod.wrap_existing_attempt(child)
    try:
        assert decision.backend == "posix-pgroup"
        assert decision.job is None
        assert decision.console_reachable is True
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not report.exists():
            time.sleep(0.05)
        with open(report, encoding="utf-8") as fh:
            gc_pid = int(fh.read().strip())
        backend_mod.terminate_attempt(attempt, decision, grace=2.0)
        assert child.poll() is not None
        assert not _posix.process_exists(gc_pid), "POSIX 组内孙进程未回收"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
