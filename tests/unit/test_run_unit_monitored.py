"""单元测试：scripts/run_unit_monitored.py 的编码容错、哨兵唯一性与升级阶梯。

回归背景：中文 Windows（GBK 控制台）下，子进程按 GBK 写出的中文警告字节被
父进程按 UTF-8 解码成 U+FFFD 后，监控器向 cp936 严格模式 stdout 回显会抛
UnicodeEncodeError——曾导致读线程静默死亡（后续 pytest 输出全丢）、final tail
重放崩溃、以及外层等待器收到两套互相矛盾的 UNIT_STATUS 哨兵。
"""
import configparser
import io
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import run_unit_monitored as m

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_emit_survives_unencodable_console_and_keeps_reader_alive(monkeypatch):
    """T1（进程内）：stdout 是编不了 U+FFFD 的 cp936 严格模式流时——

    哨兵必须恰好输出一次且不崩（_emit 的降级写入路径），读线程必须存活、
    子进程全部行都进入 tail（不因单行编码失败而中断循环）。
    刻意不调用 _force_utf8_stdio：本用例验证的正是 reconfigure 没生效时
    _emit 降级路径也能救命。
    """
    child_script = (
        "import sys\n"
        "sys.stdout.buffer.write(b'ascii line one\\n')\n"
        "sys.stdout.buffer.write(b'ascii line two\\n')\n"
        "sys.stdout.buffer.write('UserWarning: 中文警告\\n'.encode('gbk'))\n"
        "sys.stdout.buffer.flush()\n"
    )
    raw = io.BytesIO()
    # 严格模式（不带 errors="replace"）：模拟会抛 UnicodeEncodeError 的 GBK 控制台
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp936"))

    monitor = m.Monitor(
        [sys.executable, "-c", child_script],
        heartbeat=3600.0,  # 设大避免心跳输出干扰哨兵计数
        grace=30.0,
    )
    assert monitor.run() == 0

    sys.stdout.flush()
    out = raw.getvalue().decode("cp936", errors="replace")
    assert out.count("UNIT_STATUS=") == 1
    assert "UNIT_STATUS=completed" in out
    assert "UNIT_EXIT=0" in out
    assert monitor.sentinel_emitted is True

    # 父进程按 UTF-8 解 GBK 字节的期望结果（与监控器 Popen 的解码参数一致）
    gbk_line = "UserWarning: 中文警告".encode("gbk").decode("utf-8", errors="replace")
    assert list(monitor.tail) == ["ascii line one", "ascii line two", gbk_line]
    assert monitor.tail[-1] == gbk_line


@pytest.mark.slow
def test_gbk_child_under_gbk_parent_emits_single_completed_sentinel(tmp_path):
    """T2（子进程端到端）：终审事故场景的自动化复现。

    监控器自身的 stdout 被显式 PYTHONIOENCODING=cp936 置成 GBK 严格模式
    （F6 的 setdefault 语义不得覆盖它），pytest 子进程用例里用
    sys.stdout.buffer 写出 GBK 中文警告字节，制造"父进程按 UTF-8 解
    GBK 字节"的真实事故条件。修复后必须：退出码 0、恰好一套 completed
    哨兵、final tail 重放确实执行。
    """
    test_file = tmp_path / "test_gbk_warn_scene.py"
    test_file.write_text(
        "import sys\n"
        "\n"
        "\n"
        "def test_gbk_warning_to_stdout():\n"
        "    sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
        "    sys.stdout.buffer.write('UserWarning: 中文警告 敏感字段\\n'.encode('gbk'))\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp936"
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "run_unit_monitored.py"),
            "--heartbeat",
            "5",
            str(test_file),
            "-q",
            "-s",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert proc.returncode == 0
    assert proc.stdout.count("UNIT_STATUS=") == 1
    assert "UNIT_STATUS=completed" in proc.stdout
    assert "UNIT_EXIT=0" in proc.stdout
    assert "UNIT_STATUS=unknown" not in proc.stdout
    assert "[monitor] final tail" in proc.stdout


def test_escalation_thresholds_scale_with_grace_and_keep_minimums():
    """T3（纯函数）：--grace 推导升级阈值——默认行为零变化 + 缩放单调 + 极小值安全。"""
    # 锁死默认路径：grace=30 必须与历史硬编码 5s/10s/20s 完全一致
    assert m.Monitor._escalation(30.0) == (5.0, 10.0, 20.0)
    for grace in (6.0, 30.0, 60.0, 300.0):
        break_at, term_at, kill_at = m.Monitor._escalation(grace)
        assert 0 < break_at < term_at < kill_at
    # 极小 grace 不抛异常，且阈值经下限保护后仍为正且严格递增
    for grace in (0.0, 0.5, 1.0):
        break_at, term_at, kill_at = m.Monitor._escalation(grace)
        assert 0 < break_at < term_at < kill_at


def test_run_restores_signal_handlers():
    """T4：run() 结束必须把 SIGINT/SIGBREAK 处理器原样还回（is 比较同一对象）。

    信号处理器是进程级全局状态：Monitor 在宿主进程（如 pytest 自身）内被调用
    时若不恢复，宿主从此收不到 Ctrl+C（信号只喂给一个已结束的 Monitor 实例），
    只能靠暴力 terminate 收场、失去优雅中断。
    """
    sigbreak = getattr(signal, "SIGBREAK", None)
    before_int = signal.getsignal(signal.SIGINT)
    before_break = signal.getsignal(sigbreak) if sigbreak is not None else None

    monitor = m.Monitor([sys.executable, "-c", "pass"], heartbeat=3600.0, grace=30.0)
    assert monitor.run() == 0

    assert signal.getsignal(signal.SIGINT) is before_int
    if sigbreak is not None:
        assert signal.getsignal(sigbreak) is before_break


@pytest.mark.slow
def test_default_args_keep_summary_line_visible(tmp_path):
    """T5：pytest 统计行必须能穿过监控器管道；默认参数与 pytest.ini 的 -q 耦合锁死。

    pytest.ini 的 addopts 已含 -q；脚本默认参数若再叠一个 -q 会成 -qq，
    pytest 在该安静级别会吞掉 "N passed in Xs" 统计行，监控器 final tail 随之失明。
    """
    # a) 刻意不传 -q 的最小 pytest 跑法，统计行必须出现在被监控器回显的内容里
    test_file = tmp_path / "test_minimal.py"
    test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    monitor = m.Monitor(
        [sys.executable, "-m", "pytest", str(test_file), "-p", "no:warnings"],
        heartbeat=3600.0,
        grace=30.0,
    )
    assert monitor.run() == 0
    assert any("1 passed" in line for line in monitor.tail)

    # b) 锁死耦合：pytest.ini 确实含 -q，DEFAULT_PYTEST_ARGS 绝不能再含 -q/-qq
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(REPO_ROOT / "pytest.ini", encoding="utf-8")
    assert "-q" in parser["pytest"]["addopts"]
    assert "-q" not in m.DEFAULT_PYTEST_ARGS
    assert "-qq" not in m.DEFAULT_PYTEST_ARGS
