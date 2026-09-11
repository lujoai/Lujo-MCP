"""导入标记 handler（W2-2「go 前不导入 handler」证据用）。

模块**顶层**在导入时向 ``ENTRY_MARKER_FILE`` 环境变量指向的文件追加一行。
父侧据此断言：G 提交前该文件不存在（未导入），G 提交后存在（已导入）。
"""

from __future__ import annotations

import os

_marker = os.environ.get("ENTRY_MARKER_FILE")
if _marker:
    with open(_marker, "a", encoding="utf-8") as fh:
        fh.write("imported\n")


def noop(arguments):
    return {"ok": True}
