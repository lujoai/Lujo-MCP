"""轻量工具专用执行器自愈生命周期单元测试"""
import concurrent.futures
from app.mcp.protocol import server


def test_light_tool_executor_self_healing():
    """验证 _LIGHT_TOOL_EXECUTOR 在 shutdown 之后调用 _get_light_tool_executor() 能自动重建。"""
    # 获取当前执行器并强制 shutdown（模拟 stdio 退出或资源回收）
    initial_executor = server._get_light_tool_executor()
    assert not initial_executor._shutdown

    # 执行一次任务确认可用
    future = initial_executor.submit(lambda: 42)
    assert future.result() == 42

    # 模拟 shutdown
    initial_executor.shutdown(wait=True)
    assert initial_executor._shutdown

    # 再次获取：必须自愈为新执行器，且能正常执行任务
    healed_executor = server._get_light_tool_executor()
    assert not healed_executor._shutdown
    assert healed_executor is not initial_executor

    future2 = healed_executor.submit(lambda: 99)
    assert future2.result() == 99


def test_get_tool_executor_and_slots_uses_healed_executor():
    """验证 _get_tool_executor_and_slots 返回的轻量执行器不受历史 shutdown 影响。"""
    current_executor = server._get_light_tool_executor()
    current_executor.shutdown(wait=True)

    executor, slots, pool_type = server._get_tool_executor_and_slots("get_debug_context")
    assert pool_type == "light"
    assert executor is not None
    assert not executor._shutdown

    future = executor.submit(lambda: "ok")
    assert future.result() == "ok"


def test_stdio_pool_shutdown_does_not_poison_protocol_pool():
    """验证 stdio 关闭自己的池后，协议/HTTP 轻量池仍可执行。"""
    import app.mcp_server as stdio

    stdio_executor = stdio._get_tool_executor()
    protocol_executor, _, pool_type = server._get_tool_executor_and_slots(
        "get_debug_context"
    )
    assert pool_type == "light"
    assert protocol_executor is not None
    assert stdio_executor is not protocol_executor

    # 模拟 stdio cleanup_resources() 关闭其独立池；随后直接调用协议层选出的
    # 轻量执行器，等价于 HTTP/协议 tools/call 的实际执行对象。
    stdio_executor.shutdown(wait=True)
    assert stdio_executor._shutdown
    assert protocol_executor.submit(lambda: "protocol-ok").result(timeout=2) == "protocol-ok"

    # stdio 自身 getter 也必须保持原有自愈能力，避免该测试留下已关闭全局池。
    healed_stdio = stdio._get_tool_executor()
    assert healed_stdio is not stdio_executor
    assert not healed_stdio._shutdown


def test_stdio_cleanup_releases_both_executor_generations(monkeypatch):
    """B23：cleanup → getter 自愈重建 → 再次 cleanup，两代资源都释放。

    场景链（DESIGN_C1_SLOT_ACCOUNTING.md §6 表 #6）：
    1. 同代重复 cleanup 幂等——不重复清理已清理资源；
    2. 异代（getter 已自愈重建）cleanup 清理**新**实例——两代资源都释放。

    红灯形态（旧实现）：`_cleanup_done` 自持布尔置位后第二次 cleanup 永久
    短路（该布尔无任何调用路径能翻转），自愈重建出的第二代 executor 永远
    不会被 shutdown（B23 报告 #21：重建后的池不会被再次清理）。
    """
    import app.mcp_server as stdio

    # 隔离进程级副作用（本用例只关心 executor 生命周期）
    monkeypatch.setattr(stdio.settings, "storage_backend", "memory")
    monkeypatch.setattr(stdio, "_periodic_cleanup_task", None)
    monkeypatch.setattr(stdio, "uninstall_global_hook", lambda: None)
    monkeypatch.setattr(stdio, "shutdown_observability", lambda: None)

    first = stdio._get_tool_executor()
    stdio.cleanup_resources()
    assert first._shutdown, "第一代 executor 必须被 cleanup 释放"

    # 工具调用路径经 getter 自愈重建出第二代
    second = stdio._get_tool_executor()
    assert second is not first
    assert not second._shutdown

    # 异代再次 cleanup：必须清理第二代（旧布尔实现在此短路 → 红灯）
    stdio.cleanup_resources()
    assert second._shutdown, (
        "B23：自愈重建后的第二代 executor 必须被再次 cleanup 释放"
    )

    # 第三代自愈后同代重复 cleanup：幂等且无残留
    third = stdio._get_tool_executor()
    stdio.cleanup_resources()
    stdio.cleanup_resources()
    assert third._shutdown
