
"""OS-level exit deadline acceptance tests for W4-7."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from queue import Empty, Queue
from threading import Thread

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEADLINE = 25.0

_CHILD = r"""
import asyncio
import logging
import os
import sys
import threading
import time

from app.mcp.protocol import shutdown as sh

_MODE = sys.argv[1]

def marker(value):
    sys.stdout.write(value + "\n")
    sys.stdout.flush()

def wait_for(predicate, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False

if _MODE == "heavy_eof":
    from app.mcp.protocol import heavy_process

    def run_heavy():
        try:
            heavy_process.run_heavy_tool_blocking(
                "app.mcp.protocol._heavy_selftest",
                "slow_hang",
                {"sleep": 60},
                60.0,
            )
        except BaseException:
            pass

    worker = threading.Thread(target=run_heavy, name="w47-heavy", daemon=False)
    worker.start()
    if not wait_for(lambda: bool(heavy_process._live_attempts.attempts)):
        marker("HEAVY_NOT_STARTED")
        raise SystemExit(3)
    marker("HEAVY_STARTED")
    sys.stdin = sh.wrap_stdin_with_eof_awareness(
        sys.stdin, lambda: sh.record_exit_intent("stdio_eof")
    )
    if sys.stdin.buffer.read(1):
        raise SystemExit(4)
    from app.mcp_server import cleanup_resources
    cleanup_resources()
    worker.join(timeout=15.0)
    if worker.is_alive():
        marker("CLEANUP_BLOCKED")
        time.sleep(60)
    marker("CLEANUP_DONE")
    raise SystemExit(0)

if _MODE in ("light_pool", "default_pool"):
    from app import mcp_server
    started = threading.Event()

    def block():
        started.set()
        time.sleep(60)

    if _MODE == "light_pool":
        mcp_server._get_tool_executor().submit(block)
    else:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_in_executor(None, block)
    if not started.wait(5.0):
        marker("POOL_NOT_STARTED")
        raise SystemExit(3)
    marker("POOL_STARTED")
    sup = sh.ensure_exit_supervisor()
    sup.record("os_test_pool")
    mcp_server.cleanup_resources()
    time.sleep(60)

if _MODE == "stderr_full":
    started = threading.Event()

    def fill_stderr():
        started.set()
        chunk = b"x" * 65536
        while True:
            sys.stderr.buffer.write(chunk)
            sys.stderr.buffer.flush()

    threading.Thread(target=fill_stderr, name="w47-stderr", daemon=True).start()
    if not started.wait(5.0):
        raise SystemExit(3)
    marker("STDERR_WRITER_STARTED")
    sh.ensure_exit_supervisor().record("os_test_stderr")
    time.sleep(60)

if _MODE == "log_lock":
    started = threading.Event()

    def hold_logging_lock():
        logging._acquireLock()
        started.set()
        time.sleep(60)

    threading.Thread(target=hold_logging_lock, name="w47-log-lock", daemon=True).start()
    if not started.wait(5.0):
        raise SystemExit(3)
    marker("LOG_LOCK_HELD")
    sh.ensure_exit_supervisor().record("os_test_log_lock")
    time.sleep(60)

if _MODE == "loop_paused":
    sh.ensure_exit_supervisor().record("os_test_loop")
    marker("LOOP_BLOCKING")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def block_loop():
        while True:
            time.sleep(0.1)

    loop.run_until_complete(block_loop())

if _MODE == "stdin_backpressure":
    sh.ensure_exit_supervisor()
    sys.stdin = sh.wrap_stdin_with_eof_awareness(
        sys.stdin, lambda: sh.record_exit_intent("stdio_eof")
    )
    marker("STDIN_WAITING")
    sys.stdin.buffer.read(1)
    time.sleep(60)

if _MODE == "repeated_notice":
    sup = sh.ensure_exit_supervisor()
    sup.record("first")
    marker("FIRST_NOTICE")
    for index in range(4):
        time.sleep(1.0)
        sup.record("repeat_" + str(index))
    time.sleep(60)

if _MODE == "startup_block":
    sh.ensure_exit_supervisor().record("startup_failure")
    marker("STARTUP_CLEANUP_BLOCKING")
    time.sleep(60)

if _MODE == "startup_natural":
    marker("STARTUP_NATURAL_FAILURE")
    raise RuntimeError("injected startup failure")

if _MODE == "supervisor_failure":
    def fail_create(cls):
        raise RuntimeError("injected supervisor creation failure")
    sh.ExitSupervisor.create = classmethod(fail_create)
    try:
        sh.ensure_exit_supervisor()
    except RuntimeError as exc:
        marker("SUPERVISOR_FAILURE_OBSERVED")
        raise SystemExit(1) from exc
    raise SystemExit(4)

if _MODE == "two_lifespans":
    os.environ["LUJO_MCP_STDIO_MODE"] = "1"
    from fastapi import FastAPI
    from app.main import lifespan

    async def two_rounds():
        async with lifespan(FastAPI()):
            marker("ROUND_ONE")
        async with lifespan(FastAPI()):
            marker("ROUND_TWO")
            await asyncio.sleep(26.0)
        marker("ROUND_DONE")

    asyncio.run(two_rounds())
"""

def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "STORAGE_BACKEND": "memory",
            "KB_PERSIST_ENABLED": "false",
            "API_KEY": "",
            "HOST": "127.0.0.1",
            "LUJO_MCP_STDIO_MODE": "1",
            "OTEL_SDK_DISABLED": "true",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def _readline(stream, timeout: float = 15.0) -> tuple[str | None, Exception | None]:
    result: Queue = Queue()

    def read():
        try:
            result.put(("ok", stream.readline()))
        except Exception as exc:
            result.put(("error", exc))

    Thread(target=read, daemon=True).start()
    try:
        kind, value = result.get(timeout=timeout)
    except Empty:
        return None, TimeoutError(f"child marker timeout after {timeout}s")
    if kind == "error":
        return None, value
    return value.decode("utf-8", errors="replace").rstrip("\r\n"), None


def _run_child(
    mode: str,
    *,
    close_stdin: bool = False,
    timeout: float = 32.0,
    expected_marker: str | None = None,
) -> tuple[int | None, float, str, str]:
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, mode],
        cwd=str(_PROJECT_ROOT),
        env=_base_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    started = time.monotonic()
    marker_value = None
    marker_error = None
    try:
        marker_value, marker_error = _readline(proc.stdout)
        if expected_marker is not None:
            assert marker_error is None, marker_error
            assert marker_value == expected_marker, marker_value
        if close_stdin:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5.0)
        pytest.fail(f"child {mode} did not exit within {timeout}s; marker={marker_value!r}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)
    elapsed = time.monotonic() - started
    stdout_tail = proc.stdout.read().decode("utf-8", errors="replace")
    stdout = (
        ((marker_value + "\n") if marker_value is not None else "")
        + stdout_tail
    )
    stderr = proc.stderr.read().decode("utf-8", errors="replace")
    return proc.returncode, elapsed, stdout, stderr


def _assert_deadline_exit(code: int | None, elapsed: float, mode: str) -> None:
    assert code == 0, f"{mode} exit={code}, elapsed={elapsed:.2f}s"
    assert elapsed <= _DEADLINE + 4.0, f"{mode} exceeded exit deadline: {elapsed:.2f}s"


def test_stdio_eof_with_inflight_heavy_exits_cleanly():
    code, elapsed, stdout, stderr = _run_child(
        "heavy_eof",
        close_stdin=True,
        timeout=15.0,
        expected_marker="HEAVY_STARTED",
    )
    _assert_deadline_exit(code, elapsed, "heavy_eof")
    assert "CLEANUP_DONE" in stdout
    assert "Traceback" not in stderr


@pytest.mark.parametrize("mode", ["light_pool", "default_pool"])
def test_watchdog_reclaims_each_blocking_pool(mode):
    code, elapsed, _stdout, _stderr = _run_child(
        mode,
        timeout=_DEADLINE + 4.0,
        expected_marker="POOL_STARTED",
    )
    _assert_deadline_exit(code, elapsed, mode)


@pytest.mark.parametrize("mode", ["stderr_full", "log_lock"])
def test_watchdog_is_independent_of_io_and_logging(mode):
    code, elapsed, _stdout, _stderr = _run_child(
        mode,
        timeout=_DEADLINE + 4.0,
        expected_marker={
            "stderr_full": "STDERR_WRITER_STARTED",
            "log_lock": "LOG_LOCK_HELD",
        }[mode],
    )
    _assert_deadline_exit(code, elapsed, mode)


def test_watchdog_runs_while_main_event_loop_is_blocked():
    code, elapsed, _stdout, _stderr = _run_child(
        "loop_paused",
        timeout=_DEADLINE + 4.0,
        expected_marker="LOOP_BLOCKING",
    )
    _assert_deadline_exit(code, elapsed, "loop_paused")


def test_stdin_eof_does_not_wait_for_downstream_consumers():
    code, elapsed, _stdout, _stderr = _run_child(
        "stdin_backpressure",
        close_stdin=True,
        timeout=_DEADLINE + 4.0,
        expected_marker="STDIN_WAITING",
    )
    _assert_deadline_exit(code, elapsed, "stdin_backpressure")


def test_repeated_exit_notifications_do_not_refresh_deadline():
    code, elapsed, _stdout, _stderr = _run_child(
        "repeated_notice",
        timeout=_DEADLINE + 4.0,
        expected_marker="FIRST_NOTICE",
    )
    _assert_deadline_exit(code, elapsed, "repeated_notice")
    assert elapsed >= _DEADLINE - 2.0, f"deadline was refreshed: {elapsed:.2f}s"


def test_startup_failure_with_blocked_cleanup_uses_last_resort_exit():
    code, elapsed, _stdout, _stderr = _run_child(
        "startup_block",
        timeout=_DEADLINE + 4.0,
        expected_marker="STARTUP_CLEANUP_BLOCKING",
    )
    _assert_deadline_exit(code, elapsed, "startup_block")


def test_startup_failure_returns_nonzero_before_deadline():
    code, elapsed, stdout, stderr = _run_child(
        "startup_natural",
        timeout=10.0,
        expected_marker="STARTUP_NATURAL_FAILURE",
    )
    assert code not in (None, 0)
    assert elapsed < 10.0
    assert "injected startup failure" in stderr


def test_supervisor_creation_failure_fails_before_accepting_work():
    code, elapsed, stdout, stderr = _run_child(
        "supervisor_failure",
        timeout=10.0,
        expected_marker="SUPERVISOR_FAILURE_OBSERVED",
    )
    assert code not in (None, 0)
    assert elapsed < 10.0
    assert "SUPERVISOR_FAILURE_OBSERVED" in stdout


def test_two_lifespans_do_not_arm_process_exit_supervisor():
    code, elapsed, stdout, stderr = _run_child(
        "two_lifespans",
        timeout=32.0,
        expected_marker="ROUND_ONE",
    )
    assert code == 0, f"two lifespans exit={code}\nstderr={stderr}"
    assert elapsed >= 25.0
    assert "ROUND_TWO" in stdout
    assert "ROUND_DONE" in stdout
    assert "Traceback" not in stderr
