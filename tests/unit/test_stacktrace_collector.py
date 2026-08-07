"""单元测试：stacktrace collector"""
from app.runtime.collectors.stacktrace import capture_exception, format_trace_for_ai


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

        assert exc_data["frame_count"] >= 3  # outer → inner → 1/0
        frames = exc_data["frames"]
        # 从内到外，最后一帧是测试函数本身
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
        assert "调用栈:" in text

    def test_no_exception(self):
        exc_data = capture_exception(None)
        assert "error" in exc_data

    def test_capture_exception_masks_sensitive_locals_and_keeps_metadata(self):
        def boom():
            api_key = "sk-secret-123"  # noqa: F841  # 故意留在局部变量，供 capture_exception 捕获帧局部并脱敏
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
