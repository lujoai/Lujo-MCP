"""终止后端包（DESIGN_C3；B15 / W3）。

平台件按 ``sys.platform`` **惰性加载**：POSIX 环境导入本包不得失败、不得
加载 ``_win32``（C3/W3-1 硬约束；ctypes 平台件只允许在平台件内部）。

后端枚举（字符串字面量与 C3 §4 降级矩阵环境名一一对应，防漂移）：

- ``"job"``：Windows，Assign 成功（整树 + KILL_ON_JOB_CLOSE 兜底）；
- ``"breakaway+job"``：Windows 嵌套 Job 且允许 breakaway（重试一次后成功）；
- ``"direct-child"``：Windows 最弱环境（无树回收保证，降档记账）；
- ``"posix-pgroup"``：POSIX 进程组（W3-2）。

每**尝试**的终止决策读该尝试的**条目能力快照**（实际 Assign 结果）；
启动期能力探测缓存只作预判（C3 §3 R14）。
"""

from __future__ import annotations

import sys

BACKEND_JOB = "job"
BACKEND_BREAKAWAY_JOB = "breakaway+job"
BACKEND_DIRECT_CHILD = "direct-child"
BACKEND_POSIX_PGROUP = "posix-pgroup"


def load_platform_module():
    """按平台惰性加载终止原语件；POSIX 加载 _posix（W3-2）。"""
    if sys.platform == "win32":
        from app.mcp.protocol.termination import _win32

        return _win32
    from app.mcp.protocol.termination import _posix

    return _posix
