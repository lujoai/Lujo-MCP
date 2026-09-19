"""全量 unit 监控启动器（Windows 开发环境，纯 stdlib）。

动机：直接后台裸跑 ``pytest tests/unit`` 一旦被卡死或被子进程拖住，外层等待器
只能盲等、且无法区分 完成/失败/中断（Windows 下 bash ``kill -0`` 对外部 PID
也不可靠）。本启动器提供：

- ``UNIT_START=`` 时间戳 + 实际命令；
- 每 ``--heartbeat`` 秒（默认 45）打印 ``[monitor] elapsed_seconds=... last=...``
  心跳，附 pytest 最后一行进度；
- 退出时**无论成功/失败/中断/异常**必打四行哨兵；哨兵是一次运行对外的唯一
  状态契约：UNIT_STATUS= 最多出现一次，且四行恒先于 final tail 重放：
    UNIT_STATUS=completed|failed|interrupted|unknown
    UNIT_EXIT=<pytest 实际退出码>
    UNIT_ELAPSED_SECONDS=<整数秒>
    UNIT_END=<ISO 时间戳>
  随后重放最后 30 行输出，便于直接判断状态；
- Ctrl+C：pytest 子进程放在独立进程组中（不会被控制台广播误伤/漏收），由本
  启动器捕获 SIGINT 后用 ``GenerateConsoleCtrlEvent(CTRL_C_EVENT)`` 精确转发；
  自转发起算，子进程在 ``--grace`` 推导的阈值内仍未退出则逐级升级——
  默认 ``--grace 30`` 对应 5s 发 CTRL_BREAK、10s terminate、20s kill；
- 等待循环基于 ``Popen.poll()``，子进程退出即刻返回，不盲等。

输出编码契约：本脚本启动时把自身 stdout/stderr reconfigure 为 UTF-8 +
errors="replace"（个别流不支持 reconfigure 时静默降级，由 ``_emit`` 的逐行
兜底写入接管）；子进程默认被设置 ``PYTHONIOENCODING=utf-8``（调用方已显式
设置该环境变量时不覆盖）。因此外层若在 GBK 控制台直接肉眼看中文可能显示为
乱码，建议 ``chcp 65001`` 或重定向到文件后阅读。

局限（Windows）：若【监控器自身】被 TerminateProcess（taskkill /F、SIGTERM 等
价物）强杀，则一套哨兵都发不出来，状态只能由外层等待器以哨兵缺失推断为
unknown，属 OS 语义，无法在进程内补救。子进程被升级阶梯的 terminate / kill
（默认 ``--grace 30`` 对应 10s / 20s）强杀时监控器自身不受影响，run() 会正常
收尾并发出 interrupted。main() 的异常兜底覆盖的是 run() 自身抛异常这一类，
与子进程被 terminate 无关。正常 Ctrl+C / SIGBREAK 路径均可得到 interrupted。

用法：
    .venv/Scripts/python.exe scripts/run_unit_monitored.py [pytest 参数...]
    # 默认补 tests/unit -p no:warnings
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

# 默认参数不要再加 -q：pytest.ini 的 addopts 已含 -q，叠加会变 -qq，
# pytest 在该安静级别会吞掉 "N passed in Xs" 统计行，final tail 随之失明。
DEFAULT_PYTEST_ARGS = ["tests/unit", "-p", "no:warnings"]


def _force_utf8_stdio() -> None:
    """把本进程 stdout/stderr 钉在 UTF-8 + replace，必须在任何输出之前调用。

    中文 Windows 控制台的 cp936 编不了 U+FFFD，子进程的乱码行会在父进程
    回显时反向炸掉监控器自身；个别流不是 TextIOWrapper 或没有 reconfigure
    时静默降级，由 ``_emit`` 的逐行兜底接管。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _emit(line: str) -> None:
    """向 stdout 写一行的唯一出口，保证自身永不抛异常。

    即使 ``_force_utf8_stdio`` 失败、stdout 仍是编不了码的严格模式流
    （如真实 GBK 控制台），也要把哨兵等关键行降级为字节写出；两条路
    全部失败时彻底静默——监控器自己绝不能成为崩溃源。
    """
    try:
        print(line, flush=True)
        return
    except Exception:
        pass
    try:
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is None:
            return
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        buffer.write(line.encode(encoding, "replace") + b"\n")
        buffer.flush()
    except Exception:
        pass


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
        self.sentinel_emitted = False
        self.child_rc: int | None = None
        self.started_at: float | None = None
        self.break_at, self.term_at, self.kill_at = self._escalation(grace)
        self._break_sent = False
        self._terminated = False
        self._escalation_started: float | None = None

    @staticmethod
    def _escalation(grace: float) -> tuple[float, float, float]:
        """由 ``--grace`` 推导中断升级阈值（各自自转发 Ctrl+C 起算的秒数）。

        返回 (break_at, term_at, kill_at)。grace=30（默认）精确得到
        (5.0, 10.0, 20.0)，与历史硬编码行为一致；grace 变化时三者同向
        缩放且严格递增，极小 grace 由 1s/2s 下限保护，不出现 0 或负值。
        """
        break_at = max(1.0, grace / 6.0)
        term_at = max(break_at + 1.0, grace / 3.0)
        return break_at, term_at, term_at + 10.0

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
        # 逐行兜底在 _emit 内部完成：任何一行编码失败都不允许中断循环，
        # 否则读线程静默死亡、后续 pytest 输出全部丢失、心跳停在旧值。
        for line in child.stdout:  # type: ignore[union-attr]
            line = line.rstrip("\r\n")
            self.tail.append(line)
            self.last_line = line
            _emit(line)

    def _emit_sentinel(self, status: str, rc: int | None, elapsed: int) -> None:
        """输出四行哨兵：一次运行最多一套，先置位再写，每行立即落盘。

        外层兜底据此判断是否需要补发，杜绝两套互相矛盾的哨兵；立即
        flush 保证哨兵不滞留缓冲区、不与 stderr 的 monitor error 错位。
        """
        if self.sentinel_emitted:
            return
        self.sentinel_emitted = True
        _emit(f"UNIT_STATUS={status}")
        _emit(f"UNIT_EXIT={rc if rc is not None else -1}")
        _emit(f"UNIT_ELAPSED_SECONDS={elapsed}")
        _emit(f"UNIT_END={_iso()}")

    def run(self) -> int:
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        env = os.environ.copy()
        # 子进程按 UTF-8 编码，与父进程的 UTF-8 解码对齐，从源头消除乱码；
        # 调用方已显式指定时尊重调用方。不设 PYTHONUTF8，避免连带改变
        # open() 默认编码与文件系统编码这类侵入性行为。
        env.setdefault("PYTHONIOENCODING", "utf-8")
        child = subprocess.Popen(
            self.cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
            env=env,
        )
        reader = threading.Thread(target=self._reader, args=(child,), daemon=True)
        reader.start()

        # 信号处理器是进程级全局状态：run() 可能被宿主进程（如 pytest 自身）直接
        # 调用，装上的处理器必须在 finally 里原样还回，否则宿主从此收不到
        # Ctrl+C。只登记确实替换成功的信号——装不上（非主线程抛 ValueError）
        # 也就无需恢复。
        sigs = [signal.SIGINT]
        if hasattr(signal, "SIGBREAK"):
            sigs.append(signal.SIGBREAK)
        restore: list[tuple[int, object]] = []
        try:
            for sig in sigs:
                try:
                    previous = signal.getsignal(sig)
                    signal.signal(sig, self._on_signal)
                except (ValueError, OSError):
                    continue
                restore.append((sig, previous))

            t0 = time.monotonic()
            self.started_at = t0
            next_beat = t0 + self.heartbeat
            rc: int | None = None
            while True:
                rc = child.poll()
                self.child_rc = rc
                if rc is not None:
                    break
                now = time.monotonic()
                if now >= next_beat:
                    next_beat = now + self.heartbeat
                    _emit(
                        f"[monitor] elapsed_seconds={int(now - t0)} child_pid={child.pid} "
                        f"last={self.last_line[-90:]!r}",
                    )
                if self.interrupt_requested:
                    if not self.forwarded:
                        _emit("[monitor] interrupt requested -> forwarding Ctrl+C to pytest")
                        self._forward_ctrl_c(child)
                    else:
                        since = now - (self._escalation_started or now)
                        # 每一级用独立布尔防重，跨平台条件互不依赖顺序
                        if since > self.break_at and not self._break_sent and os.name == "nt":
                            _emit("[monitor] Ctrl+C ineffective -> CTRL_BREAK")
                            self._break_sent = True
                            try:
                                ctypes.windll.kernel32.GenerateConsoleCtrlEvent(1, child.pid)
                            except Exception:
                                pass
                        if since > self.term_at and not self._terminated:
                            _emit("[monitor] signals ignored -> terminate")
                            self._terminated = True
                            child.terminate()
                            # 拒绝退出的最后兜底：自 terminate 起 kill_at - term_at 秒后强杀
                            kill_timer = threading.Timer(
                                self.kill_at - self.term_at, child.kill, ()
                            )
                            # 兜底线程不得拖住监控器（乃至宿主进程）退出
                            kill_timer.daemon = True
                            kill_timer.start()
                time.sleep(0.5)

            reader.join(timeout=5)
            elapsed = int(time.monotonic() - t0)
            status = "interrupted" if self.interrupt_requested else self._classify(rc)
            self._emit_sentinel(status, rc, elapsed)
            # 哨兵已落盘且 _emit 永不抛异常：tail 重放不可能再催生第二套哨兵
            _emit("[monitor] final tail (last lines of pytest output):")
            for line in list(self.tail)[-TAIL_LINES:]:
                _emit("  | " + line)
            return 0 if status == "completed" else 1
        finally:
            for sig, previous in restore:
                try:
                    signal.signal(sig, previous)
                except (ValueError, OSError, TypeError):
                    # TypeError：原处理器不是 Python 可安装对象（getsignal 可能
                    # 返回 None 表示非 Python 安装），无法精确还原只能放弃
                    pass

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
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--heartbeat", type=float, default=45.0)
    parser.add_argument("--grace", type=float, default=30.0)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    ns = parser.parse_args(argv[1:])
    pytest_args = ns.pytest_args or DEFAULT_PYTEST_ARGS
    cmd = [sys.executable, "-u", "-m", "pytest", *pytest_args]
    _emit(f"UNIT_START={_iso()} cmd={' '.join(cmd)}")
    monitor = Monitor(cmd, heartbeat=ns.heartbeat, grace=ns.grace)
    try:
        return monitor.run()
    except BaseException as e:  # 兜底：任何异常也要留哨兵（且只有一套）
        try:
            print(f"monitor error: {e!r}", file=sys.stderr)
        except Exception:
            pass
        if not monitor.sentinel_emitted:
            elapsed = -1
            if monitor.started_at is not None:
                elapsed = int(time.monotonic() - monitor.started_at)
            monitor._emit_sentinel("unknown", monitor.child_rc, elapsed)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
