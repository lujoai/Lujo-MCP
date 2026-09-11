"""Windows 终止原语（DESIGN_C2 §2.2 / DESIGN_C3 §2.1、§2.2、§3；B15 / W3-1）。

**ctypes 只允许出现在本平台件内**；POSIX 禁止导入本模块——终止后端经
:mod:`app.mcp.protocol.termination` 包的 ``load_platform_module()`` 按
``sys.platform`` 惰性加载。

提供的原语：

- :class:`ProcessTreeJob`：每**尝试**一个 Job（不是整个调用共用一个，C3
  §2.1 每尝试独立资源）。``create`` 用 ``CreateJobObjectW`` 默认安全属性
  （**句柄不可继承**，孙进程无法自恃句柄保活；配合 heavy_spawn 的
  ``close_fds``，Job 句柄不到达子/孙进程）并置
  ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``——父进程异常死亡时 OS 直接杀树，
  不依赖 Python 清理顺序。``assign`` 失败不抛异常，返回 False 并记录
  ``last_assign_winerror`` 交 C3 降级链判定（ACCESS_DENIED → breakaway
  重试）。``close_once`` 恰好一次（closed 标志 + 原子翻牌）；KILL_ON_JOB_CLOSE
  在最后一个句柄关闭时杀整树。尝试结束的 close **不等于**调用已结算、
  **不等于**进程退出已证实（C1 §4.3）——记账属于注册表层。
- :func:`generate_console_break`：``GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT,
  pid)``；返回 (是否成功, winerror)。宿主无控制台（服务/GUI 宿主）时必然
  失败——协作级可用性以「该子进程实际控制台归属」的条目能力快照为准
  （C3 §2.2 R14），本函数的返回值只作当次事实记录。
- :func:`console_present`：宿主控制台探测（GetConsoleWindow / GetConsoleCP），
  仅作启动期预判与矩阵归档（C3 §3），不替代逐次记账。
- :func:`process_exists`：OpenProcess 存在性检查（残留扫描用）。**禁止**用
  ``os.kill(pid, 0)`` 做 Windows 存在性检查——CPython 在 Windows 上对非
  CTRL_* 信号一律 TerminateProcess，sig=0 会直接杀掉目标。
- 常量与错误分类：降级链（job / breakaway+job / direct-child）与矩阵环境名
  的字符串字面量在 :mod:`app.mcp.protocol.termination` 统一定义防漂移。
"""

from __future__ import annotations

import sys
import threading

if sys.platform != "win32":  # pragma: no cover —— POSIX 上禁止导入本平台件
    raise ImportError(
        "app.mcp.protocol.termination._win32 仅可在 Windows 导入；"
        "POSIX 必须经 app.mcp.protocol.termination.load_platform_module() 惰性加载"
    )

import ctypes
from ctypes import wintypes

# ── Windows 常量（降级链与矩阵引用，防漂移的唯一出处） ───────────────────
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CTRL_BREAK_EVENT = 1
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
HANDLE_FLAG_INHERIT = 0x00000001
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_HANDLE = 6
ERROR_INVALID_PARAMETER = 87
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Job 信息类：JobObjectExtendedLimitInformation
_JobObjectExtendedLimitInformation = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def get_handle_flags(handle: int) -> int:
    """返回句柄标志位（测试/诊断用：断言不可继承）。"""
    flags = wintypes.DWORD(0)
    if not _kernel32.GetHandleInformation(handle, ctypes.byref(flags)):
        raise OSError(ctypes.get_last_error(), "GetHandleInformation failed")
    return flags.value


def process_exists(pid: int) -> bool:
    """OpenProcess 存在性检查（残留扫描用；**不得**用 os.kill(pid, 0)）。"""
    handle = _kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
    )
    if not handle:
        err = ctypes.get_last_error()
        if err in (ERROR_INVALID_PARAMETER, ERROR_INVALID_HANDLE):
            return False  # 进程不存在
        return False  # 其他访问失败按不存在处理（残留扫描宁可重杀一次 Job）
    try:
        exit_code = wintypes.DWORD(0)
        if _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return exit_code.value == STILL_ACTIVE
        return True  # 查询失败按仍存活处理（宁可重复确认）
    finally:
        _kernel32.CloseHandle(handle)


def console_present() -> bool:
    """宿主控制台探测（启动期预判；逐次判定读条目能力快照，C3 §2.2 R14）。"""
    return bool(_kernel32.GetConsoleWindow()) or bool(_kernel32.GetConsoleCP())


def generate_console_break(pid: int) -> tuple[bool, int | None]:
    """对同控制台新进程组目标投递 CTRL_BREAK；返回 (是否成功, winerror)。

    失败（无控制台 / 目标不属于本控制台）由调用方降级直杀并记 warning
    （C3 §2.2），Job 兜底不受影响。
    """
    ok = _kernel32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, int(pid))
    if not ok:
        return False, ctypes.get_last_error()
    return True, None


def is_access_denied(winerror: int | None) -> bool:
    """Assign 失败是否为 ACCESS_DENIED(5)（C3 §2.1 ② breakaway 重试判据）。"""
    return winerror == ERROR_ACCESS_DENIED


class ProcessTreeJob:
    """每尝试一个 Job Object：KILL_ON_JOB_CLOSE 整树回收兜底。

    句柄不可继承（默认安全属性）；``close_once`` 恰好一次；尝试结束的关闭
    **不等于**调用结算或退出证实（C1 §4.3，记账在注册表层）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handle: int | None = None
        self.closed = False
        self.last_assign_winerror: int | None = None
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        self._handle = int(handle)
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            self._handle,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            err = ctypes.get_last_error()
            _kernel32.CloseHandle(self._handle)
            self._handle = None
            self.closed = True
            raise OSError(err, "SetInformationJobObject(KILL_ON_JOB_CLOSE) failed")

    @classmethod
    def create(cls) -> "ProcessTreeJob":
        """每尝试创建一个新 Job（无全局单例，实例间互不影响）。"""
        return cls()

    @property
    def raw_handle(self) -> int | None:
        """底层句柄值（测试/诊断用；关闭后为 None）。"""
        return self._handle

    def assign(self, proc_handle: int) -> bool:
        """把子进程收进 Job；成功 True，失败 False 并记录 winerror。

        失败不抛异常——C3 §2.1 降级链需要读错误码决策（ACCESS_DENIED →
        breakaway 重试一次 → direct-child）。
        """
        with self._lock:
            if self.closed or self._handle is None:
                self.last_assign_winerror = ERROR_INVALID_HANDLE
                return False
            handle = self._handle
        if not _kernel32.AssignProcessToJobObject(handle, int(proc_handle)):
            self.last_assign_winerror = ctypes.get_last_error()
            return False
        self.last_assign_winerror = None
        return True

    def is_process_in_job(self, proc_handle: int) -> bool:
        """IsProcessInJob 校验（探测/测试用）。"""
        with self._lock:
            handle = self._handle
        result = wintypes.BOOL(0)
        if not handle or not _kernel32.IsProcessInJob(
            int(proc_handle), handle, ctypes.byref(result)
        ):
            return False
        return bool(result.value)

    def close_once(self) -> None:
        """恰好一次关闭（closed 标志 + 原子翻牌）。

        KILL_ON_JOB_CLOSE 在最后一个 Job 句柄关闭时终止整树；关闭本身幂等，
        关闭后 ``assign`` 返回 False（INVALID_HANDLE）。
        """
        with self._lock:
            if self.closed or self._handle is None:
                self.closed = True
                return
            handle = self._handle
            self._handle = None
            self.closed = True
        _kernel32.CloseHandle(handle)
