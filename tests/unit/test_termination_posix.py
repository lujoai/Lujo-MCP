"""W3-2（B15）POSIX 终止原语验收：killpg 两级 / ESRCH 不升错误 / 两判据分开 /
防误发 / 首领先退反例。

对应 DESIGN_C3 §1、§2、§5：
- killpg 两级（SIGTERM → grace → SIGKILL）一律对**出生时保存的 pgid**，
  **永不重查**；
- ESRCH（组空 / 不存在）不升错误；
- 「直接子收割 / 组清理」两判据**分开**（不得以直接子 exitcode 冒充组清理）；
- 防误发：禁止 ``killpg(0)`` / ``killpg(-1)`` / 对宿主自身组发信号；
- 反例必须覆盖：**首领先退出、孙进程忽略 SIGTERM**——SIGTERM 杀不掉、
  SIGKILL 兜底才灭。

Windows 平台：直接导入本模块必须 ImportError（平台守卫），行为用例整体
skip（环境理由：POSIX 专属；Linux CI / WSL 实测口径）。
"""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import time

import pytest

_POSIX_SPEC_NAME = "app.mcp.protocol.termination._posix"


def _module_spec_exists() -> bool:
    return importlib.util.find_spec(_POSIX_SPEC_NAME) is not None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 专属行为用例（Windows skip：环境理由）")
class TestPosixTerminationBehavior:
    """POSIX 行为用例（Linux CI / WSL 实测口径）。"""

    def test_two_tier_kills_whole_group(self, tmp_path):
        """真子进程（start_new_session）内起孙进程（sleep，不用 Playwright）：
        两级终止后组内全灭、直接子被收割。"""
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
        pgid = _posix.birth_pgid(child)  # 出生时保存，永不重查
        assert pgid == child.pid
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not report.exists():
            time.sleep(0.05)
        assert report.exists(), "前置：孙进程未出生"

        assert _posix.terminate_group_two_tier(pgid, grace=2.0, leader=child) is True
        assert _posix.child_gone(child), "直接子收割判据失败"
        assert not _posix.group_has_members(pgid), "组清理判据失败"

    def test_leader_exits_first_grandchild_ignores_sigterm(self, tmp_path):
        """**必测反例**：首领先退出、孙进程忽略 SIGTERM——直接子已收割但组内
        仍有成员（两判据分开的实证）；SIGTERM 杀不掉被忽略者，SIGKILL 兜底
        才清组。"""
        from app.mcp.protocol.termination import _posix

        report = tmp_path / "gc_pid.txt"
        done = tmp_path / "done.flag"
        gc_ready = tmp_path / "gc_ready.flag"
        child_code = (
            "import os, subprocess, sys, time\n"
            "gc_cmd = ('import signal, sys, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "open(sys.argv[1], \"w\").write(\"ready\"); time.sleep(60)')\n"
            "gc = subprocess.Popen([sys.executable, '-c', gc_cmd, sys.argv[3]],\n"
            "                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
            "                      stdin=subprocess.DEVNULL)\n"
            "while not os.path.exists(sys.argv[3]):\n"
            "    time.sleep(0.02)  # 等 SIG_IGN 已装好再退场（反例前提确定性）\n"
            "open(sys.argv[1], 'w').write(str(gc.pid))\n"
            "open(sys.argv[2], 'w').write('leader-done')\n"
        )
        child = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", child_code, str(report), str(done), str(gc_ready)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True, cwd="/tmp",
        )
        pgid = _posix.birth_pgid(child)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not report.exists():
            time.sleep(0.02)
        child.wait(timeout=30)  # 首领自然退出
        with open(report, encoding="utf-8") as fh:
            gc_pid = int(fh.read().strip())

        # 两判据分开：直接子已收割，但组未清空（孙进程存活）
        assert _posix.child_gone(child) is True
        assert _posix.group_has_members(pgid) is True
        assert _posix.process_exists(gc_pid) is True

        # SIGTERM 被孙进程忽略 → 组未清空（SIGTERM 级不足的实证）
        _posix.signal_group(pgid, signal.SIGTERM)
        time.sleep(0.3)
        assert _posix.process_exists(gc_pid) is True, "孙进程未忽略 SIGTERM，反例前提不成立"

        # 两级终止：SIGKILL 兜底清组
        assert _posix.terminate_group_two_tier(pgid, grace=2.0) is True
        assert not _posix.process_exists(gc_pid)
        assert not _posix.group_has_members(pgid)

    def test_esrch_is_swallowed_not_raised(self, tmp_path):
        """ESRCH（组空 / 不存在）静默返回，不升错误：用「已自然退出的独立
        会话子进程」的 pgid 构造确定性的空组。"""
        from app.mcp.protocol.termination import _posix

        child = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True, cwd="/tmp",
        )
        dead_pgid = _posix.birth_pgid(child)
        child.wait(timeout=30)
        assert _posix.group_has_members(dead_pgid) is False
        _posix.signal_group(dead_pgid, signal.SIGTERM)  # 不应抛
        assert _posix.terminate_group_two_tier(dead_pgid, grace=0.5) is True

    def test_signal_guards_against_special_pgids(self):
        """防误发：pgid<=1（含 killpg(0)/killpg(-1) 语义）与宿主自身组一律拒绝。"""
        from app.mcp.protocol.termination import _posix

        for bad in (0, -1, 1):
            with pytest.raises(ValueError):
                _posix.signal_group(bad, signal.SIGTERM)
            with pytest.raises(ValueError):
                _posix.terminate_group_two_tier(bad, grace=0.1)
        with pytest.raises(ValueError):
            _posix.terminate_group_two_tier(os.getpgrp(), grace=0.1)  # 宿主自身组

    def test_pdeathsig_probe_never_raises(self):
        """可选探测 PR_SET_PDEATHSIG：返回 bool 或 None（探测失败），绝不抛。"""
        from app.mcp.protocol.termination import _posix

        value = _posix.pdeathsig_supported()
        assert value is None or isinstance(value, bool)


class TestPosixPlatformGuardOnWindows:
    """Windows 上直接导入 _posix 必须 ImportError（平台守卫，Windows 可跑）。"""

    @pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 需要守卫验证")
    def test_direct_import_raises_import_error(self):
        import app.mcp.protocol.termination as termination

        assert _module_spec_exists(), "前置：_posix 模块应已存在（W3-2 交付物）"
        with pytest.raises(ImportError):
            importlib.import_module(_POSIX_SPEC_NAME)

    @pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 校验加载器选择")
    def test_loader_selects_win32_not_posix(self):
        import app.mcp.protocol.termination as termination
        import app.mcp.protocol.termination._win32 as w32

        assert termination.load_platform_module() is w32
