"""W2-5 结果通道与 stdout 纯净性验收（U04 单元层 + R09 四断言 + 收容边界）。

覆盖 DESIGN_C2 §8.1：
- R09 四断言：handler 内 ``os.write(1, …)`` 噪声注入下 ① fd1 噪声不破坏结果
  反序列化 ② 握手（ready/go）可用 ③ 业务结果正确返回 ④ 父 stdout 无污染；
- 孙进程收容边界：worker 拉起孙进程后退出 → 孙进程**没有**继承结果写端，
  父读取器能结束（不得依赖杀孙进程才读到 EOF）；
- worker 不读请求 / 大请求半写 / 结果半帧停住 → 截止仍能决定超时且回收正确。

冻结侧（源码父 + 冻结子 R09 smoke）见 ``tests/integration/test_frozen_child_smoke.py``
（产物缺失时按环境理由 skip）。U04 真实 stdio 集成见
``tests/integration/test_process_boundary.py::TestU04RealStdioHeavyChain``。
"""

from __future__ import annotations

import os
import pickle
import signal
import subprocess
import sys
import time

import pytest

import app.mcp.protocol.heavy_spawn as hs

FAKE = "tests.fake_heavy_workers"


def _worker_cmd(mode: str) -> list[str]:
    return [sys.executable, "-m", FAKE, mode]


def _deadline(seconds: float = 10.0) -> float:
    return time.monotonic() + seconds


def _pickled(arguments) -> bytes:
    return pickle.dumps(arguments, protocol=pickle.HIGHEST_PROTOCOL)


def test_r09_four_assertions_with_fd1_noise(capfd):
    """R09 四断言：fd1 噪声注入下握手可用、结果正确、反序列化不破坏、
    父 stdout 无污染。"""
    env = dict(os.environ, FAKE_FD1_NOISE="<<<garbage-to-fd1>>>")
    attempt = hs.spawn_attempt(1, _worker_cmd("echo"), env=env)
    try:
        payload = hs.handshake(attempt, _pickled({"k": "v"}), deadline=_deadline(),
                               allow_commit=lambda: True)
        # ② 握手（ready/go）可用：ready 事件已置位、结果帧完整到达
        assert attempt.result.ready.is_set()
        assert attempt.result.result_bytes is not None
        # ① fd1 噪声不破坏结果反序列化 + ③ 业务结果正确返回
        status, value = pickle.loads(payload)
        assert status == "ok" and value == {"k": "v"}
        assert b"garbage" not in payload
    finally:
        exitcode = hs.terminate_and_reap(attempt, grace=5.0)
    assert exitcode == 0
    # ④ 父 stdout 无污染（子进程 stdout 出生即 DEVNULL）
    captured = capfd.readouterr()
    assert "garbage" not in captured.out
    assert "garbage" not in captured.err


def test_grandchild_does_not_inherit_result_write_end():
    """孙进程收容边界：worker 拉起孙进程后正常退出——父读取器立即拿到完整
    结果并确认 worker 退出，**无需杀孙进程**（若孙进程继承了结果写端，
    EOF/结果收取会被孙进程的存活拖延）。"""
    attempt = hs.spawn_attempt(1, _worker_cmd("grandchild_then_exit"))
    gc_pid = None
    try:
        t0 = time.monotonic()
        payload = hs.handshake(attempt, _pickled({}), deadline=_deadline(),
                               allow_commit=lambda: True)
        status, value = pickle.loads(payload)
        assert status == "ok"
        gc_pid = value["gc_pid"]
        # 结果已完整到达；worker 退出确认不受孙进程拖累（结果收取与 EOF
        # 不依赖孙进程死亡——若写端被孙进程继承，这里会被拖到超时）
        assert attempt.proc.wait(timeout=5.0) == 0
        elapsed = time.monotonic() - t0
        assert elapsed < 8.0
    finally:
        hs.terminate_and_reap(attempt, grace=5.0)
    assert attempt.result.closed  # 读端已收口（读取器线程真实结束已记录）
    assert attempt.result.reader_alive is False
    # 测试清理：仅回收本测试制造的孙进程（此刻孙进程应仍存活——它从未持有
    # 结果写端，进程存活与否不影响父侧收口）
    if gc_pid is not None:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(gc_pid)],
                           capture_output=True, timeout=10)
        else:
            try:
                os.kill(gc_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_big_request_to_non_reading_worker_respects_deadline():
    """大请求 + worker 不读 stdin：请求写入线程被管道背压阻塞，但独立控制
    路径的截止判定不受阻（§3.2）——截止后按超时归类并回收子进程。"""
    attempt = hs.spawn_attempt(1, _worker_cmd("hang_no_ready"))
    t0 = time.monotonic()
    try:
        with pytest.raises(hs.HeavyHandshakeTimeout):
            hs.handshake(attempt, b"\x00" * (8 * 1024 * 1024),
                         deadline=time.monotonic() + 1.5,
                         allow_commit=lambda: True)
        assert time.monotonic() - t0 < 9.0  # 未被 8MB 管道背压卡死
    finally:
        hs.terminate_and_reap(attempt, grace=5.0)
    assert attempt.proc.poll() is not None  # 已回收


def test_result_half_frame_hang_respects_deadline():
    """结果半帧停住（R + 半个帧头后挂住）：截止仍能决定超时，回收正确。"""
    attempt = hs.spawn_attempt(1, _worker_cmd("half_result"))
    t0 = time.monotonic()
    try:
        with pytest.raises(hs.HeavyHandshakeTimeout):
            hs.handshake(attempt, _pickled({}), deadline=time.monotonic() + 1.5,
                         allow_commit=lambda: True)
        assert time.monotonic() - t0 < 9.0
    finally:
        hs.terminate_and_reap(attempt, grace=5.0)
    assert attempt.proc.poll() is not None


def test_repeated_close_does_not_leak_or_crash():
    """重复关闭（§8.1「无重复关闭」）：ResultChannel/回收幂等，二次关闭安全。"""
    attempt = hs.spawn_attempt(1, _worker_cmd("crash_before_ready"))
    attempt.result.start_reader()
    hs.terminate_and_reap(attempt, grace=5.0)
    first_closed = attempt.result.closed
    attempt.result.close()  # 二次关闭必须安全
    hs.terminate_and_reap(attempt, grace=5.0)  # 二次回收必须安全
    assert first_closed and attempt.result.closed
    assert attempt.proc.poll() is not None
