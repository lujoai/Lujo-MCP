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
import contextlib
import json
import pickle
import threading
import time

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams

from app.config import settings
from app.mcp.protocol import heavy_process
from app.mcp.protocol import server as protocol
from app.mcp.protocol.executor_lifecycle import SlotPool
from app.mcp.protocol.jsonrpc import JSONRPCRequest
from app.mcp.protocol.server import _handle_tools_call, _tool_registry, register_tool
from app.mcp.protocol.tool_errors import conclusion_tool_is_failure
from app.mcp.protocol.termination import backend as backend_mod
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


# ── 九、W12：取消/关闭语义与许可记账 ────────────────────────────────────
#
# 覆盖 P1-HEAVY-1（外部取消提前归还 heavy 许可 → 并发上限被击穿）、
# P1-HEAVY-2 / P3-HEAVY-5（closing 被错标 TOOL_TIMEOUT + 虚构「>60s」文案）、
# P2-HEAVY-1（GO 提交闸门是恒 True 的空壳、aborted 无生产消费者）、
# P3-HEAVY-2（spawn 成功后、try/finally 之前的异常使子进程脱离回收）、
# P3-HEAVY-4（埋点异常发生在「已取许可、未登记 token」窗口 → 许可永久丢失）。


async def _call(transport: str, name: str, arguments: dict):
    """按传输发起调用，统一返回 (协议级错误, 结果载体)。"""
    if transport == "http":
        resp = await _http_call(name, arguments)
        return resp.get("error"), resp.get("result", {})
    return None, await _stdio_call(name, arguments)


def _error_code_of(transport: str, result) -> str | None:
    if transport == "http":
        return result.get("error_code")
    return _stdio_payload(result).get("error_code")


def _text_of(transport: str, result) -> str:
    if transport == "http":
        return result["content"][0]["text"]
    return _stdio_payload(result).get("error", "")


def _install_blocking_heavy(monkeypatch, started, release):
    """把两个传输的 heavy 派发目标换成可控阻塞替身（不拉子进程）。

    patch 靶点是**被测模块自己的绑定**（server / mcp_server 各自
    ``from ... import run_heavy_tool_blocking``），patch 上游模块无效。
    """
    import app.mcp_server as stdio

    calls: list = []

    def _blocking_heavy(handler_module, handler_name, arguments, timeout):
        calls.append((handler_module, handler_name, arguments, timeout))
        started.set()
        if not release.wait(30.0):
            raise AssertionError("测试自身超时：收割线程未被放行")
        return {"ok": True}

    monkeypatch.setattr(protocol, "run_heavy_tool_blocking", _blocking_heavy)
    monkeypatch.setattr(stdio, "run_heavy_tool_blocking", _blocking_heavy)
    return calls


async def _wait_until(predicate, loop, timeout_s: float = 10.0) -> bool:
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


class TestHeavyCancellationKeepsSlotUntilReaperFinishes:
    """P1-HEAVY-1：外部取消不得提前归还 heavy 许可。

    收割线程最长要跑 tool_timeout + 终止宽限；许可一旦提前归还，
    取消+重试风暴下容量=2 的门控形同不存在，浏览器子进程无界堆积。
    """

    @pytest.mark.parametrize("transport", ["http", "stdio"])
    @pytest.mark.asyncio
    async def test_cancel_while_reaper_running_holds_slot(self, monkeypatch, transport):
        started = threading.Event()
        release = threading.Event()
        _install_blocking_heavy(monkeypatch, started, release)

        register_tool(
            "heavy_cancel_slot",
            description="heavy tool blocked for cancellation accounting",
            handler=handlers.echo,
            inputSchema={"type": "object"},
            heavy=True,
        )

        pool = protocol._heavy_pool
        before = pool.semaphore._value
        loop = asyncio.get_running_loop()
        coro = (
            _http_call("heavy_cancel_slot", {})
            if transport == "http"
            else _stdio_call("heavy_cancel_slot", {})
        )
        call = loop.create_task(coro)

        try:
            await loop.run_in_executor(None, started.wait, 10.0)
            assert started.is_set(), "收割线程未启动，无法验证取消语义"
            assert pool.counters().P == 1, "取得许可后必须登记 ACTIVE token"

            call.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await call

            # 核心断言：取消之后许可仍被占用（收割线程还在跑）
            assert pool.counters().P == 1, "外部取消提前结算了许可（P1-HEAVY-1）"
            assert pool.semaphore._value == before - 1, "许可被提前归还，并发上限可被击穿"
        finally:
            release.set()

        assert await _wait_until(lambda: pool.semaphore._value == before, loop), (
            "收割线程真实结束后许可必须归还"
        )
        assert pool.counters().P == 0
        assert pool.counters().R == 1
        pool.assert_conservation()


class TestHeavyClosingIsToolBusyNotTimeout:
    """P1-HEAVY-2 / P3-HEAVY-5：关闭中拒绝必须按 TOOL_BUSY fast-fail。

    closing 路径从未消耗任何超时预算，把它映射成 TOOL_TIMEOUT + 「>60s」
    既骗了调用方，也把指标记成 timeout。
    """

    @pytest.mark.parametrize("transport", ["http", "stdio"])
    @pytest.mark.asyncio
    async def test_closing_rejection_maps_to_tool_busy(self, monkeypatch, transport):
        import app.mcp_server as stdio

        events: list = []
        monkeypatch.setattr(protocol, "record_mcp_tool_call", _make_spy(events))
        monkeypatch.setattr(stdio, "record_mcp_tool_call", _make_spy(events))

        spawned: list = []

        def _spy_spawn(*args, **kwargs):
            spawned.append(1)
            raise AssertionError("closing 后绝不应拉起子进程")

        monkeypatch.setattr(
            heavy_process.termination_backend, "spawn_with_backend", _spy_spawn
        )

        register_tool(
            "heavy_closing",
            description="heavy tool rejected while closing",
            handler=handlers.echo,
            inputSchema={"type": "object"},
            heavy=True,
        )

        # 生产由 M1 ①（begin_close）触发；这里直接置 closing 复现「关闭后被调用」
        protocol._heavy_pool.begin_close()

        _proto_error, result = await _call(transport, "heavy_closing", {})
        assert result.get("isError") is True
        assert _error_code_of(transport, result) == "TOOL_BUSY", (
            "closing 被错标（P1-HEAVY-2），实际=%r" % _error_code_of(transport, result)
        )
        if transport == "http":
            assert result["_busy"] is True
            assert result.get("_timed_out") is not True
        else:
            payload = _stdio_payload(result)
            assert payload["_busy"] is True
            assert payload.get("_timed_out") is not True
        assert "超时" not in _text_of(transport, result), (
            "虚构的超时文案仍在: %r" % _text_of(transport, result)
        )
        assert spawned == [], "closing 后仍尝试 spawn"
        assert [e[1] for e in events] == ["busy"], (
            "指标必须记 busy 而不是 timeout: %r" % events
        )


class _StubProbe:
    """能力探测替身：不拉探测子进程（单元层确定性）。"""

    def ensure_probed(self, *, gate=None, deadline=None):
        return object()


class _FakeProc:
    def __init__(self, pid: int = 41000):
        self.pid = pid
        self.returncode = None
        self.stdin = None

    def poll(self):
        return self.returncode


def _install_fake_spawn(monkeypatch, *, aborted: bool = False):
    """替换 probe/spawn/terminate/handshake，返回可断言的记录器。"""
    attempt = backend_mod._ExternalAttempt(_FakeProc(), 0)
    decision = backend_mod.AttemptBackend(
        "direct-child", None, console_reachable=False, aborted=aborted
    )
    rec = {
        "attempt": attempt,
        "decision": decision,
        "terminated": [],
        "handshake": [],
        "allow_commit": None,
        "result": pickle.dumps(("ok", {"done": True})),
    }

    def _fake_spawn(attempt_id, command, **kwargs):
        return attempt, decision

    def _fake_terminate(att, dec, **kwargs):
        rec["terminated"].append(att)
        return 0

    def _fake_handshake(att, request, deadline, allow_commit):
        rec["handshake"].append(att)
        rec["allow_commit"] = allow_commit
        return rec["result"]

    monkeypatch.setattr(heavy_process, "capability_probe", _StubProbe())
    monkeypatch.setattr(
        heavy_process.termination_backend, "spawn_with_backend", _fake_spawn
    )
    monkeypatch.setattr(
        heavy_process.termination_backend, "terminate_attempt", _fake_terminate
    )
    monkeypatch.setattr(heavy_process.heavy_spawn, "handshake", _fake_handshake)
    return rec


class TestGoCommitGateIsWired:
    """P2-HEAVY-1：GO 提交闸门必须真的由 closing/aborted 驱动，不是恒 True。"""

    def test_allow_commit_reflects_closing_gate(self, monkeypatch):
        rec = _install_fake_spawn(monkeypatch)

        assert heavy_process.run_heavy_tool_blocking("m", "h", {}, 5.0) == {"done": True}
        gate = rec["allow_commit"]
        assert gate is not None, "handshake 未收到 allow_commit"
        assert gate() is True, "未 closing 时应当允许提交"

        protocol._heavy_pool.begin_close()
        assert gate() is False, "allow_commit 仍是恒 True 的空壳（P2-HEAVY-1）"
        assert rec["terminated"] == [rec["attempt"]], "在途尝试必须被回收"

    def test_aborted_decision_short_circuits_before_go(self, monkeypatch):
        """换胎闸门拒绝（aborted）的尝试不得写 go，且子进程仍被回收。"""
        rec = _install_fake_spawn(monkeypatch, aborted=True)

        with pytest.raises(heavy_process.HeavyServiceClosing):
            heavy_process.run_heavy_tool_blocking("m", "h", {}, 5.0)

        assert rec["handshake"] == [], "aborted 的尝试绝不应进入握手/写 go"
        assert rec["terminated"] == [rec["attempt"]], "aborted 的子进程必须被终止回收"


class TestSpawnWindowReclaim:
    """P3-HEAVY-2：spawn 成功之后任何异常都不得让子进程脱离回收。"""

    def test_register_failure_still_terminates_child(self, monkeypatch):
        rec = _install_fake_spawn(monkeypatch)

        def _boom(*args, **kwargs):
            raise RuntimeError("registry exploded")

        monkeypatch.setattr(heavy_process._live_attempts, "register", _boom)

        with pytest.raises(RuntimeError, match="registry exploded"):
            heavy_process.run_heavy_tool_blocking("m", "h", {}, 5.0)

        assert rec["terminated"] == [rec["attempt"]], (
            "登记失败时子进程未被终止 → 脱离回收成为孤儿（P3-HEAVY-2）"
        )


class TestMetricFailureDoesNotStrandPermit:
    """P3-HEAVY-4：埋点异常不得把已取得的许可永久带走。"""

    @pytest.mark.parametrize("transport", ["http", "stdio"])
    @pytest.mark.asyncio
    async def test_wait_metric_raise_does_not_leak_slot(self, monkeypatch, transport):
        import app.mcp_server as stdio

        def _boom(*args, **kwargs):
            raise RuntimeError("OTel exporter exploded")

        monkeypatch.setattr(protocol, "record_mcp_tool_wait", _boom)
        monkeypatch.setattr(stdio, "record_mcp_tool_wait", _boom)

        register_tool(
            "light_metric_boom",
            description="light tool used to probe the acquire/token window",
            handler=handlers.echo,
            inputSchema={"type": "object"},
            heavy=False,
        )

        pool = protocol._light_pool
        before = pool.semaphore._value

        proto_error, result = await _call(transport, "light_metric_boom", {})
        # 埋点炸了只能落成工具级失败，不得冒泡成协议级 -32603
        assert proto_error is None, "埋点异常冒泡成了 JSON-RPC 协议错误: %r" % proto_error
        assert result.get("isError") is True

        # 关键不变量：许可必须已归还，否则后续调用恒 TOOL_BUSY。
        # 归还经 call_soon_threadsafe 投递到属主 loop，故按既有超时回收用例的
        # 同一范式有界轮询，而不是当场断言。
        loop = asyncio.get_running_loop()
        assert await _wait_until(lambda: pool.semaphore._value == before, loop), (
            "许可被埋点异常永久带走（P3-HEAVY-4），当前=%s 期望=%s"
            % (pool.semaphore._value, before)
        )

        monkeypatch.setattr(settings, "tool_busy_queue_timeout", 0.0)
        _proto_error2, result2 = await _call(transport, "light_metric_boom", {})
        assert _error_code_of(transport, result2) != "TOOL_BUSY", (
            "第二次调用被恒 TOOL_BUSY 挡掉 → 许可确实泄漏了"
        )
        pool.assert_conservation()
