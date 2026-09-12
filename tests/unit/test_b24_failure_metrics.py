"""B24 回归测试：业务失败结果的指标状态与 isError 由同一次 predicate 判定派生。

原实现（B24 缺陷，报告 #22）：``app/mcp/protocol/server.py:476``（async-light）、
``app/mcp/protocol/server.py:579``（sync/heavy）、``app/mcp_server.py:551``
（stdio call_tool）在求值 ``tool_failure_predicate(tool)(result)`` **之前**
就执行 ``record_mcp_tool_call(..., "ok", ...)``——handler 返回
``{"error": "business failure"}`` 时响应 ``isError=true``，但指标已错误记为 ok。

新实现：先求值**同一个** tool_failure_predicate，按同一布尔值**只记录一次**
``error`` / ``ok`` 指标，再构造 HTTP 响应（isError）或抛出 stdio
``ToolExecutionError``。

观测纪律（本文件所有用例）：通过 monkeypatch spy 检查
``record_mcp_tool_call`` 的调用参数（tool_name / status / duration），断言：

- ``len(events) == 1``（每次工具调用只记录一次指标）；
- 业务失败 ``events[0][1] == "error"``；业务成功 ``events[0][1] == "ok"``；
- tool_name 正确、duration 非负；
- 不允许「先 ok 后 error」或「error 后再次 ok」；不断言只靠最终 isError；
- 不依赖日志文本推断指标状态。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams

from app.config import settings
from app.mcp.protocol import server as protocol
from app.mcp.protocol.jsonrpc import JSONRPCRequest
from app.mcp.protocol.server import _handle_tools_call, _tool_registry, register_tool
from app.mcp.protocol.tool_errors import ToolExecutionError, conclusion_tool_is_failure


def _make_spy(events: list) -> callable:
    def record_spy(tool_name, status, duration, events=events):
        events.append((tool_name, status, duration))

    return record_spy


@pytest.fixture(autouse=True)
def _fresh_pools(monkeypatch):
    """隔离池状态：W4-4 后 cleanup_resources 会对共享池 begin_close
    （生产正确语义：进程退出后不再接纳）。本文件用例需要未 closing 的双池，
    每用例注入全新 SlotPool，避免同进程内测试顺序污染。"""
    from app.mcp.protocol.executor_lifecycle import SlotPool
    import app.mcp_server as stdio

    light = SlotPool("light", settings.tool_executor_workers)
    heavy = SlotPool("heavy", settings.tool_heavy_executor_workers)
    monkeypatch.setattr(protocol, "_light_pool", light)
    monkeypatch.setattr(protocol, "_heavy_pool", heavy)
    monkeypatch.setattr(stdio, "_light_pool", light)
    monkeypatch.setattr(stdio, "_heavy_pool", heavy)


@pytest.fixture
def _clean_registry():
    """快照恢复注册表；临时工具不得泄漏到其他用例 / tools/list。"""
    before = dict(_tool_registry)
    yield
    _tool_registry.clear()
    _tool_registry.update(before)


async def _http_call(name: str, arguments: dict) -> dict:
    return await _handle_tools_call(
        JSONRPCRequest(
            id="b24-http",
            method="tools/call",
            params={"name": name, "arguments": arguments},
        )
    )


async def _stdio_direct(name: str, arguments: dict):
    """直接调用 stdio 适配层入口（绕过 SDK 包装，异常原样上抛）。"""
    import app.mcp_server as stdio

    return await stdio.call_tool(name, arguments)


async def _stdio_sdk(name: str, arguments: dict) -> dict:
    """经官方 Server request handler 调用（SDK 把 ToolExecutionError 包装为
    CallToolResult(isError=True)）。"""
    import app.mcp_server as stdio

    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )
    result = await stdio.server.request_handlers[CallToolRequest](req)
    return result.model_dump(mode="json")


async def _wait_until(predicate, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


# ── 一、HTTP/协议层 async-light 分支（原 server.py:476 提前 ok 点）────────


class TestHttpAsyncLightBranch:
    @pytest.mark.asyncio
    async def test_business_failure_records_error_once(self, monkeypatch, _clean_registry):
        """async 轻量 handler 返回业务失败字典 → 指标唯一 error。

        原实现行为：async-light 分支在求值 predicate 之前先记 ``("ok")``，
        响应虽 isError=true，指标却错误记为 ok。
        新实现行为：先求值同一 predicate，指标唯一记录 ``("error")``，
        响应 isError=true。
        断言锁住的不变量：指标只记录一次且状态为 error；tool_name 正确、
        duration 非负；不得出现 ok 记录。
        """
        events: list = []

        async def _handler(arguments):
            return {"error": "business failure"}

        register_tool(
            "b24_async_light_err", description="test", handler=_handler,
            inputSchema={"type": "object"}, heavy=False,
        )
        monkeypatch.setattr(protocol, "record_mcp_tool_call", _make_spy(events))
        try:
            resp = await _http_call("b24_async_light_err", {})
        finally:
            _tool_registry.pop("b24_async_light_err", None)

        assert resp["result"]["isError"] is True
        assert len(events) == 1, f"每次调用只允许一条指标记录：{events}"
        assert events[0] == ("b24_async_light_err", "error", events[0][2])
        assert events[0][2] >= 0


# ── 二、HTTP/协议层 sync 分支（原 server.py:579 提前 ok 点）──────────────


class TestHttpSyncBranch:
    @pytest.mark.asyncio
    async def test_business_failure_records_error_once(self, monkeypatch, _clean_registry):
        """sync 非 heavy handler 返回业务失败字典 → 指标唯一 error。

        原实现行为：sync/heavy 分支在求值 predicate 之前先记 ``("ok")``，
        响应虽 isError=true，指标却错误记为 ok。
        新实现行为：先求值同一 predicate，指标唯一记录 ``("error")``，
        响应 isError=true。用例刻意用**测试专用 sync、非 heavy** handler，
        不启动真实重型子进程。
        断言锁住的不变量：指标只记录一次且状态为 error；isError=true。
        """
        events: list = []

        def _handler(arguments):
            return {"error": "business failure"}

        register_tool(
            "b24_sync_err", description="test", handler=_handler,
            inputSchema={"type": "object"}, heavy=False,
        )
        monkeypatch.setattr(protocol, "record_mcp_tool_call", _make_spy(events))
        try:
            resp = await _http_call("b24_sync_err", {})
        finally:
            _tool_registry.pop("b24_sync_err", None)

        assert resp["result"]["isError"] is True
        assert len(events) == 1, f"每次调用只允许一条指标记录：{events}"
        assert events[0][0] == "b24_sync_err"
        assert events[0][1] == "error"
        assert events[0][2] >= 0


# ── 三、成功结果（HTTP 与 stdio）────────────────────────────────────────


class TestSuccessRecordsOkOnce:
    @pytest.mark.asyncio
    async def test_http_success_records_ok_once(self, monkeypatch, _clean_registry):
        """HTTP 成功结果 ``{"ok": True}`` → 指标唯一 ok，isError=false。

        原实现行为：成功路径记一次 ok（行为正确）。
        新实现行为：谓词求值后仍只记一次 ok。
        断言锁住的不变量：成功结果不产生 error 记录；只有一条 ok。
        """
        events: list = []

        def _handler(arguments):
            return {"ok": True}

        register_tool(
            "b24_http_ok", description="test", handler=_handler,
            inputSchema={"type": "object"},
        )
        monkeypatch.setattr(protocol, "record_mcp_tool_call", _make_spy(events))
        try:
            resp = await _http_call("b24_http_ok", {})
        finally:
            _tool_registry.pop("b24_http_ok", None)

        assert resp["result"]["isError"] is False
        assert len(events) == 1, f"每次调用只允许一条指标记录：{events}"
        assert events[0] == ("b24_http_ok", "ok", events[0][2])
        assert events[0][2] >= 0

    @pytest.mark.asyncio
    async def test_stdio_direct_success_records_ok_once(self, monkeypatch, _clean_registry):
        """stdio 直接 call_tool 成功 -> 指标唯一 ok，不抛异常。

        原实现行为：成功路径记一次 ok（行为正确）。
        新实现行为：谓词求值后仍只记一次 ok。
        断言锁住的不变量：成功结果不产生 error 记录；只有一条 ok。
        """
        import app.mcp_server as stdio_module

        events: list = []

        def _handler(arguments):
            return {"ok": True}

        register_tool(
            "b24_stdio_ok", description="test", handler=_handler,
            inputSchema={"type": "object"},
        )
        monkeypatch.setattr(stdio_module, "record_mcp_tool_call", _make_spy(events))
        try:
            text = await _stdio_direct("b24_stdio_ok", {})
        finally:
            _tool_registry.pop("b24_stdio_ok", None)

        assert json.loads(text[0].text) == {"ok": True}
        assert len(events) == 1, f"每次调用只允许一条指标记录：{events}"
        assert events[0] == ("b24_stdio_ok", "ok", events[0][2])
        assert events[0][2] >= 0


# ── 四、自定义 predicate 返回 True 的真正红灯场景 ────────────────────────


class TestCustomPredicateTrue:
    @pytest.mark.parametrize("transport", ["http", "stdio"], ids=["http", "stdio"])
    @pytest.mark.asyncio
    async def test_predicate_true_metrics_error_and_is_error(
        self, monkeypatch, _clean_registry, transport
    ):
        """自定义 is_failure predicate 对结果返回 True：指标唯一 error。

        原实现行为：handler 返回 ``{"ok": True}``（默认谓词判成功），
        自定义谓词判失败——旧实现先记 ok 指标、再由自定义谓词把响应判为
        isError=true，**指标与 isError 不一致**；该场景只查 isError 无法
        暴露缺陷，必须靠指标 spy 区分（red）。
        新实现行为：先求值自定义谓词，指标唯一记录 ``("error")``，
        响应仍为 isError=true（HTTP）或抛 ToolExecutionError（stdio）。
        断言锁住的不变量：指标只记录一次且状态为 error；谓词判定与
        响应语义一致。
        """
        import app.mcp_server as stdio_module

        events: list = []

        def _handler(arguments):
            return {"ok": True}

        def _always_failure(result):
            return True

        register_tool(
            "b24_custom_pred", description="test", handler=_handler,
            inputSchema={"type": "object"}, is_failure=_always_failure,
        )
        spy = _make_spy(events)
        monkeypatch.setattr(protocol, "record_mcp_tool_call", spy)
        monkeypatch.setattr(stdio_module, "record_mcp_tool_call", spy)
        try:
            if transport == "http":
                resp = await _http_call("b24_custom_pred", {})
                assert resp["result"]["isError"] is True
            else:
                with pytest.raises(ToolExecutionError) as exc_info:
                    await _stdio_direct("b24_custom_pred", {})
                assert json.loads(str(exc_info.value)) == {"ok": True}
        finally:
            _tool_registry.pop("b24_custom_pred", None)

        assert len(events) == 1, f"每次调用只允许一条指标记录：{events}"
        assert events[0][0] == "b24_custom_pred"
        assert events[0][1] == "error"
        assert events[0][2] >= 0

    @pytest.mark.asyncio
    async def test_predicate_evaluated_before_metric_recorded(
        self, monkeypatch, _clean_registry
    ):
        """执行顺序：handler 返回 → predicate → 记指标 → 构造响应。

        原实现行为：``["metric:ok", "predicate"]`` —— 指标先记录，
        谓词后求值（B24 缺陷的时序本质）。
        新实现行为：``["predicate", "metric:error"]`` —— 谓词先求值，
        指标后记录，两者由同一判定派生。
        断言锁住的不变量：同一次调用内 predicate 求值必须先于指标记录，
        且指标状态与谓词判定一致。
        """
        events: list = []
        order: list = []

        def _handler(arguments):
            return {"error": "business failure"}

        def _spy_predicate(result):
            order.append("predicate")
            return True

        def _spy_metric(tool_name, status, duration):
            order.append(f"metric:{status}")
            events.append((tool_name, status, duration))

        register_tool(
            "b24_order", description="test", handler=_handler,
            inputSchema={"type": "object"}, is_failure=_spy_predicate,
        )
        monkeypatch.setattr(protocol, "record_mcp_tool_call", _spy_metric)
        try:
            resp = await _http_call("b24_order", {})
        finally:
            _tool_registry.pop("b24_order", None)

        assert resp["result"]["isError"] is True
        assert len(events) == 1
        assert events[0][1] == "error"
        assert order == ["predicate", "metric:error"], f"执行顺序错误：{order}"


# ── 五、stdio 观察层一：直接 call_tool───────────────────────────────────


class TestStdioDirectCall:
    @pytest.mark.asyncio
    async def test_business_failure_records_error_once(
        self, monkeypatch, _clean_registry
    ):
        """stdio 直接 call_tool：业务失败 → 指标唯一 error + 契约保持。

        原实现行为：``app/mcp_server.py:551`` 在求值谓词之前先记 ok——
        业务失败结果指标错误记为 ok，随后才抛 ToolExecutionError。
        新实现行为：先求值谓词，指标唯一记录 error，再抛
        ToolExecutionError（错误载荷结构不变，SDK 外层转 isError=true）。
        断言锁住的不变量：指标只记录一次且为 error；handler 只执行一次；
        ToolExecutionError 载荷与结果 dict 一致；槽位最终归还。
        """
        import app.mcp_server as stdio_module

        events: list = []
        handler_calls: list = []

        def _handler(arguments):
            handler_calls.append(1)
            return {"error": "business failure"}

        register_tool(
            "b24_stdio_err", description="test", handler=_handler,
            inputSchema={"type": "object"},
        )
        monkeypatch.setattr(stdio_module, "record_mcp_tool_call", _make_spy(events))
        before = protocol._light_pool.semaphore._value
        try:
            with pytest.raises(ToolExecutionError) as exc_info:
                await _stdio_direct("b24_stdio_err", {})
        finally:
            _tool_registry.pop("b24_stdio_err", None)

        assert json.loads(str(exc_info.value)) == {"error": "business failure"}
        assert handler_calls == [1], "业务失败路径 handler 只允许执行一次"
        assert len(events) == 1, f"每次调用只允许一条指标记录：{events}"
        assert events[0][0] == "b24_stdio_err"
        assert events[0][1] == "error"
        assert events[0][2] >= 0
        assert await _wait_until(
            lambda: protocol._light_pool.semaphore._value >= before
        ), "业务失败结束后槽位必须归还"


# ── 六、stdio 观察层二：官方 Server request handler（isError 包装）────────


class TestStdioSdkWrapper:
    @pytest.mark.asyncio
    async def test_business_failure_is_error_true(self, monkeypatch, _clean_registry):
        """经 SDK request handler：业务失败被包装为 isError=true。

        原实现行为：isError=true（包装语义正确，但指标已先记 ok）。
        新实现行为：isError=true 仍保持；指标（有 spy 时）唯一 error。
        断言锁住的不变量：SDK 外层 isError 语义不变；不验证 wire output。
        """
        import app.mcp_server as stdio_module

        events: list = []

        def _handler(arguments):
            return {"error": "business failure"}

        register_tool(
            "b24_sdk_err", description="test", handler=_handler,
            inputSchema={"type": "object"},
        )
        monkeypatch.setattr(stdio_module, "record_mcp_tool_call", _make_spy(events))
        try:
            dumped = await _stdio_sdk("b24_sdk_err", {})
        finally:
            _tool_registry.pop("b24_sdk_err", None)

        assert dumped["isError"] is True
        payload = json.loads(dumped["content"][0]["text"])
        assert payload == {"error": "business failure"}
        assert len(events) == 1
        assert events[0][1] == "error"


# ── 七、结论型工具防回归（不误记 error）──────────────────────────────────


class TestConclusionToolRegression:
    @pytest.mark.asyncio
    async def test_conclusion_payload_not_recorded_as_error(
        self, monkeypatch, _clean_registry
    ):
        """结论型载荷（matched/diffs + error 原因说明）由自定义谓词判成功。

        原实现与新实现行为一致：conclusion_tool_is_failure 把验证结论判为
        成功（isError=false、指标 ok）——该谓词在旧实现下本来就不该改；
        本用例只防回归，**不宣称**它单独证明 B24 主缺陷。
        断言锁住的不变量：结论载荷不记录 error；isError=false。
        """
        events: list = []

        def _handler(arguments):
            return {
                "matched": False,
                "diffs": [],
                "silent_failure": False,
                "error": "must provide spec or spec_id",
            }

        register_tool(
            "b24_conclusion", description="test", handler=_handler,
            inputSchema={"type": "object"}, is_failure=conclusion_tool_is_failure,
        )
        monkeypatch.setattr(protocol, "record_mcp_tool_call", _make_spy(events))
        try:
            resp = await _http_call("b24_conclusion", {})
        finally:
            _tool_registry.pop("b24_conclusion", None)

        assert resp["result"]["isError"] is False
        assert len(events) == 1
        assert events[0][0] == "b24_conclusion"
        assert events[0][1] == "ok"

    @pytest.mark.asyncio
    async def test_found_false_not_recorded_as_error(self, monkeypatch, _clean_registry):
        """found=false 正常无数据结果（无 error 键）→ 不误记 error。

        原实现与新实现行为一致：默认谓词对无 error 键的 dict 判成功。
        断言锁住的不变量：正常无数据结果指标唯一 ok、isError=false。
        """
        events: list = []

        def _handler(arguments):
            return {"found": False, "message": "not found"}

        register_tool(
            "b24_found_false", description="test", handler=_handler,
            inputSchema={"type": "object"},
        )
        monkeypatch.setattr(protocol, "record_mcp_tool_call", _make_spy(events))
        try:
            resp = await _http_call("b24_found_false", {})
        finally:
            _tool_registry.pop("b24_found_false", None)

        assert resp["result"]["isError"] is False
        assert len(events) == 1
        assert events[0][1] == "ok"


# ── 八、既有失败状态保持（exception / timeout / busy / invalid_params）───


class TestExistingFailureSemantics:
    @pytest.mark.asyncio
    async def test_handler_exception_records_error(self, monkeypatch, _clean_registry):
        """handler 异常 → HTTP：TOOL_INTERNAL + 指标 error（保持）。

        原实现与新实现行为一致：exception 分支记 error，不因 B24 改变。
        断言锁住的不变量：异常路径指标唯一 error、isError=true、
        error_code=TOOL_INTERNAL。
        """
        events: list = []

        def _boom(arguments):
            raise RuntimeError("boom")

        register_tool(
            "b24_exc", description="test", handler=_boom,
            inputSchema={"type": "object"},
        )
        monkeypatch.setattr(protocol, "record_mcp_tool_call", _make_spy(events))
        try:
            resp = await _http_call("b24_exc", {})
        finally:
            _tool_registry.pop("b24_exc", None)

        assert resp["result"]["isError"] is True
        assert resp["result"]["error_code"] == "TOOL_INTERNAL"
        assert len(events) == 1
        assert events[0][1] == "error"

    @pytest.mark.asyncio
    async def test_timeout_records_timeout(self, monkeypatch, _clean_registry):
        """超时 → HTTP：TOOL_TIMEOUT + 指标 timeout（保持）。

        原实现与新实现行为一致：timeout 分支记 timeout，不因 B24 改变。
        断言锁住的不变量：超时路径指标唯一 timeout、isError=true、
        error_code=TOOL_TIMEOUT。
        """
        import time as _time

        events: list = []
        monkeypatch.setattr(settings, "tool_timeout_seconds", 0.05)

        def _slow(arguments):
            _time.sleep(1.0)
            return {"ok": True}

        register_tool(
            "b24_to", description="test", handler=_slow,
            inputSchema={"type": "object"},
        )
        monkeypatch.setattr(protocol, "record_mcp_tool_call", _make_spy(events))
        try:
            resp = await _http_call("b24_to", {})
        finally:
            _tool_registry.pop("b24_to", None)

        assert resp["result"]["isError"] is True
        assert resp["result"]["error_code"] == "TOOL_TIMEOUT"
        assert len(events) == 1
        assert events[0][1] == "timeout"

    @pytest.mark.asyncio
    async def test_busy_records_busy(self, monkeypatch, _clean_registry):
        """槽位满 → HTTP：TOOL_BUSY + 指标 busy（保持）。

        原实现与新实现行为一致：busy 分支记 busy，不因 B24 改变。
        断言锁住的不变量：busy 路径指标唯一 busy、isError=true、
        error_code=TOOL_BUSY。
        """

        async def _no_slots(slots, busy_timeout, pool=None):
            return False

        events: list = []
        monkeypatch.setattr(protocol, "_acquire_slot_or_fastfail", _no_slots)

        def _handler(arguments):
            return {"ok": True}

        register_tool(
            "b24_busy", description="test", handler=_handler,
            inputSchema={"type": "object"},
        )
        monkeypatch.setattr(protocol, "record_mcp_tool_call", _make_spy(events))
        try:
            resp = await _http_call("b24_busy", {})
        finally:
            _tool_registry.pop("b24_busy", None)

        assert resp["result"]["isError"] is True
        assert resp["result"]["error_code"] == "TOOL_BUSY"
        assert len(events) == 1
        assert events[0][1] == "busy"

    @pytest.mark.asyncio
    async def test_invalid_params_records_invalid_params(
        self, monkeypatch, _clean_registry
    ):
        """入参校验失败 → HTTP：-32602 + 指标 invalid_params（保持）。

        原实现与新实现行为一致：invalid_params 分支记 invalid_params，
        不因 B24 改变。
        断言锁住的不变量：校验失败路径指标唯一 invalid_params、-32602。
        """
        events: list = []

        def _handler(arguments):
            return {"ok": True}

        register_tool(
            "b24_inv", description="test", handler=_handler,
            inputSchema={
                "type": "object",
                "required": ["need"],
                "properties": {"need": {"type": "string"}},
            },
        )
        monkeypatch.setattr(protocol, "record_mcp_tool_call", _make_spy(events))
        try:
            resp = await _http_call("b24_inv", {})
        finally:
            _tool_registry.pop("b24_inv", None)

        assert resp["error"]["code"] == -32602
        assert len(events) == 1
        assert events[0][1] == "invalid_params"


# ── 九、临时工具清理与注册表隔离 ────────────────────────────────────────


class TestRegistryHygiene:
    def test_temp_tool_cleaned_and_list_unaffected(self, _clean_registry):
        """临时注册工具在 finally 清理后，不得留在注册表 / tools/list。

        原实现与新实现行为一致（测试纪律）。断言锁住的不变量：
        清理后工具不在 _tool_registry；tools/list 不因临时工具改变数量。
        """
        before_count = len(protocol.get_agent_visible_tools())

        def _handler(arguments):
            return {"ok": True}

        register_tool(
            "b24_temp", description="temp", handler=_handler,
            inputSchema={"type": "object"},
        )
        assert "b24_temp" in _tool_registry
        try:
            assert len(protocol.get_agent_visible_tools()) == before_count + 1
        finally:
            _tool_registry.pop("b24_temp", None)

        assert "b24_temp" not in _tool_registry
        assert len(protocol.get_agent_visible_tools()) == before_count