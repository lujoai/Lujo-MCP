"""W2-2（B08 前置）heavy_worker_entry `python -m` 引导验收测试。

覆盖 DESIGN_C2 §1.1/§2.1 子进程入口时序：接管结果写端 → 读请求**原始字节**
帧 → 发 ``R`` → 阻塞等 ``G`` → go 后才 ``pickle.loads`` / 导入 handler →
执行 → 结果帧。关键约束（CHECKLIST W2-2）：go 前**不反序列化、不导入
handler**；入口只依赖 stdlib 与 heavy_spawn/heavy_process（均无 server
副作用），维持轻量入口原则。

B 类·新机制验收：断言新构件不变量；「模块不存在」不构成红灯证据（C1 §7）。
"""

from __future__ import annotations

import os
import pickle
import sys

import pytest

import app.mcp.protocol.heavy_spawn as hs

HANDLERS = "tests._heavy_entry_handlers"
MARKER_MODULE = "tests._heavy_entry_import_marker"


def _entry_cmd(module: str, name: str) -> list[str]:
    return [
        sys.executable, "-m", "app.mcp.protocol.heavy_worker_entry",
        "--lujo-heavy-worker", module, name,
    ]


def _pickled(arguments) -> bytes:
    return pickle.dumps(arguments, protocol=pickle.HIGHEST_PROTOCOL)


def _deadline(seconds: float = 15.0) -> float:
    return time.monotonic() + seconds


import time  # noqa: E402  —— 置于常量后便于阅读


def test_full_roundtrip_via_real_entry():
    """真实入口全链路：请求帧 → R → G → 执行 → 结果帧；退出码 0。"""
    attempt = hs.spawn_attempt(1, _entry_cmd(HANDLERS, "echo"))
    try:
        payload = hs.handshake(attempt, _pickled({"n": 7}), deadline=_deadline(),
                               allow_commit=lambda: True)
        status, value = pickle.loads(payload)
        assert status == "ok"
        assert value == {"n": 7}
    finally:
        exitcode = hs.terminate_and_reap(attempt, grace=5.0)
    assert exitcode == 0


def test_no_handler_import_before_go():
    """核心约束：G 提交前 handler 模块未被导入（标记文件不存在）；
    G 提交并执行后才存在。"""
    import tempfile

    marker = os.path.join(tempfile.gettempdir(), f"entry_marker_{os.getpid()}.txt")
    if os.path.exists(marker):
        os.remove(marker)
    os.environ["ENTRY_MARKER_FILE"] = marker

    # ① 提交被拒：go 永不写出 → handler 不导入
    attempt = hs.spawn_attempt(1, _entry_cmd(MARKER_MODULE, "noop"))
    with pytest.raises(hs.CommitRefused):
        hs.handshake(attempt, _pickled({}), deadline=_deadline(),
                     allow_commit=lambda: False)
    hs.terminate_and_reap(attempt, grace=5.0)
    assert not os.path.exists(marker), "G 提交前 handler 已被导入（违反 go 前不导入约束）"

    # ② 正常提交：go 后执行 → handler 导入
    try:
        attempt2 = hs.spawn_attempt(2, _entry_cmd(MARKER_MODULE, "noop"))
        payload = hs.handshake(attempt2, _pickled({}), deadline=_deadline(),
                               allow_commit=lambda: True)
        assert pickle.loads(payload) == ("ok", {"ok": True})
        assert os.path.exists(marker), "G 提交后 handler 应已被导入"
    finally:
        hs.terminate_and_reap(attempt2, grace=5.0)
        os.environ.pop("ENTRY_MARKER_FILE", None)
        if os.path.exists(marker):
            os.remove(marker)


def test_handler_exception_returns_structured_error():
    """handler 抛异常 → 子进程回传 ("error", "ValueError: ...")，不挂死。"""
    attempt = hs.spawn_attempt(1, _entry_cmd(HANDLERS, "boom"))
    try:
        payload = hs.handshake(attempt, _pickled({}), deadline=_deadline(),
                               allow_commit=lambda: True)
        status, detail = pickle.loads(payload)
        assert status == "error"
        assert "ValueError" in detail
        assert "boom on purpose" in detail
    finally:
        exitcode = hs.terminate_and_reap(attempt, grace=5.0)
    assert exitcode == 0  # 结构化错误是正常回传，非崩溃


def test_unserializable_result_returns_structured_error():
    """结果不可 pickle → 退化 ("error", "heavy tool result not serializable")。"""
    attempt = hs.spawn_attempt(1, _entry_cmd(HANDLERS, "unserializable"))
    try:
        payload = hs.handshake(attempt, _pickled({}), deadline=_deadline(),
                               allow_commit=lambda: True)
        status, detail = pickle.loads(payload)
        assert status == "error"
        assert detail == "heavy tool result not serializable"
    finally:
        hs.terminate_and_reap(attempt, grace=5.0)


def test_invalid_request_pickle_returns_structured_error_after_go():
    """请求体不是合法 pickle：go 前只读原始字节不解析；go 后解析失败走
    结构化错误（不崩溃、不静默）。"""
    attempt = hs.spawn_attempt(1, _entry_cmd(HANDLERS, "echo"))
    try:
        payload = hs.handshake(attempt, b"\x00not-a-pickle", deadline=_deadline(),
                               allow_commit=lambda: True)
        status, detail = pickle.loads(payload)
        assert status == "error"
    finally:
        hs.terminate_and_reap(attempt, grace=5.0)


def test_entry_rejects_bad_argv():
    """入口参数不合法 → 退出码 2（不接管通道、不读 stdin）。"""
    from app.mcp.protocol import heavy_worker_entry

    assert heavy_worker_entry.main(["wrong"]) == 2
    assert heavy_worker_entry.main([]) == 2
    assert heavy_worker_entry.main(
        ["--lujo-heavy-worker", "only_module"]
    ) == 2


def test_entry_flag_matches_frozen_flag():
    """源码入口 flag 与冻结 flag 同串（C2 §1.1：源码/冻结同一入口解析）。"""
    from app.mcp.protocol import heavy_process, heavy_worker_entry

    assert heavy_worker_entry.WORKER_FLAG == heavy_process._FROZEN_WORKER_FLAG
