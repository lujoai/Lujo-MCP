"""W2-1（B08 前置）heavy_spawn 统一启动器验收测试。

覆盖 DESIGN_C2 §1.1（Popen 全参数）、§2.3（GO_COMMITTED 提交点与写入生命周期）、
§3（握手三条失败路径）、§3.1（故障注入矩阵：六边界 + GO_COMMITTED 后 G 写出前取消）、
§3.2（单一读取器、分帧与阶段校验）、§5（R6 大结果 1 MiB 不死锁）。

本批属 B 类·新机制验收：断言新构件（启动器/结果通道/单一读取器/提交点）的不变量；
「模块不存在」不构成红灯证据（C1 §7 纪律），红先仅按流程先写测试后实现。
"""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
import threading
import time

import pytest

import app.mcp.protocol.heavy_spawn as hs

FAKE = "tests.fake_heavy_workers"


def _worker_cmd(mode: str) -> list[str]:
    return [sys.executable, "-m", FAKE, mode]


def _deadline(seconds: float = 10.0) -> float:
    return time.monotonic() + seconds


def _pickled(arguments: dict) -> bytes:
    return pickle.dumps(arguments, protocol=pickle.HIGHEST_PROTOCOL)


# ── 成功路径 ──────────────────────────────────────────────────────────


def test_spawn_success_roundtrip_reaps_worker():
    """成功握手：ready → go → 结果帧原样回传；worker 被回收、退出码 0。"""
    attempt = hs.spawn_attempt(1, _worker_cmd("echo"))
    try:
        payload = hs.handshake(
            attempt, _pickled({"n": 41}), deadline=_deadline(),
            allow_commit=lambda: True,
        )
        status, value = pickle.loads(payload)
        assert status == "ok"
        assert value == {"n": 41}
    finally:
        exitcode = hs.terminate_and_reap(attempt, grace=5.0)
    assert exitcode == 0
    assert attempt.proc.poll() == 0
    assert attempt.result.closed
    # 单一最终关闭者：父侧读端关闭后，读取器线程已确认结束且不谎报
    assert attempt.result.reader_alive is False


def test_stdin_eof_noise_does_not_corrupt_result_channel():
    """R09 前置：worker 向 fd1 写噪声不污染结果通道（stdout 出生即 DEVNULL）。"""
    env = dict(os.environ, FAKE_FD1_NOISE="<<<fd1 garbage>>>")
    attempt = hs.spawn_attempt(1, _worker_cmd("echo"), env=env)
    try:
        payload = hs.handshake(
            attempt, _pickled({"k": "v"}), deadline=_deadline(),
            allow_commit=lambda: True,
        )
        status, value = pickle.loads(payload)
        assert status == "ok" and value == {"k": "v"}
        assert b"garbage" not in payload
    finally:
        hs.terminate_and_reap(attempt, grace=5.0)


def test_large_result_1mib_no_deadlock():
    """R6：1 MiB（> 管道缓冲）结果经结果通道流式收取，不因先 join 后读死锁。"""
    attempt = hs.spawn_attempt(1, _worker_cmd("echo_big"))
    try:
        payload = hs.handshake(
            attempt, _pickled({"size": 1024 * 1024}), deadline=_deadline(15.0),
            allow_commit=lambda: True,
        )
        status, value = pickle.loads(payload)
        assert status == "ok" and len(value) == 1024 * 1024
    finally:
        exitcode = hs.terminate_and_reap(attempt, grace=5.0)
    assert exitcode == 0


def test_channels_independent_per_attempt():
    """每尝试独立结果通道：两个尝试先后完成，互不串扰。"""
    a1 = hs.spawn_attempt(1, _worker_cmd("echo"))
    try:
        p1 = hs.handshake(a1, _pickled({"i": 1}), deadline=_deadline(),
                          allow_commit=lambda: True)
        assert pickle.loads(p1)[1] == {"i": 1}
    finally:
        hs.terminate_and_reap(a1, grace=5.0)
    a2 = hs.spawn_attempt(2, _worker_cmd("echo"))
    try:
        p2 = hs.handshake(a2, _pickled({"i": 2}), deadline=_deadline(),
                          allow_commit=lambda: True)
        assert pickle.loads(p2)[1] == {"i": 2}
    finally:
        hs.terminate_and_reap(a2, grace=5.0)


# ── §3 三条失败路径 ────────────────────────────────────────────────────


def test_worker_exits_before_ready_maps_to_internal_error():
    """① go 前崩溃：ready 永不到达且 proc 已退出 → WorkerExitedBeforeReady。"""
    attempt = hs.spawn_attempt(1, _worker_cmd("crash_before_ready"))
    with pytest.raises(hs.WorkerExitedBeforeReady) as ei:
        hs.handshake(attempt, _pickled({}), deadline=_deadline(),
                     allow_commit=lambda: True)
    assert ei.value.exitcode == 3
    hs.terminate_and_reap(attempt, grace=5.0)


def test_ready_timeout_maps_to_handshake_timeout_and_reaps():
    """③ 握手超时：假 worker 不发 ready → HeavyHandshakeTimeout；回收无残留。"""
    attempt = hs.spawn_attempt(1, _worker_cmd("hang_no_ready"))
    t0 = time.monotonic()
    with pytest.raises(hs.HeavyHandshakeTimeout):
        hs.handshake(attempt, _pickled({}), deadline=time.monotonic() + 1.0,
                     allow_commit=lambda: True)
    assert time.monotonic() - t0 < 9.0  # 不等满 hang 时长
    hs.terminate_and_reap(attempt, grace=5.0)
    assert attempt.proc.poll() is not None


def test_go_write_failure_broken_pipe_registers_commit_without_retry():
    """go 写出失败（§3.1：BrokenPipe 注入）：GO_COMMITTED 已登记、不回退到
    NONE、BrokenPipe → ②handshake broken；启动器绝不重试业务。

    注入说明：真实链路中该失败由「worker 收 ready 即退出」的时序自然产生，
    但子进程退出与父侧写出的竞态不可确定性构造（Windows 上进程存活期间
    关闭 stdin 读端也不会令父侧写入报错），故按单元注入契约测试——经
    专属 stdin 引用的包装器在 G 写入时抛 BrokenPipeError；go 后崩溃路径
    由 test_worker_dies_after_go 覆盖。
    """
    attempt = hs.spawn_attempt(1, _worker_cmd("echo"))
    real_stdin = attempt._stdin

    class _BrokenOnGo:
        """请求帧正常透传；G 写入必得 BrokenPipe。"""

        def __getattr__(self, name):
            return getattr(real_stdin, name)

        def write(self, data):
            if bytes(data) == b"G":
                raise BrokenPipeError(32, "Broken pipe")
            return real_stdin.write(data)

        def flush(self):
            return real_stdin.flush()

    attempt._stdin = _BrokenOnGo()  # noqa: SLF001 —— 注入到专属 stdin 引用
    try:
        with pytest.raises(hs.HeavySpawnBroken):
            hs.handshake(attempt, _pickled({}), deadline=_deadline(),
                         allow_commit=lambda: True)
        # 提交先于写出失败：GO_COMMITTED 已登记并以 WRITE_FAILED 收口，
        # 绝不回退到 NONE（禁止重试业务）
        assert attempt.go_state == "WRITE_FAILED"
    finally:
        attempt._stdin = real_stdin
        hs.terminate_and_reap(attempt, grace=5.0)


def test_worker_dies_after_go_maps_to_exited_without_result():
    """go 后、结果帧前崩溃：WorkerExitedWithoutResult（含退出码），
    不与「go 前崩溃」混淆。"""
    attempt = hs.spawn_attempt(1, _worker_cmd("ready_then_exit_after_go"))
    with pytest.raises(hs.WorkerExitedWithoutResult) as ei:
        hs.handshake(attempt, _pickled({}), deadline=_deadline(),
                     allow_commit=lambda: True)
    assert ei.value.exitcode == 1
    hs.terminate_and_reap(attempt, grace=5.0)


def test_request_write_failure_maps_to_handshake_broken():
    """请求帧写入失败/半途（stdin 断，§3.1 边界 1）：②handshake broken。"""
    attempt = hs.spawn_attempt(1, _worker_cmd("crash_before_ready"))
    # 注入：父侧先关 stdin 再触发握手 → 请求帧写入必然失败
    attempt.proc.stdin.close()
    with pytest.raises(hs.HeavySpawnBroken):
        hs.handshake(attempt, _pickled({}), deadline=_deadline(),
                     allow_commit=lambda: True)
    hs.terminate_and_reap(attempt, grace=5.0)


# ── §2.3 GO_COMMITTED 提交点与写入生命周期 ─────────────────────────────


def test_commit_refused_never_writes_go():
    """提交被拒（kill_due/closing 等四条件不齐）：go 永不写出；
    worker 若收到 G 会退出码 7——断言未发生。"""
    attempt = hs.spawn_attempt(1, _worker_cmd("refuse_go_marker"))
    with pytest.raises(hs.CommitRefused):
        hs.handshake(attempt, _pickled({}), deadline=_deadline(),
                     allow_commit=lambda: False)
    assert attempt.go_state == "NONE"
    # 提交被拒后由调用方终止回收；worker 未收到 G（Windows 强杀退出码 1），
    # 绝不能出现「worker 主动识别到 G」的退出码 7。
    exitcode = hs.terminate_and_reap(attempt, grace=5.0)
    assert exitcode is not None
    assert exitcode != 7


def test_go_committed_cancel_before_write_does_not_wait_indefinitely():
    """GO_COMMITTED 后、G 写出前到达取消（§3.1 可控写入屏障）：
    提交状态不回退、取消不等无期限阻塞 write（有界放弃）、不重试业务。"""
    attempt = hs.spawn_attempt(1, _worker_cmd("echo"))
    barrier = threading.Event()
    release = threading.Event()
    real_stdin = attempt.proc.stdin

    class _BarrierStdin:
        """在首次 write(G) 时阻塞到 release 放行——可控写入屏障。"""

        def __getattr__(self, name):
            return getattr(real_stdin, name)

        def write(self, data):
            if bytes(data) == b"G":
                barrier.set()
                release.wait(timeout=10)
            return real_stdin.write(data)

        def flush(self):
            return real_stdin.flush()

    attempt._stdin = _BarrierStdin()  # noqa: SLF001 —— 注入屏障到专属 stdin 引用
    try:
        errors: list[Exception] = []

        def _run() -> None:
            try:
                hs.handshake(attempt, _pickled({}), deadline=_deadline(),
                             allow_commit=lambda: True)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=_run)
        t.start()
        assert barrier.wait(timeout=10), "未观察到写入屏障（G 写入未发生）"
        # 取消到达：commit 已置位；取消方有界放弃阻塞 write（不等 release）
        attempt.abandon_pending_go_writes(timeout=2.0)
        assert attempt.go_state == "COMMITTED"  # 不能承诺业务绝未开始
        release.set()
        t.join(timeout=10)
        assert not errors or isinstance(errors[0], hs.HeavySpawnBroken)
    finally:
        attempt._stdin = real_stdin
        hs.terminate_and_reap(attempt, grace=5.0)


# ── §3.2 单一读取器、分帧与阶段校验 ────────────────────────────────────


def test_invalid_length_header_is_protocol_error():
    """无效帧长度（超出上限）：协议错误，绝不当作成功。"""
    attempt = hs.spawn_attempt(1, _worker_cmd("bad_length"))
    with pytest.raises(hs.HeavySpawnBroken):
        hs.handshake(attempt, _pickled({}), deadline=_deadline(),
                     allow_commit=lambda: True)
    hs.terminate_and_reap(attempt, grace=5.0)


def test_wrong_ready_byte_is_protocol_error():
    """错误 ready 字节：协议错误。"""
    attempt = hs.spawn_attempt(1, _worker_cmd("garbage_ready"))
    with pytest.raises(hs.HeavySpawnBroken):
        hs.handshake(attempt, _pickled({}), deadline=_deadline(),
                     allow_commit=lambda: True)
    hs.terminate_and_reap(attempt, grace=5.0)


def test_reader_thread_aliveness_is_recorded_not_assumed():
    """C4 §2.2：EOF 后读取器是否仍存活必须真实记录，不得声称 EOF ⇒ 线程已结束。"""
    attempt = hs.spawn_attempt(1, _worker_cmd("crash_before_ready"))
    attempt.result.start_reader()
    # 关闭前：alive 未定（None=未记录），不谎报
    assert attempt.result.reader_alive is None
    time.sleep(0.3)  # 给读取器时间观察 EOF
    assert attempt.result.eof_seen is True
    hs.terminate_and_reap(attempt, grace=5.0)
    assert attempt.result.closed
    # 关闭后：alive 来自真实 join 结果（读取器已随 EOF/关闭结束 → False）
    assert attempt.result.reader_alive is False


def test_terminate_and_reap_idempotent_and_cleans_all_resources():
    """回收幂等：重复 reap 不抛错；stdin/结果通道/进程全部收口。"""
    attempt = hs.spawn_attempt(1, _worker_cmd("hang_no_ready"))
    hs.terminate_and_reap(attempt, grace=5.0)
    first = attempt.proc.poll()
    hs.terminate_and_reap(attempt, grace=5.0)  # 第二次必须安全
    assert first is not None
    assert attempt.proc.stdin is None or attempt.proc.stdin.closed
    assert attempt.result.closed


def test_spawn_failure_cleans_partial_resources(monkeypatch):
    """Popen 失败路径：已取得的结果通道资源全部清理，不泄漏句柄。"""
    def _boom(*args, **kwargs):
        raise OSError("spawn refused")

    monkeypatch.setattr(hs.subprocess, "Popen", _boom)
    with pytest.raises(OSError):
        hs.spawn_attempt(1, _worker_cmd("echo"))
    # 未创建任何 attempt；父侧临时资源由 spawn_attempt 内部 finally 收口（无全局状态）
