"""heavy handler 载体（W2-2 worker 入口验收测试用，可被子进程导入执行）。

本模块会被 worker 子进程在 **go 之后** 导入；配套的
``tests/_heavy_entry_import_marker.py`` 在模块顶层写导入标记，
用于证明「go 前不导入 handler」。
"""

from __future__ import annotations

import threading


def echo(arguments):
    """原样返回入参（父→子→父 pickle 往返校验）。"""
    return arguments


def boom(arguments):
    """handler 内抛异常 → 子进程结构化 error 回传。"""
    raise ValueError("boom on purpose")


def unserializable(arguments):
    """返回不可 pickle 对象（threading.Lock 不可序列化）→ 子进程退化结构化错误。"""
    return threading.Lock()
