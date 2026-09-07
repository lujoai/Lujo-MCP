"""C2（重型工具进程隔离）回归测试。

覆盖：
- verify_ui_prepare_args：父进程把 spec_id 预解析为 spec（子进程读不到内存态 spec_store）。
- is_heavy_tool / _get_tool_executor_and_slots：重活判定与「重活不再用线程池」。
- run_heavy_tool_blocking：子进程执行成功回传、异常结构化回传、超时强杀（真起轻量
  子进程，不起浏览器；浏览器路径另有冒烟脚本验证）。
- R6：大结果（超过管道缓冲）在子进程运行期间读取，不再先 join 后 recv 死锁误判
  超时；超时与成功路径均无残留子进程。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.mcp.protocol import heavy_process
from app.mcp.protocol.heavy_process import run_heavy_tool_blocking

_SELFTEST = "app.mcp.protocol._heavy_selftest"


# ── verify_ui_prepare_args：spec_id → spec 父进程预解析 ──


class TestVerifyUiPrepareArgs:
    def test_resolves_spec_id_to_spec(self, monkeypatch):
        from app.mcp.tools import verify_ui_api
        from app.runtime.verifier import spec_store

        spec = {"kind": "ui", "target": "https://example.com", "expect": {}}
        monkeypatch.setattr(spec_store, "get", lambda sid: spec if sid == "s-1" else None)

        prepared = verify_ui_api.verify_ui_prepare_args({"spec_id": "s-1", "timeout_ms": 5000})
        assert prepared["spec"] == spec
        assert prepared["spec_id"] == "s-1"  # 保留原键，不破坏入参

    def test_spec_present_returns_unchanged(self, monkeypatch):
        from app.mcp.tools import verify_ui_api
        from app.runtime.verifier import spec_store

        called = {"n": 0}

        def _boom(sid):
            called["n"] += 1
            return None

        monkeypatch.setattr(spec_store, "get", _boom)
        args = {"spec": {"kind": "ui"}}
        assert verify_ui_api.verify_ui_prepare_args(args) is args
        assert called["n"] == 0  # spec 已在，不应再查 spec_store

    def test_spec_not_found_returns_unchanged(self, monkeypatch):
        from app.mcp.tools import verify_ui_api
        from app.runtime.verifier import spec_store

        monkeypatch.setattr(spec_store, "get", lambda sid: None)
        args = {"spec_id": "missing"}
        assert verify_ui_api.verify_ui_prepare_args(args) == args


# ── 重活判定与派发目标 ──


class TestHeavyRouting:
    def test_is_heavy_tool(self):
        from app.mcp.protocol.server import is_heavy_tool

        assert is_heavy_tool("verify_ui") is True
        assert is_heavy_tool("auto_test") is True
        assert is_heavy_tool("context") is False

    def test_heavy_has_no_thread_executor(self):
        """C2：重活不再用重型线程池，仅保留信号量门控（执行走子进程）。"""
        from app.mcp.protocol.server import _get_tool_executor_and_slots

        executor, slots, pool_type = _get_tool_executor_and_slots("verify_ui")
        assert pool_type == "heavy"
        assert executor is None
        assert slots is not None

        light_executor, _, light_type = _get_tool_executor_and_slots("context")
        assert light_type == "light"
        assert light_executor is not None


# ── run_heavy_tool_blocking：子进程执行 / 异常 / 超时强杀（真起轻量子进程）──


class TestRunHeavyToolBlocking:
    def test_success_returns_result(self):
        result = run_heavy_tool_blocking(_SELFTEST, "quick_ok", {"x": 42}, timeout=60)
        assert result == {"ok": True, "echo": 42}

    def test_handler_exception_propagates_as_runtime_error(self):
        with pytest.raises(RuntimeError, match="intentional failure"):
            run_heavy_tool_blocking(_SELFTEST, "boom", {}, timeout=60)

    def test_timeout_kills_subprocess(self):
        """超时抛 TimeoutError 且整体耗时受限（子进程已被 terminate，不会干等 30s）。"""
        start = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            run_heavy_tool_blocking(_SELFTEST, "slow_hang", {"sleep": 30}, timeout=2)
        elapsed = time.monotonic() - start
        # 超时 2s + 强杀宽限，远小于子进程本应的 30s
        assert elapsed < 20


class TestHeavyLargeResultNoDeadlock:
    """FIX: R6 —— 大结果不再因「先 join 后 recv」的相互等待被误判超时强杀。"""

    def test_big_result_beyond_pipe_buffer_returns(self):
        """1 MiB（> 管道缓冲）结果正常回传；业务计算早已完成即应在时限内返回。"""
        size = 1024 * 1024
        start = time.monotonic()
        result = run_heavy_tool_blocking(_SELFTEST, "big_result", {"size": size}, timeout=15)
        elapsed = time.monotonic() - start
        assert result["ok"] is True
        assert len(result["echo"]) == size
        # 结果写回只需远小于 timeout 的时间（修复前 1 MiB 即触发 TimeoutError）
        assert elapsed < 10

    def test_timeout_semantics_preserved_with_small_deadline(self):
        """截止时间语义保持：无结果产出的挂起子进程 + 极小 timeout 仍按超时强杀。"""
        start = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            run_heavy_tool_blocking(_SELFTEST, "slow_hang", {"sleep": 30}, timeout=0.5)
        assert time.monotonic() - start < 15

    def test_unserializable_result_returns_structured_error(self):
        """R6 验收：不可序列化结果 → 子进程退化为结构化错误，父进程 RuntimeError 而非挂死。"""
        with pytest.raises(RuntimeError, match="not serializable"):
            run_heavy_tool_blocking(_SELFTEST, "unserializable_result", {}, timeout=15)


@pytest.mark.asyncio
async def test_run_heavy_tool_blocking_usable_from_event_loop():
    """模拟派发路径：事件循环里经 run_in_executor 调用，不阻塞且可拿到结果。"""
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, run_heavy_tool_blocking, _SELFTEST, "quick_ok", {"x": 7}, 60.0
    )
    assert result == {"ok": True, "echo": 7}
