"""单元测试：stacktrace collector 与框架栈帧智能折叠"""
from app.runtime.collectors.stacktrace import (
    capture_exception,
    format_trace_for_ai,
    is_framework_frame,
    fold_stack_frames,
)


class TestStacktraceCollector:

    def test_capture_zero_division(self):
        try:
            1 / 0
        except ZeroDivisionError as e:
            exc_data = capture_exception(e)

        assert exc_data["type"] == "ZeroDivisionError"
        assert "division by zero" in exc_data["message"]
        assert "frame_count" in exc_data
        assert len(exc_data["frames"]) > 0
        # 验证每一帧的字段
        for frame in exc_data["frames"]:
            assert "file" in frame
            assert "line" in frame
            assert "function" in frame
            assert "code" in frame
            assert "locals" in frame

    def test_nested_exception_capture(self):
        def inner():
            return 1 / 0

        def outer():
            return inner()

        try:
            outer()
        except ZeroDivisionError as e:
            exc_data = capture_exception(e)

        assert exc_data["frame_count"] >= 3  # outer 和 inner 和 1/0
        frames = exc_data["frames"]
        func_names = [f["function"] for f in frames]
        assert "inner" in func_names or "outer" in func_names

    def test_format_trace_for_ai(self):
        try:
            {"key": 1}[  # 故意触发 KeyError
                "nonexistent"
            ]
        except KeyError as e:
            exc_data = capture_exception(e)
            text = format_trace_for_ai(exc_data)

        assert "KeyError" in text
        assert "调用栈" in text

    def test_no_exception(self):
        exc_data = capture_exception(None)
        assert "error" in exc_data

    def test_capture_exception_masks_sensitive_locals_and_keeps_metadata(self):
        def boom():
            api_key = "sk-secret-123"  # noqa: F841
            password = "pw-123"  # noqa: F841
            normal = "hello"  # noqa: F841
            raise RuntimeError("boom")

        try:
            boom()
        except RuntimeError as e:
            exc_data = capture_exception(e, source="global_hook", extra={"origin": "test"})

        assert exc_data["source"] == "global_hook"
        assert exc_data["extra"] == {"origin": "test"}
        frame = next(frame for frame in exc_data["frames"] if frame["function"] == "boom")
        assert frame["locals"]["api_key"] == "***REDACTED***"
        assert frame["locals"]["password"] == "***REDACTED***"
        assert "hello" in frame["locals"]["normal"]

    def test_is_framework_frame_identification(self):
        assert is_framework_frame("/usr/lib/python3.12/site-packages/starlette/routing.py") is True
        assert is_framework_frame("C:\\Python312\\Lib\\site-packages\\uvicorn\\server.py") is True
        assert is_framework_frame("<frozen importlib._bootstrap>") is True
        assert is_framework_frame("<string>") is True
        assert is_framework_frame("") is True
        # 业务项目代码
        assert is_framework_frame("app/services/payment.py") is False
        assert is_framework_frame("src/controllers/order_controller.py") is False

    def test_pythonpath_project_frame_not_misjudged_as_framework(self, monkeypatch):
        """R7-T3 回归：项目根经 PYTHONPATH 进 sys.path（容器 WORKDIR=/ +
        PYTHONPATH=/app 常态）时，项目帧不得被误判为框架帧。

        旧实现用整个 sys.path 前缀判定：/app 在 sys.path 且 realpath != cwd
        → /app/services/orders.py 被判框架 → 折叠 + 丢 [PROJECT CODE] 标记。
        新实现只用 sysconfig 真实 stdlib 路径，对 sys.path 免疫。
        """
        monkeypatch.syspath_prepend("/app")
        assert is_framework_frame("/app/services/orders.py") is False
        assert is_framework_frame("/app/api/routes.py") is False

    def test_real_stdlib_frame_still_detected(self):
        """真实 stdlib 帧（sysconfig stdlib 目录下）仍判为框架帧。"""
        import os
        import sysconfig

        std = sysconfig.get_paths()["stdlib"]
        assert is_framework_frame(os.path.join(std, "json", "__init__.py")) is True

    def test_fold_stack_frames_collapses_consecutive_frameworks(self):
        raw_frames = [
            {"file": "site-packages/uvicorn/main.py", "line": 100, "function": "run"},
            {"file": "site-packages/starlette/routing.py", "line": 200, "function": "app"},
            {"file": "site-packages/anyio/_core/_eventloop.py", "line": 50, "function": "run"},
            {"file": "app/services/order.py", "line": 42, "function": "calculate_total"},
            {"file": "app/utils/math.py", "line": 10, "function": "divide"},
            {"file": "site-packages/pydantic/main.py", "line": 300, "function": "validate"},
        ]

        folded = fold_stack_frames(raw_frames, min_fold_count=2)
        # 前 3 个 uvicorn/starlette/anyio 应该被折叠为 1 个汇总帧
        # 接着 2 个业务项目帧原样保留
        # 最后的 1 个 pydantic 帧作为末尾异常抛出点单独保留（不满足 min_fold_count=2 且为最后帧）
        assert len(folded) == 4
        assert folded[0]["is_folded"] is True
        assert folded[0]["folded_count"] == 3
        assert "starlette" in folded[0]["function"] or "uvicorn" in folded[0]["function"]

        # 业务帧
        assert folded[1]["function"] == "calculate_total"
        assert folded[2]["function"] == "divide"
        assert folded[3]["function"] == "validate"

    def test_format_trace_for_ai_with_folding_labels(self):
        exc_data = {
            "type": "ValueError",
            "message": "invalid quantity",
            "frame_count": 4,
            "frames": [
                {"file": "site-packages/starlette/middleware/base.py", "line": 50, "function": "call_next", "locals": {}},
                {"file": "site-packages/fastapi/routing.py", "line": 150, "function": "run_endpoint", "locals": {}},
                {"file": "app/routes/items.py", "line": 25, "function": "create_item", "code": "calc(x)", "locals": {"x": 0}},
                {"file": "app/services/calc.py", "line": 12, "function": "calc", "code": "raise ValueError()", "locals": {}},
            ]
        }
        text = format_trace_for_ai(exc_data, fold_frameworks=True)
        assert "framework frames folded" in text
        assert "[PROJECT CODE]" in text
        assert "app/routes/items.py:25 in create_item [PROJECT CODE]" in text
