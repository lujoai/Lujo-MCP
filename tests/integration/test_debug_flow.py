"""集成测试：端到端调试流程"""

import pytest


def _assert_two_step_trace(trace: list, index: int) -> None:
    """P3-TEST-5：单个 request_id 的 trace 必须恰好是本用例 setup 写入的两条。

    期望值由用例自身 setup 推出（``test_concurrent_requests`` 里 5 个
    request_id × 每个 2 次 ``add_log``），不是 ``len(trace) >= 2`` 这类
    不可能失败的宽松断言；同时校验每条 data 的 index 归属，使"并发请求
    互不干扰"这一被断言的语义真的可失败（条数、首尾 step、归属三者都锁）。
    """
    assert len(trace) == 2, (
        f"request_id 的 trace 应恰为 setup 写入的 2 条，实际 {len(trace)} 条："
        f"{[e.get('step') for e in trace]}"
    )
    assert [e.get("step") for e in trace] == ["start", "end"], (
        f"首/尾 step 应为 start → end，实际 {[e.get('step') for e in trace]}"
    )
    assert trace[0].get("data") == {"index": index}, (
        f"首条 data 应属于本次请求 index={index}，实际 {trace[0].get('data')}"
    )
    assert trace[1].get("data") == {"index": index}, (
        f"末条 data 应属于本次请求 index={index}，实际 {trace[1].get('data')}"
    )


class TestDebugFlow:

    def test_full_debug_flow(self):
        """模拟从请求到上下文构建的完整流程"""
        from app.runtime.core.logs import create_request_id, add_log, get_logs, delete_logs
        from app.runtime.context.builder import build_context

        request_id = create_request_id()

        # 模拟一个带错误的完整流程
        add_log(request_id, "request_start", {"user_id": 42, "action": "transfer"})
        add_log(request_id, "processing", {"step": "validate"})
        add_log(request_id, "error", "InsufficientBalance: balance is -10")
        add_log(request_id, "processing", {"step": "rollback"})
        add_log(request_id, "response_ready", {"status": "failed", "code": 402})

        trace = get_logs(request_id)
        context = build_context(request_id, trace)

        # 验证追踪
        assert len(trace) == 5
        assert trace[2]["step"] == "error"
        assert trace[2]["data"] == "InsufficientBalance: balance is -10"

        # 验证上下文
        assert context["request_id"] == request_id
        assert context["flow"] == [
            "request_start", "processing", "error", "processing", "response_ready"
        ]
        assert len(context["errors"]) == 1
        assert context["errors"][0] == "InsufficientBalance: balance is -10"
        assert context["output"] == {"status": "failed", "code": 402}

        # 清理
        delete_logs(request_id)

    def test_concurrent_requests(self):
        """验证多个请求的追踪互不干扰"""
        from app.runtime.core.logs import create_request_id, add_log, get_logs, delete_logs

        ids = [create_request_id() for _ in range(5)]

        for i, rid in enumerate(ids):
            add_log(rid, "start", {"index": i})
            add_log(rid, "end", {"index": i})

        # 断言自检（先红后绿）：证明新断面对不合规 trace 确实会失败——
        # ① 多出一条（旧 >= 2 会放行）② index 归属错位（串场）两种形态都必须红
        with pytest.raises(AssertionError):
            _assert_two_step_trace(
                [
                    {"step": "start", "data": {"index": 0}},
                    {"step": "end", "data": {"index": 0}},
                    {"step": "leak", "data": {"index": 1}},
                ],
                0,
            )
        with pytest.raises(AssertionError):
            _assert_two_step_trace(
                [
                    {"step": "start", "data": {"index": 1}},
                    {"step": "end", "data": {"index": 1}},
                ],
                0,
            )

        for i, rid in enumerate(ids):
            _assert_two_step_trace(get_logs(rid), i)

        for rid in ids:
            delete_logs(rid)
