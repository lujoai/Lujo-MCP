"""执行器生命周期开放项的端到端核实测试（1.0.x 维护工单，2026-09-28）。

背景（CODE_REVIEW §0「待复核/生命周期」+ AGENTS.md §6 执行器子节）：
W12（`2794ca3`）对 P1-HEAVY-2 采纳了「删死契约 + 改正语义」方案——生产不接线
代际重建（retire/start_new_generation 仅测试使用）、closing 后 heavy 一律
`HeavyServiceClosing → TOOL_BUSY`（不误标 TOOL_TIMEOUT）。该语义已有
test_heavy_tool_observability.py::TestHeavyClosingIsToolBusyNotTimeout 双传输锁定。

本文件补上当时留下的覆盖缺口（文档登记的「仍需验证」项）：

1. 轻量执行器（协议层 `_LIGHT_TOOL_EXECUTOR`，HTTP 侧使用）被 shutdown 后，
   **经完整 tools/call dispatch 链路**的下一次轻量工具调用应自愈成功
   （getter 重建线程池），而不是 RuntimeError: cannot schedule new futures
   ——此前只有对 getter 的直接 submit 单测，没有端到端 dispatch 用例。
2. 共享双池 `begin_close`（模拟统一模式任一侧先行关闭）后，heavy 调用保持
   TOOL_BUSY fast-fail（复锁既有语义），且轻量链路仍可自愈服务——即
   「closing 单向不可逆」只影响 heavy，不拖垮轻量路径。
3. stdio 侧私有 `_TOOL_EXECUTOR` 关闭不毒化协议层池（复锁既有事实，
   与 test_mcp_executor.py 同口径，但经 dispatch 链路验证）。
"""
import asyncio

import pytest

from app.mcp.protocol import server as protocol_server
from app.mcp.protocol.jsonrpc import JSONRPCRequest
from app.mcp.protocol.server import _handle_tools_call
from app.mcp.tools import register_all_tools


@pytest.fixture(autouse=True)
def _registered():
    register_all_tools()
    yield
    # 不留下已 shutdown 的模块级 executor 影响后续用例：若当前实例已关闭则
    # 触发一次重建（getter 语义）并保持新实例为干净状态
    ex = protocol_server._LIGHT_TOOL_EXECUTOR
    if getattr(ex, "_shutdown", False):
        protocol_server._get_light_tool_executor()


async def _call_light_tool() -> dict:
    """经协议层完整 dispatch 链路调用一个廉价轻量工具（list_recent_traces）。"""
    req = JSONRPCRequest(
        id="lc-1",
        method="tools/call",
        params={"name": "list_recent_traces", "arguments": {"limit": 1}},
    )
    resp = await _handle_tools_call(req)
    assert resp.get("error") is None, f"协议层报错: {resp.get('error')}"
    return resp


@pytest.mark.asyncio
async def test_light_dispatch_self_heals_after_executor_shutdown():
    """缺口①：HTTP 侧轻量执行器 shutdown 后，下一次 dispatch 调用自愈成功。

    场景来源：AGENTS.md §6「stdio 关闭 _LIGHT_TOOL_EXECUTOR 后 HTTP 路径的
    生命周期/自愈需真实验证」。统一模式下 HTTP lifespan 先于 stdio cleanup
    关闭该池；若进程仍在服务（HTTP 未停或测试复用进程），后续轻量调用必须
    经 `_get_light_tool_executor` 重建线程池并正常完成。
    """
    # 基线：链路正常
    base = await _call_light_tool()
    assert "result" in base
    old_executor = protocol_server._LIGHT_TOOL_EXECUTOR

    # 模拟关闭（与 HTTP lifespan M1⑤ 同一动作）
    old_executor.shutdown(wait=False, cancel_futures=True)
    assert old_executor._shutdown

    # 关键断言：完整 dispatch 链路仍成功（自愈），而非
    # RuntimeError: cannot schedule new futures after shutdown
    healed = await _call_light_tool()
    assert "result" in healed
    new_executor = protocol_server._LIGHT_TOOL_EXECUTOR
    assert new_executor is not old_executor, "getter 未重建线程池"
    assert not new_executor._shutdown


@pytest.mark.asyncio
async def test_light_self_heal_survives_shared_pool_begin_close():
    """缺口②：共享双池 begin_close（closing 单向）后，轻量链路仍自愈可用。

    heavy 的 closing 不可逆是 W12 裁定的设计（TOOL_BUSY fast-fail，由
    test_heavy_tool_observability 锁定）；本用例证明它不连带拖垮轻量路径——
    这正是「HTTP 路径的生命周期/自愈」验证要求的另一半。
    """
    light_pool = protocol_server._light_pool
    heavy_pool = protocol_server._heavy_pool
    was_closing = light_pool.is_closing or heavy_pool.is_closing
    if not was_closing:
        light_pool.begin_close()
        heavy_pool.begin_close()
    try:
        result = await _call_light_tool()
        assert "result" in result
    finally:
        # 恢复现场：按 test_executor_generations 的规范换代路径
        # （retire 关闭当前代 + start_new_generation 开新代），只 retire 不开新代
        # 会让共享池永远停留在 closing，污染同进程后续用例
        if not was_closing:
            light_pool.retire()
            light_pool.start_new_generation()
            heavy_pool.retire()
            heavy_pool.start_new_generation()


@pytest.mark.asyncio
async def test_stdio_pool_shutdown_does_not_poison_protocol_light_dispatch():
    """缺口③：stdio 私有 _TOOL_EXECUTOR 关闭后，协议层轻量 dispatch 不受影响。

    与 test_mcp_executor.py 的直接 submit 验证同口径，这里走完整 dispatch。
    """
    import app.mcp_server as mcp_server

    stdio_executor = mcp_server._TOOL_EXECUTOR
    stdio_executor.shutdown(wait=False, cancel_futures=True)
    assert stdio_executor._shutdown

    result = await _call_light_tool()

    assert "result" in result
    assert protocol_server._LIGHT_TOOL_EXECUTOR is not stdio_executor
    # 复原：让 stdio 侧 getter 在下次使用时自愈（不把 shutdown 实例留给后续用例）
    if mcp_server._TOOL_EXECUTOR._shutdown:
        mcp_server._get_tool_executor()
