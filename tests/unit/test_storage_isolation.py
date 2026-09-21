"""W2（P2-TEST-1 / P4「factory fixture 上移」）：storage factory 单例逐用例隔离证明。

缺陷背景：integration 的 ``_isolate_storage`` 名实不符（只清 ``errors._recent``）、
unit 侧 factory 重置仅在 conftest 导入时执行一次——memory 后端的进程级单例
（``app/runtime/core/storage/factory.py`` 的五个 store 引用：trace / session /
error / spec / knowledge）在用例间累积数据、产生顺序敏感。

红证据形态：用例 01 往 factory 单例（trace store + session store 会话注册表）
各写一条唯一 marker，用例 02（同进程、后执行）断言读不到。
修复前：02 读到 01 的写入（状态泄漏）→ 失败；修复后：autouse fixture 在用例间
重置 factory 单例 → 02 读不到 → 通过。
"""
import app.runtime.core.storage.factory as storage_factory

_MARKER_REQUEST_ID = "w2-isolation-marker-request-01"
_MARKER_SESSION_ID = "w2-isolation-marker-session-01"


def test_01_writer_leaves_factory_state():
    """写入者：往 factory 单例（trace store + session store）各写一条唯一 marker。

    本用例刻意不自行清理——逐用例隔离是 conftest autouse fixture 的职责，
    用例依赖它来验证 fixture 真正生效（而非用例自扫门前雪）。
    """
    trace_store = storage_factory.get_trace_store()
    trace_store.save_entry(_MARKER_REQUEST_ID, {"timestamp": 1.0, "step": "w2-marker"})
    session_store = storage_factory.get_session_store()
    session_store.save(_MARKER_SESSION_ID, {"marker": "w2"})

    # 用例内自产自销必须立即可读（否则是写入路径本身坏了，不是隔离问题）
    assert trace_store.get_entries(_MARKER_REQUEST_ID), "写入后立即读回失败（非隔离问题）"
    assert session_store.get(_MARKER_SESSION_ID) is not None, "写入后立即读回失败（非隔离问题）"


def test_02_reader_must_not_see_writer_state():
    """隔离证明：前一个用例写入 factory 单例的 marker 不得跨用例泄漏。"""
    trace_store = storage_factory.get_trace_store()
    leaked_entries = trace_store.get_entries(_MARKER_REQUEST_ID)
    assert not leaked_entries, (
        f"P2-TEST-1：factory trace store 单例跨用例泄漏——"
        f"读到上一用例写入的 {len(leaked_entries)} 条记录（逐用例重置未生效）"
    )
    session_store = storage_factory.get_session_store()
    leaked_session = session_store.get(_MARKER_SESSION_ID)
    assert leaked_session is None, (
        f"P2-TEST-1：factory session store（会话注册表）单例跨用例泄漏: {leaked_session}"
    )
