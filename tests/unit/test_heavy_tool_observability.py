"""M1-D: 重型工具失败可观测性与错误语义收敛测试。

验证重型子进程工具在全部终端结局下的指标状态、错误码与 MCP isError 语义：
1. 业务失败 vs 工具执行失败的区分（A 类 vs B 类）；
2. 结论型工具（verify_ui 契约：matched=False 仍为 ok 结论，裸 error 才是工具失败）；
3. 子进程非零崩溃退出与不可序列化结果（B 类：TOOL_INTERNAL + error 指标）；
4. 超时强杀生命周期与资源回收（C 类：TOOL_TIMEOUT + timeout 指标 + 槽位/句柄回收）；
5. 并发门控拒绝（D 类：TOOL_BUSY + busy 指标 + 不拉起子进程）；
6. 终端指标唯一性（每次调用恰好一条指标，不重复计数）；
7. 敏感信息隔离（指标与监控标签绝不包含用户载荷、密钥或路径）；
8. HTTP 与 stdio 双传输层口径完全对齐（Parity）。
"""
from __future__ import annotations

import asyncio
import json
import pytest
from mcp.types import CallToolRequest, CallToolRequestParams

from app.config import settings
from app.mcp.protocol import heavy_process
from app.mcp.protocol import server as protocol
from app.mcp.protocol.executor_lifecycle import SlotPool
from app.mcp.protocol.jsonrpc import JSONRPCRequest
from app.mcp.protocol.server import _handle_tools_call, _tool_registry, register_tool
from app.mcp.protocol.tool_errors import conclusion_tool_is_failure
import tests._heavy_entry_handlers as handlers


def _make_spy(events: list) -> callable:
    def record_spy(tool_name, status, duration, events=events):
        events.append((tool_name, status, duration))

    return record_spy


@pytest.fixture(autouse=True)
def _isolated_heavy_environment(monkeypatch):
    """为每个测试注入全新的 SlotPool 并重置注册表，防止跨用例污染。"""
    import app.mcp_server as stdio

    light = SlotPool("light", settings.tool_executor_workers)
    heavy = SlotPool("heavy", settings.tool_heavy_executor_workers)
    monkeypatch.setattr(protocol, "_light_pool", light)
    monkeypatch.setattr(protocol, "_heavy_pool", heavy)
    monkeypatch.setattr(stdio, "_light_pool", light)
    monkeypatch.setattr(stdio, "_heavy_pool", heavy)

    before_registry = dict(_tool_registry)
    yield
    _tool_registry.clear()
    _tool_registry.update(before_registry)


async def _http_call(name: str, arguments: dict) -> dict:
    return await _handle_tools_call(
        JSONRPCRequest(
            id="m1d-http",
            method="tools/call",
            params={"name": name, "arguments": arguments},
        )
    )


async def _stdio_call(name: str, arguments: dict) -> dict:
    import app.mcp_server as stdio

    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )
    result = await stdio.server.request_handlers[CallToolRequest](req)
    return result.model_dump(mode="json")


def _stdio_payload(dumped: dict) -> dict:
    return json.loads(dumped["content"][0]["text"])


# ── 一、A 类：业务层失败 vs B 类：工具执行失败 ──────────────────────────


class TestHeavyBusinessVsExecutionFailure:
    """验证真实重型子进程返回业务失败与未捕获异常的严格区分。"""

    @pytest.mark.parametrize("transport", ["http", "stdio"])
    @pytest.mark.asyncio
    async def test_heavy_business_failure_records_error_metric_and_is_error(
        self, monkeypatch, transport
    ):
        """A 类：子进程正常运行并返回业务失败字典。

        - 指标记录为 error；
        - isError 为 True；
        - 结果保留完整业务错误载荷，不伪装成成功，也不混淆为内部崩溃。
        """
        import app.mcp_server as stdio

        events: list = []
        spy = _make_spy(events)
        monkeypatch.setattr(protocol, "record_mcp_tool_call", spy)
        monkeypatch.setattr(stdio, "record_mcp_tool_call", spy)

        register_tool(
            "heavy_biz_err",
            description="heavy tool returning business error",
            handler=handlers.business_failure,
            inputSchema={"type": "object"},
            heavy=True,
        )

        if transport == "http":
            resp = await _http_call("heavy_biz_err", {})
            assert resp["result"]["isError"] is True
            payload = json.loads(resp["result"]["content"][0]["text"])
        else:
            dumped = await _stdio_call("heavy_biz_err", {})
            assert dumped["isError"] is True
            payload = _stdio_payload(dumped)

        assert payload["error"] == "heavy business failure occurred"
        assert payload["details"] == "invalid business state"
        assert len(events) == 1
        assert events[0] == ("heavy_biz_err", "error", events[0][2])
        assert events[0][2] >= 0

    @pytest.mark.parametrize("transport", ["http", "stdio"])
    @pytest.mark.asyncio
    async def test_heavy_handler_exception_records_internal_error(
        self, monkeypatch, transport
    ):
        """B 类：子进程内 handler 抛出未捕获异常。

        - 子进程将异常结构化为 error 回传；
        - 父进程捕获为工具执行失败；
        - 指标记录为 error；
        - HTTP 明确返回 TOOL_INTERNAL，stdio 标记 isError=True。
        """
        import app.mcp_server as stdio

        events: list = []
        spy = _make_spy(events)
        monkeypatch.setattr(protocol, "record_mcp_tool_call", spy)
        monkeypatch.setattr(stdio, "record_mcp_tool_call", spy)

        register_tool(
            "heavy_exception",
            description="heavy tool throwing exception",
            handler=handlers.boom,
            inputSchema={"type": "object"},
            heavy=True,
        )

        if transport == "http":
            resp = await _http_call("heavy_exception", {})
            assert resp["result"]["isError"] is True
            assert resp["result"]["error_code"] == "TOOL_INTERNAL"
        else:
            dumped = await _stdio_call("heavy_exception", {})
            assert dumped["isError"] is True
            payload = _stdio_payload(dumped)
            assert "Tool execution failed" in payload["error"]

        assert len(events) == 1
        assert events[0] == ("heavy_exception", "error", events[0][2])


# ── 二、结论型工具契约（verify_ui 语义）──────────────────────────────────


class TestHeavyConclusionToolContract:
    """验证重型结论工具的失败判定与指标一致性（不把验证不通过当作工具崩溃）。"""

    @pytest.mark.parametrize("transport", ["http", "stdio"])
    @pytest.mark.asyncio
    async def test_conclusion_failure_is_not_tool_execution_error(
        self, monkeypatch, transport
    ):
        """结论型工具 matched=False 属于有价值的业务结论，不是工具执行失败。

        - isError 必须为 False；
        - 指标必须记录为 ok；
        - 包含结论说明及 diff 信息。
        """
        import app.mcp_server as stdio

        events: list = []
        spy = _make_spy(events)
        monkeypatch.setattr(protocol, "record_mcp_tool_call", spy)
        monkeypatch.setattr(stdio, "record_mcp_tool_call", spy)

        register_tool(
            "heavy_verify_mismatch",
            description="heavy conclusion tool with matched=False",
            handler=handlers.conclusion_failure,
            inputSchema={"type": "object"},
            heavy=True,
            is_failure=conclusion_tool_is_failure,
        )

        if transport == "http":
            resp = await _http_call("heavy_verify_mismatch", {})
            assert resp["result"]["isError"] is False
            payload = json.loads(resp["result"]["content"][0]["text"])
        else:
            dumped = await _stdio_call("heavy_verify_mismatch", {})
            assert dumped["isError"] is False
            payload = _stdio_payload(dumped)

        assert payload["matched"] is False
        assert payload["diffs"] == [{"field": "status", "expected": 200, "actual": 500}]
        assert "spec mismatch" in payload["error"]

        assert len(events) == 1
        assert events[0] == ("heavy_verify_mismatch", "ok", events[0][2])

    @pytest.mark.parametrize("transport", ["http", "stdio"])
    @pytest.mark.asyncio
    async def test_conclusion_bare_error_is_tool_failure(
        self, monkeypatch, transport
    ):
        """结论型工具返回裸 error 字典（无 matched 键）时，仍按工具失败处理。"""
        import app.mcp_server as stdio

        events: list = []
        spy = _make_spy(events)
        monkeypatch.setattr(protocol, "record_mcp_tool_call", spy)
        monkeypatch.setattr(stdio, "record_mcp_tool_call", spy)

        register_tool(
            "heavy_verify_bare_err",
            description="heavy conclusion tool with bare error",
            handler=handlers.conclusion_bare_error,
            inputSchema={"type": "object"},
            heavy=True,
            is_failure=conclusion_tool_is_failure,
        )

        if transport == "http":
            resp = await _http_call("heavy_verify_bare_err", {})
            assert resp["result"]["isError"] is True
            payload = json.loads(resp["result"]["content"][0]["text"])
        else:
            dumped = await _stdio_call("heavy_verify_bare_err", {})
            assert dumped["isError"] is True
            payload = _stdio_payload(dumped)

        assert payload["error"] == "bare error without matched key"
        assert len(events) == 1
        assert events[0] == ("heavy_verify_bare_err", "error", events[0][2])


# ── 三、B 类：子进程非零退出与不可序列化结果 ────────────────────────────


class TestHeavySubprocessCrashAndSerializationFailure:
    """验证子进程非零退出、段错误/崩溃及序列化失败能被正确分类为 TOOL_INTERNAL。"""

    @pytest.mark.parametrize("transport", ["http", "stdio"])
    @pytest.mark.asyncio
    async def test_subprocess_crash_exit_classified_as_error(
        self, monkeypatch, transport
    ):
        """子进程非零退出（exitcode=42）→ 正确分类为 error 指标与 TOOL_INTERNAL。"""
        import app.mcp_server as stdio

        events: list = []
        spy = _make_spy(events)
        monkeypatch.setattr(protocol, "record_mcp_tool_call", spy)
        monkeypatch.setattr(stdio, "record_mcp_tool_call", spy)

        register_tool(
            "heavy_crash",
            description="heavy tool crashing with non-zero exitcode",
            handler=handlers.crash_exit,
            inputSchema={"type": "object"},
            heavy=True,
        )

        if transport == "http":
            resp = await _http_call("heavy_crash", {})
            assert resp["result"]["isError"] is True
            assert resp["result"]["error_code"] == "TOOL_INTERNAL"
        else:
            dumped = await _stdio_call("heavy_crash", {})
            assert dumped["isError"] is True

        assert len(events) == 1
        assert events[0] == ("heavy_crash", "error", events[0][2])

    @pytest.mark.parametrize("transport", ["http", "stdio"])
    @pytest.mark.asyncio
    async def test_unserializable_result_classified_as_error(
        self, monkeypatch, transport
    ):
        """不可 pickle 结果 → 正确分类为 error 指标，不挂死进程。"""
        import app.mcp_server as stdio

        events: list = []
        spy = _make_spy(events)
        monkeypatch.setattr(protocol, "record_mcp_tool_call", spy)
        monkeypatch.setattr(stdio, "record_mcp_tool_call", spy)

        register_tool(
            "heavy_unserializable",
            description="heavy tool returning unserializable object",
            handler=handlers.unserializable,
            inputSchema={"type": "object"},
            heavy=True,
        )

        if transport == "http":
            resp = await _http_call("heavy_unserializable", {})
            assert resp["result"]["isError"] is True
            assert resp["result"]["error_code"] == "TOOL_INTERNAL"
        else:
            dumped = await _stdio_call("heavy_unserializable", {})
            assert dumped["isError"] is True

        assert len(events) == 1
        assert events[0] == ("heavy_unserializable", "error", events[0][2])


# ── 四、C 类：超时强杀与生命周期资源回收 ────────────────────────────────


class TestHeavyTimeoutAndResourceReclamation:
    """验证重型工具调用超时、强杀、指标分类与槽位/进程回收。"""

    @pytest.mark.parametrize("transport", ["http", "stdio"])
    @pytest.mark.asyncio
    async def test_heavy_timeout_records_timeout_metric_and_reclaims_slot(
        self, monkeypatch, transport
    ):
        """子进程长时间阻塞 → 超时强杀，指标唯一记录 timeout，槽位与在途注销。"""
        import app.mcp_server as stdio

        events: list = []
        spy = _make_spy(events)
        monkeypatch.setattr(protocol, "record_mcp_tool_call", spy)
        monkeypatch.setattr(stdio, "record_mcp_tool_call", spy)

        register_tool(
            "heavy_timeout_tool",
            description="heavy tool timing out",
            handler=handlers.slow_hang,
            inputSchema={"type": "object"},
            heavy=True,
        )

        # 调低超时阈值以加快测试（0.3s）
        monkeypatch.setattr(settings, "tool_timeout_seconds", 0.3)

        initial_slot_value = protocol._heavy_pool.semaphore._value
        assert initial_slot_value > 0

        if transport == "http":
            resp = await _http_call("heavy_timeout_tool", {"sleep": 10.0})
            assert resp["result"]["isError"] is True
            assert resp["result"]["error_code"] == "TOOL_TIMEOUT"
            assert resp["result"]["_timed_out"] is True
        else:
            dumped = await _stdio_call("heavy_timeout_tool", {"sleep": 10.0})
            assert dumped["isError"] is True
            payload = _stdio_payload(dumped)
            assert payload["_timed_out"] is True

        # 指标断言：唯一记录且状态为 timeout（非 error，非 ok）
        assert len(events) == 1
        assert events[0] == ("heavy_timeout_tool", "timeout", events[0][2])

        # 资源回收断言：重型槽位已归还，在途尝试注册表已清空
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while loop.time() < deadline:
            if protocol._heavy_pool.semaphore._value == initial_slot_value:
                break
            await asyncio.sleep(0.02)
        assert protocol._heavy_pool.semaphore._value == initial_slot_value
        assert len(heavy_process._live_attempts.attempts) == 0


# ── 五、D 类：并发门控拒绝（TOOL_BUSY）──────────────────────────────────


class TestHeavyConcurrencyGatingBusy:
    """验证重型槽位打满时 fast-fail，指标独立记录 busy，不拉起子进程。"""

    @pytest.mark.parametrize("transport", ["http", "stdio"])
    @pytest.mark.asyncio
    async def test_heavy_busy_fast_fail_records_busy_and_no_subprocess(
        self, monkeypatch, transport
    ):
        import app.mcp_server as stdio

        call_events: list = []
        busy_events: list = []
        monkeypatch.setattr(protocol, "record_mcp_tool_call", _make_spy(call_events))
        monkeypatch.setattr(stdio, "record_mcp_tool_call", _make_spy(call_events))

        def _busy_spy(tool_name, pool_type, wait_sec):
            busy_events.append((tool_name, pool_type, wait_sec))

        monkeypatch.setattr(protocol, "record_mcp_tool_busy", _busy_spy)
        monkeypatch.setattr(stdio, "record_mcp_tool_busy", _busy_spy)

        spawn_called = []

        def _spy_spawn(*args, **kwargs):
            spawn_called.append(1)
            raise AssertionError("当槽位已满时，绝不应尝试拉起子进程")

        monkeypatch.setattr(heavy_process.termination_backend, "spawn_with_backend", _spy_spawn)

        register_tool(
            "heavy_busy_tool",
            description="heavy tool to be rejected by busy gate",
            handler=handlers.business_failure,
            inputSchema={"type": "object"},
            heavy=True,
        )

        monkeypatch.setattr(settings, "tool_busy_queue_timeout", 0.0)

        # 占满所有 heavy 槽位
        free = protocol._heavy_pool.semaphore._value
        held = []
        for _ in range(free):
            await protocol._heavy_pool.semaphore.acquire()
            held.append(1)

        try:
            if transport == "http":
                resp = await _http_call("heavy_busy_tool", {})
                assert resp["result"]["isError"] is True
                assert resp["result"]["error_code"] == "TOOL_BUSY"
                assert resp["result"]["_busy"] is True
            else:
                dumped = await _stdio_call("heavy_busy_tool", {})
                assert dumped["isError"] is True
                payload = _stdio_payload(dumped)
                assert payload["error_code"] == "TOOL_BUSY"
                assert payload["_busy"] is True
        finally:
            for _ in held:
                protocol._heavy_pool.semaphore.release()

        # 确认未拉起任何子进程
        assert len(spawn_called) == 0

        # 指标断言：唯一记录 busy，不被混淆为 error 或 timeout
        assert len(call_events) == 1
        assert call_events[0][0] == "heavy_busy_tool"
        assert call_events[0][1] == "busy"

        assert len(busy_events) == 1
        assert busy_events[0][0] == "heavy_busy_tool"
        assert busy_events[0][1] == "heavy"


# ── 六、敏感信息隔离性（无密钥、路径泄漏）────────────────────────────────


class TestHeavyMetricDataSanitization:
    """验证重型工具在执行成功或包含敏感数据时，指标与日志标签无敏感信息泄漏。"""

    @pytest.mark.asyncio
    async def test_sensitive_payload_not_leaked_into_metric_labels(
        self, monkeypatch
    ):
        """指标标签集合有限且静态，入参或返回体中的 API key、路径不进入指标。"""
        import app.observability as obs
        from app.observability import _counter_lock, _mcp_tool_calls_total

        register_tool(
            "heavy_sensitive_tool",
            description="heavy tool with secrets",
            handler=handlers.sensitive_echo,
            inputSchema={"type": "object"},
            heavy=True,
        )

        with _counter_lock:
            _mcp_tool_calls_total.clear()

        resp = await _http_call("heavy_sensitive_tool", {"client_token": "bearer-12345"})
        assert resp["result"]["isError"] is False

        with _counter_lock:
            recorded_keys = list(_mcp_tool_calls_total.keys())

        # 校验指标维度只有 (tool_name, status)
        for tool_name, status in recorded_keys:
            assert tool_name == "heavy_sensitive_tool"
            assert status == "ok"
            assert "bearer-12345" not in tool_name
            assert "sk-secret-token" not in tool_name
            assert "confidential" not in tool_name

        rendered = obs._render_prometheus()
        assert "sk-secret-token" not in rendered
        assert "confidential" not in rendered
        assert "bearer-12345" not in rendered
