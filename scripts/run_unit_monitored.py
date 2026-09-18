"""全量 unit 监控启动器（Windows 开发环境，纯 stdlib）。

动机：直接后台裸跑 ``pytest tests/unit`` 一旦被卡死或被子进程拖住，外层等待器
只能盲等、且无法区分 完成/失败/中断（Windows 下 bash ``kill -0`` 对外部 PID
也不可靠）。本启动器提供：

- ``UNIT_START=`` 时间戳 + 实际命令；
- 每 ``--heartbeat`` 秒（默认 45）打印 ``[monitor] elapsed_seconds=... last=...``
  心跳，附 pytest 最后一行进度；
- 退出时**无论成功/失败/中断/异常**必打四行哨兵：
    UNIT_STATUS=completed|failed|interrupted|unknown
    UNIT_EXIT=<pytest 实际退出码>
    UNIT_ELAPSED_SECONDS=<整数秒>
    UNIT_END=<ISO 时间戳>
  随后重放最后 30 行输出，便于直接判断状态；
- Ctrl+C：pytest 子进程放在独立进程组中（不会被控制台广播误伤/漏收），由本
  启动器捕获 SIGINT 后用 ``GenerateConsoleCtrlEvent(CTRL_C_EVENT)`` 精确转发，
  等子进程自行收尾（最多 ``--grace`` 秒）再升级为 terminate；
- 等待循环基于 ``Popen.poll()``，子进程退出即刻返回，不盲等。

局限（Windows）：``TerminateProcess``（taskkill /F、SIGTERM 等价物）不给目标
任何收尾机会，此时哨兵由**外层 shell 的 echo 缺失**体现为 unknown，属 OS 语义，
无法在子进程内补救。正常 Ctrl+C / SIGBREAK 路径均可得到 interrupted。

用法：
    .venv/Scripts/python.exe scripts/run_unit_monitored.py [pytest 参数...]
    # 默认补 tests/unit -q -p no:warnings
"""
from __future__ import annotations

import argparse
import collections
import ctypes
import os
import signal
import subprocess
import sys
import threading
import time

TAIL_LINES = 30


def _iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Monitor:
    def __init__(self, cmd: list[str], heartbeat: float, grace: float) -> None:
        self.cmd = cmd
        self.heartbeat = heartbeat
        self.grace = grace
        self.tail: collections.deque[str] = collections.deque(maxlen=TAIL_LINES)
        self.last_line = ""
        self.interrupt_requested = False
        self.forwarded = False
        self._escalation_started: float | None = None
        self._break_sent = False

    # ── 信号：Ctrl+C（SIGINT）与 Ctrl+Break（SIGBREAK）都按“用户要求中断”处理 ──
    def _on_signal(self, signum, _frame) -> None:
        self.interrupt_requested = True

    def _forward_ctrl_c(self, child: subprocess.Popen) -> None:
        """尝试把 Ctrl+C 投递给子进程组。控制台事件并非总能跨进程组生效，投递
        失败/无效由主循环的升级阶梯（CTRL_BREAK → terminate）兜底。"""
        if self.forwarded:
            return
        self.forwarded = True
        if os.name != "nt":
            child.send_signal(signal.SIGINT)
            self._escalation_started = time.monotonic()
            return
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleCtrlHandler(None, True)  # 屏蔽广播到本组
            kernel32.GenerateConsoleCtrlEvent(0, child.pid)  # CTRL_C_EVENT
        except Exception:
            pass
        self._escalation_started = time.monotonic()

    def _reader(self, child: subprocess.Popen) -> None:
        try:
            for line in child.stdout:  # type: ignore[union-attr]
                line = line.rstrip("\r\n")
                self.tail.append(line)
                self.last_line = line
                print(line, flush=True)
        except Exception:
            pass

    def run(self) -> int:
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        child = subprocess.Popen(
            self.cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        reader = threading.Thread(target=self._reader, args=(child,), daemon=True)
        reader.start()

        signal.signal(signal.SIGINT, self._on_signal)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, self._on_signal)

        t0 = time.monotonic()
        next_beat = t0 + self.heartbeat
        rc: int | None = None
        while True:
            rc = child.poll()
            if rc is not None:
                break
            now = time.monotonic()
            if now >= next_beat:
                next_beat = now + self.heartbeat
                print(
                    f"[monitor] elapsed_seconds={int(now - t0)} child_pid={child.pid} "
                    f"last={self.last_line[-90:]!r}",
                    flush=True,
                )
            if self.interrupt_requested:
                if not self.forwarded:
                    print("[monitor] interrupt requested -> forwarding Ctrl+C to pytest", flush=True)
                    self._forward_ctrl_c(child)
                else:
                    since = now - (self._escalation_started or now)
                    if since > 5 and not self._break_sent and os.name == "nt":
                        print("[monitor] Ctrl+C ineffective -> CTRL_BREAK", flush=True)
                        self._break_sent = True
                        try:
                            ctypes.windll.kernel32.GenerateConsoleCtrlEvent(1, child.pid)
                        except Exception:
                            pass
                    elif since > 10 and rc is None:
                        print("[monitor] signals ignored -> terminate", flush=True)
                        child.terminate()
                        self._escalation_started = now + 1e9  # 停止重复升级
                        # 子进程拒绝退出时最后兜底 kill
                        threading.Timer(10.0, child.kill, ()).start()
            time.sleep(0.5)

        reader.join(timeout=5)
        elapsed = int(time.monotonic() - t0)
        status = "interrupted" if self.interrupt_requested else self._classify(rc)
        print(f"UNIT_STATUS={status}")
        print(f"UNIT_EXIT={rc}")
        print(f"UNIT_ELAPSED_SECONDS={elapsed}")
        print(f"UNIT_END={_iso()}")
        print("[monitor] final tail (last lines of pytest output):")
        for line in list(self.tail)[-TAIL_LINES:]:
            print("  | " + line)
        return 0 if status == "completed" else 1

    @staticmethod
    def _classify(rc: int | None) -> str:
        if rc is None:
            return "unknown"
        # 0xC000013A = STATUS_CONTROL_C_EXIT；pytest 收 KeyboardInterrupt 常规退码 2
        if rc in (2, 130, 3221225786, -2):
            return "interrupted"
        if rc == 0:
            return "completed"
        return "failed"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--heartbeat", type=float, default=45.0)
    parser.add_argument("--grace", type=float, default=30.0)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    ns = parser.parse_args(argv[1:])
    pytest_args = ns.pytest_args or ["tests/unit", "-q", "-p", "no:warnings"]
    cmd = [sys.executable, "-u", "-m", "pytest", *pytest_args]
    print(f"UNIT_START={_iso()} cmd={' '.join(cmd)}", flush=True)
    monitor = Monitor(cmd, heartbeat=ns.heartbeat, grace=ns.grace)
    try:
        return monitor.run()
    except BaseException as e:  # 兜底：任何异常也要留哨兵
        print(f"monitor error: {e!r}", file=sys.stderr)
        print("UNIT_STATUS=unknown")
        print("UNIT_EXIT=-1")
        print("UNIT_ELAPSED_SECONDS=-1")
        print(f"UNIT_END={_iso()}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
