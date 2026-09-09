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
