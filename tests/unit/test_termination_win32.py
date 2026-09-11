"""W3-1（B15）Windows 终止原语验收：ProcessTreeJob / KILL_ON_JOB_CLOSE /
close_once / CTRL_BREAK / 能力探测原语。

对应 DESIGN_C2 §2.2（Job 助手签名级设计）与 DESIGN_C3 §2.1/§2.2/§3：
- 每**尝试**一个 Job（实例独立，不是全局共用）；
- 句柄**不可继承**（CreateJobObjectW 默认安全属性）；
- ``close_once`` 恰好一次（closed 标志 + 原子翻牌）；KILL_ON_JOB_CLOSE 在
  最后一个句柄关闭时杀整树；
- 协作级 CTRL_BREAK 状态与控制台归属一致（宿主无控制台 → 不可达）；
- ctypes 只允许出现在本平台件内；POSIX 不导入本模块（包级惰性加载）。

POSIX 环境整文件 skip（环境理由：Windows 专属原语）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

import pytest

if sys.platform != "win32":
    pytest.skip("Windows 专属终止原语（POSIX skip：环境理由）", allow_module_level=True)

import app.mcp.protocol.termination._win32 as w32

_SLEEP_CHILD = "import time; time.sleep(60)"


def _spawn_child(extra_args: list[str] | None = None) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _SLEEP_CHILD, *(extra_args or [])],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=w32.CREATE_NEW_PROCESS_GROUP,
    )


def _wait_dead(pred, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_create_job_handle_not_inheritable():
    """句柄不可继承（CreateJobObjectW 默认安全属性，C3 §2.1 硬约束）。"""
    job = w32.ProcessTreeJob.create()
    try:
        assert job.closed is False
        assert job.last_assign_winerror is None
        flags = wintype_flags = w32.get_handle_flags(job.raw_handle)
        assert not (flags & w32.HANDLE_FLAG_INHERIT)
    finally:
        job.close_once()


def test_assign_child_then_job_membership():
    """assign 成功 → IsProcessInJob 为真；assign 失败路径返回 False 并记录
    winerror（交 C3 降级链判定）。"""
    job = w32.ProcessTreeJob.create()
    child = _spawn_child()
    try:
        assert job.assign(int(child._handle)) is True
        assert job.last_assign_winerror is None
        assert job.is_process_in_job(int(child._handle)) is True
    finally:
        job.close_once()
        child.wait(timeout=10)
    # KILL_ON_JOB_CLOSE：close 后子进程被终止
    assert child.returncode != 0 or child.poll() is not None


def test_kill_on_job_close_reaps_whole_tree(tmp_path):
    """核心保证：**先 assign、后由子进程拉孙进程**（生产时序：C2 §2.1 中
    Assign 在 go 之前、业务在 go 之后——assign 后创建的后代自动入 Job）→
    close_once → 父子全灭。若孙进程先于 assign 出生则不会被追溯收编（Job
    语义本身如此），这正是「Assign 必须先于 go」时序约束的实证。"""
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
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, start_file, report],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=w32.CREATE_NEW_PROCESS_GROUP,
    )

    job = w32.ProcessTreeJob.create()
    try:
        # 生产时序：先 Assign（⑤），后放行业务（go 之后才可能有孙进程）
        assert job.assign(int(child._handle)) is True
        assert job.is_process_in_job(int(child._handle)) is True
        with open(start_file, "w", encoding="utf-8") as fh:
            fh.write("go")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not os.path.exists(report):
            time.sleep(0.05)
        with open(report, encoding="utf-8") as fh:
            gc_pid = int(fh.read().strip())

        job.close_once()

        assert _wait_dead(lambda: child.poll() is not None), "直接子未随 Job 关闭退出"
        assert _wait_dead(lambda: not w32.process_exists(gc_pid)), (
            "孙进程未随 Job 关闭退出（整树回收失效）"
        )
    finally:
        job.close_once()
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
        # 兜底清理本测试制造的孙进程（正常路径已由 Job 杀掉，taskkill 为空操作）
        if os.path.exists(report):
            with open(report, encoding="utf-8") as fh:
                subprocess.run(["taskkill", "/F", "/PID", fh.read().strip()],
                               capture_output=True, timeout=10)


def test_close_once_idempotent_and_assign_after_close_fails():
    """close_once 恰好一次：二次/三次关闭安全；关闭后 assign 返回 False 且
    记录 INVALID_HANDLE，绝不抛未定义异常。"""
    job = w32.ProcessTreeJob.create()
    job.close_once()
    assert job.closed is True
    job.close_once()  # 幂等
    job.close_once()
    assert job.closed is True

    child = _spawn_child()
    try:
        assert job.assign(int(child._handle)) is False
        assert job.last_assign_winerror == w32.ERROR_INVALID_HANDLE
    finally:
        child.kill()
        child.wait(timeout=10)


def test_two_jobs_independent_per_attempt():
    """每尝试一个 Job：两个实例并存互不影响；关一个不杀另一个的子进程。"""
    job1 = w32.ProcessTreeJob.create()
    job2 = w32.ProcessTreeJob.create()
    child1 = _spawn_child()
    child2 = _spawn_child()
    try:
        assert job1.assign(int(child1._handle)) is True
        assert job2.assign(int(child2._handle)) is True
        job1.close_once()
        assert _wait_dead(lambda: child1.poll() is not None)
        assert child2.poll() is None, "关闭 job1 不应影响 job2 的子进程"
    finally:
        job1.close_once()
        job2.close_once()
        for child in (child1, child2):
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)


def test_generate_console_break_status_consistent_with_console_presence():
    """协作级 CTRL_BREAK 状态与控制台归属一致：宿主有控制台 → 对同控制台
    新进程组子进程投递成功；无控制台宿主 → (False, winerror)。"""
    child = _spawn_child()
    try:
        delivered, winerror = w32.generate_console_break(child.pid)
        if w32.console_present():
            assert delivered is True and winerror is None
        else:
            assert delivered is False and winerror is not None
    finally:
        child.kill()
        child.wait(timeout=10)


def test_capability_probe_primitives():
    """能力探测原语：console_present 布尔、ACCESS_DENIED 分类、降级链常量。"""
    assert isinstance(w32.console_present(), bool)
    assert w32.is_access_denied(w32.ERROR_ACCESS_DENIED) is True
    assert w32.is_access_denied(w32.ERROR_INVALID_HANDLE) is False
    assert w32.CREATE_BREAKAWAY_FROM_JOB == 0x01000000
    assert w32.CREATE_NO_WINDOW == 0x08000000
    assert w32.CREATE_NEW_PROCESS_GROUP == 0x200
    assert w32.CTRL_BREAK_EVENT == 1
    assert w32.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE == 0x2000


def test_package_import_lazy_and_backend_loader():
    """POSIX 导入不得失败（结构约束）：termination 包导入不带平台件副作用；
    平台后端经 load_platform_module() 惰性加载。"""
    import importlib

    package = importlib.import_module("app.mcp.protocol.termination")
    backend = package.load_platform_module()
    assert backend is w32  # Windows 上加载 _win32
    # 后端枚举与本件字符串字面量一致（C3 §4 防漂移）
    assert package.BACKEND_JOB == "job"
    assert package.BACKEND_BREAKAWAY_JOB == "breakaway+job"
    assert package.BACKEND_DIRECT_CHILD == "direct-child"
