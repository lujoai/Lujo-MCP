"""集成测试：端到端调试流程"""


class TestDebugFlow:

    def test_full_debug_flow(self):
        """模拟从请求到上下文构建的完整流程"""
        from app.runtime.core.logs import create_request_id, add_log, get_logs, delete_logs
        from app.mcp.builders.context import build_context

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

        for i, rid in enumerate(ids):
            trace = get_logs(rid)
            assert len(trace) >= 2
            assert trace[0]["step"] == "start"
            assert trace[-1]["step"] == "end"

        for rid in ids:
            delete_logs(rid)
