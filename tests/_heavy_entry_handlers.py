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


def slow_sync(arguments):
    """模拟阻塞 heavy handler（sleep 0.3s）→ 结构化成功回传。"""
    import time

    time.sleep(0.3)
    return {"matched": True, "diffs": [], "silent_failure": False}


def business_failure(arguments):
    """重型工具正常执行但返回业务失败字典。"""
    return {"error": "heavy business failure occurred", "details": "invalid business state"}


def conclusion_success(arguments):
    """重型结论工具（如 verify_ui）验证通过。"""
    return {"matched": True, "diffs": [], "silent_failure": False}


def conclusion_failure(arguments):
    """重型结论工具验证不通过（matched=False 且含说明性 error）。属于业务结论而非工具崩溃。"""
    return {
        "matched": False,
        "diffs": [{"field": "status", "expected": 200, "actual": 500}],
        "silent_failure": False,
        "error": "spec mismatch on status code",
    }


def conclusion_bare_error(arguments):
    """重型结论工具返回裸 error 字典（无 matched 键），应被判为工具执行失败。"""
    return {"error": "bare error without matched key"}


def crash_exit(arguments):
    """模拟子进程非零异常崩溃退出（exitcode=42）。"""
    import os

    os._exit(42)


def slow_hang(arguments):
    """长时间阻塞，用于验证调用超时及子进程强杀回收。"""
    import time

    time.sleep(float(arguments.get("sleep", 10.0)))
    return {"ok": True}


def sensitive_echo(arguments):
    """返回包含敏感信息的业务载荷，用于验证指标与日志脱敏。"""
    return {
        "ok": True,
        "api_key": "sk-secret-token-12345",
        "secret_path": "C:\\Users\\SecretUser\\confidential.txt",
    }
