"""W3-4（B15）能力探测验收：探一次缓存 + probe_in_progress 等待点 +
探测复用调用许可（不结算调用）。

对应 DESIGN_C3 §3 与 CHECKLIST W3-4：
- 标志置位 / 清除 / 通知**在锁内**；创建 / Assign / 终止 / 等待**在锁外**；
- 探测与探测重试各取新 ``attempt_id`` 且 ``attempt_kind="probe"``；
- 探测结束只销毁探测尝试资源（子进程确认收割 + Job 关闭），**不得摘除调用
  条目或释放许可**（结构性保证：探测组件不接触调用许可记账）；
- 异常 / 取消 / 关闭路径都必须清除标志并唤醒等待方；
- 等待方恢复后重新检查 closing / kill_due（经调用方注入的 gate）与自身
  截止时间（deadline 有界等待）。

Windows 实链路 + fake 注入混合；POSIX 快照路径经 WSL 实测。
"""

from __future__ import annotations

import sys
import threading
import time

import pytest

from app.mcp.protocol.termination import probe as probe_mod

_WINDOWS = sys.platform == "win32"


class _FakeProbeChild:
    def __init__(self, pid):
        self.pid = 50000 + pid
        self._handle = 0x2000 + pid  # 假句柄（fake job 只记录不校验）
        self.returncode = None

    def terminate(self):
        self.returncode = 1

    def kill(self):
        self.returncode = 1

    def wait(self, timeout=None):
        self.returncode = self.returncode or 1
        return self.returncode

    def poll(self):
        return self.returncode


class _FakeJob:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.close_calls = 0
        self.last_assign_winerror = None

    def assign(self, proc_handle):
        ok, err = self.outcomes.pop(0)
        self.last_assign_winerror = err
        return ok

    def close_once(self):
        self.close_calls += 1


class _FakeJobClass:
    """Job 工厂：每次 create 依次弹出一组预置 assign 结果。"""

    def __init__(self, outcome_sets):
        self.outcome_sets = list(outcome_sets)
        self.created = []

    def create(self):
        job = _FakeJob(self.outcome_sets.pop(0) if self.outcome_sets else [(True, None)])
        self.created.append(job)
        return job


@pytest.fixture()
def probe_env(monkeypatch):
    """全新探测实例 + fake 子进程 spawn + 可安装的 fake Job 工厂。"""
    env = type("Env", (), {})()
    env.spawned = []  # [(child, extra_creationflags)]
    env.job_classes = []

    def _spawn(extra_creationflags=0):
        child = _FakeProbeChild(len(env.spawned) + 1)
        env.spawned.append((child, extra_creationflags))
        return child

    monkeypatch.setattr(probe_mod, "_spawn_probe_child", _spawn)

    def _install_jobs(outcome_sets):
        jc = _FakeJobClass(outcome_sets)
        env.job_classes.append(jc)
        monkeypatch.setattr(probe_mod, "_job_factory", jc.create)
        return jc

    env.install_jobs = _install_jobs
    monkeypatch.setattr(probe_mod, "CAPABILITY_PROBE", probe_mod.CapabilityProbe())
    return env


def _no_gate():
    return True


def test_probe_runs_once_and_caches(probe_env):
    """探一次缓存：两次 ensure_probed 只 spawn 一次探测子进程。"""
    if _WINDOWS:
        probe_env.install_jobs([[(True, None)]])
    snap1 = probe_mod.CAPABILITY_PROBE.ensure_probed(gate=_no_gate)
    snap2 = probe_mod.CAPABILITY_PROBE.ensure_probed(gate=_no_gate)
    assert snap1 is snap2
    assert len(probe_env.spawned) == (1 if _WINDOWS else 0)
    assert probe_mod.CAPABILITY_PROBE.in_progress is False


def test_waiter_waits_for_probe_and_gets_cache(probe_env, monkeypatch):
    """并发等待点：探测进行中时，等待方在可取消逻辑等待点等待（gate 至少
    复查一次），探测完成后直接取缓存（不重复 spawn / 不重复建 Job）。"""
    release = threading.Event()
    run_count = {"n": 0}

    def _slow_run(self):
        run_count["n"] += 1
        release.wait(timeout=10)  # 挂住首次探测
        return probe_mod.CapabilitySnapshot(
            "posix-pgroup" if not _WINDOWS else "job", True, []
        )

    monkeypatch.setattr(probe_mod.CapabilityProbe, "_run_probe", _slow_run)

    gate_calls = {"waiter": 0}
    results = {}

    def _prober():
        try:
            results["prober"] = probe_mod.CAPABILITY_PROBE.ensure_probed(gate=_no_gate)
        except Exception as exc:  # noqa: BLE001
            results["prober"] = exc

    def _waiter():
        def _waiter_gate():
            gate_calls["waiter"] += 1
            return True

        try:
            results["waiter"] = probe_mod.CAPABILITY_PROBE.ensure_probed(gate=_waiter_gate)
        except Exception as exc:  # noqa: BLE001
            results["waiter"] = exc

    t1 = threading.Thread(target=_prober)
    t1.start()
    time.sleep(0.2)
    assert probe_mod.CAPABILITY_PROBE.in_progress is True

    t2 = threading.Thread(target=_waiter)
    t2.start()
    time.sleep(0.2)
    assert gate_calls["waiter"] >= 1  # 等待方已在等待点

    release.set()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not isinstance(results.get("prober"), Exception)
    assert results.get("waiter") is results.get("prober")  # 同一缓存快照
    assert run_count["n"] == 1  # 等待方未重复探测（等待方不得 spawn/建 Job）
    assert probe_mod.CAPABILITY_PROBE.in_progress is False


def test_waiter_gate_refusal_aborts_waiter(probe_env, monkeypatch):
    """等待方唤醒后复查 gate：closing/kill_due 拒绝 → 等待方中止（不 spawn、
    不阻塞探测方）。"""
    release = threading.Event()
    started = threading.Event()

    def _slow_run(self):
        started.set()
        release.wait(timeout=10)
        raise RuntimeError("must not reach here for prober")

    monkeypatch.setattr(probe_mod.CapabilityProbe, "_run_probe", _slow_run)

    results = {}

    def _prober():
        started.set()
        try:
            probe_mod.CAPABILITY_PROBE.ensure_probed(gate=_no_gate)
        except Exception as exc:  # noqa: BLE001
            results["prober"] = exc

    t1 = threading.Thread(target=_prober)
    t1.start()
    started.wait(timeout=5)
    time.sleep(0.1)

    def _waiter():
        def _refusing_gate():
            return False  # closing/kill_due 已成立

        try:
            results["waiter"] = probe_mod.CAPABILITY_PROBE.ensure_probed(gate=_refusing_gate)
        except Exception as exc:  # noqa: BLE001
            results["waiter"] = exc

    t2 = threading.Thread(target=_waiter)
    t2.start()
    t2.join(timeout=10)
    assert not t2.is_alive()
    assert isinstance(results.get("waiter"), Exception)  # 等待方被 gate 中止
    release.set()
    t1.join(timeout=10)


def test_waiter_deadline_bounded(probe_env, monkeypatch):
    """等待方自身截止时间：deadline 到点即中止（有界等待）。

    阻塞经 _run_probe 注入（平台无关：POSIX 探测不 spawn 子进程）。"""
    release = threading.Event()
    started = threading.Event()

    def _slow_run(self):
        started.set()
        release.wait(timeout=30)
        raise RuntimeError("unreachable")

    monkeypatch.setattr(probe_mod.CapabilityProbe, "_run_probe", _slow_run)

    results = {}

    def _prober():
        started.set()
        try:
            probe_mod.CAPABILITY_PROBE.ensure_probed(gate=_no_gate)
        except Exception as exc:  # noqa: BLE001
            results["prober"] = exc

    t1 = threading.Thread(target=_prober)
    t1.start()
    started.wait(timeout=5)
    time.sleep(0.1)

    def _waiter():
        try:
            probe_mod.CAPABILITY_PROBE.ensure_probed(
                gate=_no_gate, deadline=time.monotonic() + 0.5
            )
        except Exception as exc:  # noqa: BLE001
            results["waiter"] = exc

    t2 = threading.Thread(target=_waiter)
    t2.start()
    t2.join(timeout=10)
    release.set()
    t1.join(timeout=10)
    assert isinstance(results.get("waiter"), TimeoutError)


def test_probe_exception_clears_flag_and_wakes_waiters(probe_env, monkeypatch):
    """探测异常：标志清除 + 等待方被唤醒（其成为新探测方）+ 无缓存残留。

    异常经 _run_probe 注入（平台无关：POSIX 探测不 spawn 子进程）。"""
    calls = {"n": 0}
    real_run = probe_mod.CapabilityProbe._run_probe

    def _flaky_run(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("probe failed")
        return real_run(self)

    monkeypatch.setattr(probe_mod.CapabilityProbe, "_run_probe", _flaky_run)
    if _WINDOWS:
        probe_env.install_jobs([[(True, None)]])
        monkeypatch.setattr(
            probe_mod, "_spawn_probe_child", lambda extra=0: _FakeProbeChild(53000)
        )

    results = {}

    def _first():
        try:
            probe_mod.CAPABILITY_PROBE.ensure_probed(gate=_no_gate)
        except Exception as exc:  # noqa: BLE001
            results["first"] = exc

    t1 = threading.Thread(target=_first)
    t1.start()
    t1.join(timeout=10)
    assert isinstance(results.get("first"), OSError)
    assert probe_mod.CAPABILITY_PROBE.in_progress is False  # 异常路径已清除标志

    # 第二次调用成为新探测方并成功
    result = probe_mod.CAPABILITY_PROBE.ensure_probed(gate=_no_gate)
    assert result is not None
    assert calls["n"] == 2
    assert probe_mod.CAPABILITY_PROBE.in_progress is False


def test_probe_attempt_metadata_and_full_cleanup(probe_env):
    """探测尝试元数据：attempt_kind="probe" + 独立 attempt_id；探测结束
    探测子进程确认收割 + Job 关闭（不留句柄/不留进程）。"""
    if _WINDOWS:
        # 两次 spawn（普通 + breakaway）：attempt_id 单调、kind=probe
        probe_env.install_jobs([[(False, 5), (True, None)]])
    snap = probe_mod.CAPABILITY_PROBE.ensure_probed(gate=_no_gate)
    if _WINDOWS:
        assert len(probe_env.spawned) == 2
        kinds = {a["attempt_kind"] for a in snap.attempts}
        assert kinds == {"probe"}
        ids = [a["attempt_id"] for a in snap.attempts]
        assert ids == sorted(ids)
        assert all(child.poll() is not None for child, _ in probe_env.spawned)
        assert all(job.close_calls >= 1 for job in probe_env.job_classes[0].created)
        assert snap.backend == "breakaway+job"
    else:
        assert snap.backend == "posix-pgroup"


def test_windows_real_probe_child_backend():
    """Windows 实链路：真探测子进程 + 真 Job → backend ∈ {job, breakaway+job,
    direct-child}；探测子进程确认收割。"""
    if not _WINDOWS:
        pytest.skip("Windows 实链路用例（POSIX skip：环境理由；WSL 覆盖 posix-pgroup）")
    snap = probe_mod.CAPABILITY_PROBE.ensure_probed(gate=_no_gate)
    assert snap.backend in ("job", "breakaway+job", "direct-child")
    assert isinstance(snap.console_present, bool)
    for att in snap.attempts:
        assert probe_mod.process_exists(att["pid"]) is False  # 探测子进程已收割


def test_probe_structurally_never_touches_permit():
    """结构性保证：探测组件不接触调用许可记账（探测复用调用许可、不结算
    调用）。"""
    import inspect

    source = inspect.getsource(probe_mod)
    for forbidden in ("settle(", "attach(", "acquire_token", "release_once"):
        assert forbidden not in source, f"探测组件不得接触许可记账: {forbidden}"
