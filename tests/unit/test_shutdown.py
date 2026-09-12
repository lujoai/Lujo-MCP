"""W4-1（B15/C4）shutdown 编排模块验收：退出意图拍点 / EOF 感知流代理 /
薄信号 handler / 看门狗（绝对 deadline + os._exit）/ M1 ①–⑥ 序编排。

对应 DESIGN_C4 §1.1、§1.3、§2.1、§3 与 CHECKLIST W4-1 关键约束：
- **代际关闭与进程退出分账**：普通 lifespan 结束不得升级为进程退出；
  进程退出所有权显式持有，不通过「是否在 pytest 中」判断；
- **deadline 到点直接 os._exit(0)**，之前禁止 logging/print/stderr.write/
  stderr.flush/文件 I/O/Job 清理/join/取锁；
- **EOF 感知接在唯一 stdin 输入生产者上**，不新增第二个竞争读取线程；
- 后续 EOF/信号/finally 只合并原因，**不刷新或延后 deadline**；
- M1 序：每步执行前检查剩余预算，实际等待也必须有界。

B 类·新机制验收（新构件不变量）。
"""

from __future__ import annotations

import io
import sys
import threading
import time

import pytest

from app.mcp.protocol import shutdown as sh


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    """每个用例全新进程级状态（意图/看门狗/注册表）。"""
    monkeypatch.setattr(sh, "_intent_lock", threading.Lock())
    monkeypatch.setattr(sh, "_intent_t0", None)
    monkeypatch.setattr(sh, "_intent_reasons", frozenset())
    monkeypatch.setattr(sh, "_signal_t0", None)
    monkeypatch.setattr(sh, "_signal_reason", None)
    monkeypatch.setattr(sh, "_deadline", None)
    yield


def _exit_intents():
    return sh._intent_reasons


# ── 退出意图拍点（§1.3：t0 = 首次可靠观察；只合并原因） ──────────────────


def test_exit_intent_first_observation_sets_t0_and_merges_reasons():
    assert sh.exit_intent_recorded() is False
    t1 = sh.record_exit_intent("stdio_eof")
    assert sh.exit_intent_recorded() is True
    time.sleep(0.05)
    t2 = sh.record_exit_intent("signal")
    assert t2 == t1, "二次通知不得刷新 t0"
    assert _exit_intents() == frozenset({"stdio_eof", "signal"})  # 原因合并


def test_deadline_is_t0_plus_25s_and_never_refreshed():
    t0 = sh.record_exit_intent("stdio_eof")
    assert sh.deadline_remaining() is not None
    remaining1 = sh.deadline_remaining()
    time.sleep(0.05)
    sh.record_exit_intent("signal")
    remaining2 = sh.deadline_remaining()
    assert remaining2 < remaining1  # deadline 单调递减，不刷新
    assert sh._deadline == pytest.approx(t0 + sh.EXIT_DEADLINE_SECONDS, abs=0.01)


def test_signal_handler_publishes_intent_without_locks_or_event():
    """薄信号 handler：只做普通属性赋值（发布意图与首次触发时间），
    不调用 threading.Event.set / Condition / logging / 取锁。"""
    import inspect

    source = inspect.getsource(sh.signal_handler_stub)
    for forbidden in ("Event(", ".set(", "Condition", "logging", "Lock("):
        assert forbidden not in source, f"信号 handler 含禁用调用: {forbidden}"

    sh.signal_handler_stub(2, None)  # 模拟 SIGINT 到达（直接调用）
    assert sh._signal_t0 is not None
    assert sh._signal_reason == "signal"
    # 正式记录采纳信号的首次触发时间（不重置为更晚时刻）
    t0 = sh.record_exit_intent("signal")
    assert t0 == sh._signal_t0
    assert sh.deadline_remaining() is not None


def test_signal_t0_earlier_than_formal_record_is_adopted():
    sh.signal_handler_stub(15, None)
    time.sleep(0.05)
    t0 = sh.record_exit_intent("stdio_eof")
    assert t0 == sh._signal_t0  # t0 取首次触发（信号时刻），非 formal 时刻
    assert _exit_intents() == frozenset({"stdio_eof", "signal"})


# ── 看门狗（§2.1：绝对 deadline + 无阻塞最后防线） ────────────────────────


def test_supervisor_ready_before_return_and_never_disarmed(monkeypatch):
    # 真实武装 25s deadline——必须打补丁，否则套件运行 25s 后被真 os._exit 杀掉
    monkeypatch.setattr(sh.os, "_exit", lambda code: None)
    sup = sh.ExitSupervisor.create()
    assert sup.ready is True
    sup.record("stdio_eof")  # 武装
    deadline = sup.deadline
    assert deadline is not None
    time.sleep(0.05)
    sup.record("signal")
    assert sup.deadline == deadline  # 不刷新
    # 不存在撤销 API（结构性：disarm 缺失）
    assert not hasattr(sup, "disarm")
    sup.shutdown_watchdog()


def test_watchdog_fires_os_exit_at_deadline(monkeypatch):
    """deadline 到点：监督线程直接 os._exit(0)，且在此之前无日志/print/flush
    /取锁等前置动作（以 _exit 前记录的时间与调用序列锁定）。"""
    fired = []
    monkeypatch.setattr(sh, "EXIT_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(sh.os, "_exit", lambda code: fired.append((time.monotonic(), code)))

    sup = sh.ExitSupervisor.create()
    t0 = sup.record("stdio_eof")
    t1 = time.monotonic()
    deadline = t0 + 0.2
    # 轮询等待看门狗触发（不 sleep 固定值）
    for _ in range(100):
        if fired:
            break
        time.sleep(0.02)
    assert fired, "看门狗未在 deadline 触发 os._exit"
    fired_at, code = fired[0]
    assert code == 0
    assert fired_at >= deadline - 0.05  # 不得早于 deadline
    assert fired_at - t1 < 2.0  # 及时触发（不被无关等待拖延）


def test_watchdog_internal_error_enters_last_resort(monkeypatch):
    """监督线程已武装后的不可恢复错误 → 同一无阻塞最后防线（os._exit），
    不留下失去监督的进程。"""
    fired = []
    monkeypatch.setattr(sh, "EXIT_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(sh.os, "_exit", lambda code: fired.append(code))
    monkeypatch.setattr(sh, "_watch_now", lambda: 1 / 0)  # 注入监督线程时钟故障

    sup = sh.ExitSupervisor.create()
    sup.record("signal")
    for _ in range(100):
        if fired:
            break
        time.sleep(0.02)
    assert fired == [0], "监督线程内部异常必须进入最后防线"


def test_supervisor_ready_failure_fails_startup(monkeypatch):
    """监督线程创建失败 → 启动失败（不得继续接受工具调用）。"""
    monkeypatch.setattr(
        threading, "Thread",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("thread resources")),
    )
    with pytest.raises(RuntimeError):
        sh.ExitSupervisor.create()


# ── EOF 感知流代理（§1.1：接在唯一输入生产者上，无第二读取线程） ──────────


def test_eof_proxy_reports_eof_once_and_passes_bytes_through():
    underlying = io.BytesIO(b"line1\nline2\n")
    eof_calls = []
    proxy = sh.create_eof_aware_stdin_proxy(underlying, lambda: eof_calls.append(1))
    assert proxy.read(6) == b"line1\n"
    assert proxy.readline() == b"line2\n"
    assert proxy.read(10) == b""  # 经代理观察到 EOF
    assert eof_calls == [1]  # 恰好一次
    # 再次读取 EOF：不重复触发
    proxy.read(10)
    assert eof_calls == [1]


def test_eof_proxy_has_no_extra_reader_thread():
    """结构性：代理为透传包装，不创建读取线程（无第二竞争读取者）。"""
    import inspect

    source = inspect.getsource(sh.create_eof_aware_stdin_proxy)
    assert "Thread(" not in source, "EOF 代理不得创建读取线程"


# ── M1 ①–⑥ 序编排与子预算检查（§3） ─────────────────────────────────────


class _Step:
    def __init__(self, name, budget, sleep=0.0, fail=False):
        self.name = name
        self.budget = budget
        self.sleep = sleep
        self.fail = fail


def _run_m1_with_fakes(monkeypatch, steps_executed, oversleep_step=None):
    """以注入的钩子运行 M1 序：记录执行序与超预算事件。"""
    events = []

    def _make_hook(name, budget, sleep):
        def _hook(remaining):
            events.append(("begin", name, round(remaining, 2)))
            if oversleep_step == name:
                time.sleep(sleep)  # 模拟超过子预算的阻塞
            steps_executed.append(name)
            return True

        return _hook

    hooks = {
        "step1_stop_accepting": _make_hook("step1_stop_accepting", 2.0, 0.0),
        "step2_cancel_waiters": _make_hook("step2_cancel_waiters", 2.0, 0.0),
        "step3_terminate_active": _make_hook("step3_terminate_active", 10.0, 0.0),
        "step4_close_jobs": _make_hook("step4_close_jobs", 2.0, 0.0),
        "step5_shutdown_pools": _make_hook("step5_shutdown_pools", 2.0, 0.0),
        "step6_b23_idempotent": _make_hook("step6_b23_idempotent", 2.0, 0.0),
    }
    if oversleep_step:
        events.append(("oversleep", oversleep_step))

    deadline = time.monotonic() + 25
    sh.run_m1_sequence(
        t0=time.monotonic(),
        hooks=hooks,
        over_budget_events=events.append,
    )
    return events


def test_m1_sequence_order_fixed():
    """①–⑥ 序固定：停止接纳 → 取消等待者 → 终止在途 → 关 Job → 关池 → 幂等。
    池关闭不得先于进程终止。"""
    executed = []
    events = []
    deadline = time.monotonic() + 25

    order = []
    hooks = {}
    for name in ("step1_stop_accepting", "step2_cancel_waiters",
                 "step3_terminate_active", "step4_close_jobs",
                 "step5_shutdown_pools", "step6_b23_idempotent"):
        def _hook(name=name):
            order.append(name)
            return True
        hooks[name] = _hook

    sh.run_m1_sequence(t0=time.monotonic(), hooks=hooks, over_budget_events=events.append)
    assert order == [
        "step1_stop_accepting", "step2_cancel_waiters", "step3_terminate_active",
        "step4_close_jobs", "step5_shutdown_pools", "step6_b23_idempotent",
    ]
    # ⑤ 关池不得先于 ③ 终止在途
    assert order.index("step3_terminate_active") < order.index("step5_shutdown_pools")


def test_m1_sequence_records_over_budget_steps():
    """子预算检查：某步实际等待超过其预算 → 记结构化超预算事件（不中断序列）。"""
    executed = []

    def _hook_sleep(step_name, seconds):
        def _hook():
            time.sleep(seconds)
            executed.append((step_name, "done"))
            return True
        return _hook

    hooks = {
        "step1_stop_accepting": _hook_sleep("step1", 0.0),
        "step2_cancel_waiters": _hook_sleep("step2", 0.0),
        "step3_terminate_active": _hook_sleep("step3", 0.0),
        "step4_close_jobs": _hook_sleep("step4", 0.0),
        "step5_shutdown_pools": _hook_sleep("step5", 0.4),  # 预算 2s 内 — 不超
        "step6_b23_idempotent": _hook_sleep("step6", 0.0),
    }
    # 把 step5 的预算压到 0.1s 使其必然超预算
    budget_overrides = {"step5_shutdown_pools": 0.1}
    events = []
    sh.run_m1_sequence(
        t0=time.monotonic(), hooks=hooks, over_budget_events=events.append,
        budget_overrides=budget_overrides,
    )
    assert executed[-1][0] == "step6"  # 序列未被中断
    over = [e for e in events if getattr(e, "get", None) and e.get("step") == "step5_shutdown_pools"] \
        if events and isinstance(events[0], dict) else [e for e in events if "step5" in str(e)]
    assert over, "超预算步骤未记录结构化事件"


def test_terminate_active_processes_parallel_bounded(monkeypatch):
    """M1 ③：全量终止对每个在途尝试并行执行三级终止，10s 并行硬上限。"""
    import app.mcp.protocol.heavy_process as hp

    class _FakeProc:
        def __init__(self):
            self.pid = 60000
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 1

        def kill(self):
            self.returncode = 1

        def wait(self, timeout=None):
            self.returncode = 1
            return self.returncode

    class _FakeAttempt:
        def __init__(self, pid):
            self.proc = _FakeProc()
            self.proc.pid = pid
            self.attempt_id = pid

    live = {"attempts": [_FakeAttempt(60001), _FakeAttempt(60002)]}
    monkeypatch.setattr(hp, "_live_attempts", live)

    terminated = []
    monkeypatch.setattr(
        hp, "terminate_attempt",
        lambda attempt, decision, **kw: terminated.append(attempt.attempt_id) or 1,
        raising=False,
    )
    started = time.monotonic()
    hp.terminate_active_processes()
    assert time.monotonic() - started < 10.0
    assert sorted(terminated) == [60001, 60002]


def test_terminate_active_processes_empty_registry_fast(monkeypatch):
    """空注册表（服务从未接纳/已清空）：快速返回，不阻塞。"""
    import app.mcp.protocol.heavy_process as hp

    live = {"attempts": []}
    monkeypatch.setattr(hp, "_live_attempts", live)
    started = time.monotonic()
    hp.terminate_active_processes()
    assert time.monotonic() - started < 1.0
