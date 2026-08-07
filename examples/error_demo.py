"""
错误演示脚本 —— 展示如何使用 ai-debug-mcp 捕获和分析错误
"""
import sys
import os

# 添加项目根目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.runtime.core.logs import create_request_id, add_log, get_logs
from app.runtime.context.builder import build_context
from app.runtime.collectors.stacktrace import capture_exception, format_trace_for_ai


def risky_function(x, y):
    """一个可能出错的函数"""
    return x / y


def nested_function():
    """嵌套调用"""
    return risky_function(10, 0)  # 故意触发 ZeroDivisionError


def demo_debug_flow():
    """演示完整的调试流程"""
    request_id = create_request_id()

    # 记录开始
    add_log(request_id, "request_start", {"operation": "demo"})
    add_log(request_id, "processing", {"step": "nested_function"})

    try:
        result = nested_function()
        add_log(request_id, "response_ready", {"result": result})
    except ZeroDivisionError as e:
        # 记录错误
        add_log(request_id, "error", str(e))

        # 捕获详细堆栈
        exc_data = capture_exception(e)

        # 获取追踪
        trace = get_logs(request_id)
        context = build_context(request_id, trace)
        context["exception"] = exc_data

        # 输出分析结果
        print("=" * 60)
        print("[DEBUG] request_id: {}".format(request_id))
        print("=" * 60)
        print("执行流程: {} {}".format(
            " -> ".join(context['flow']),
            "[ERROR]" if context['errors'] else "[OK]"
        ))
        print()

        print("[EXCEPTION] 异常信息:")
        print(format_trace_for_ai(exc_data))
        print()

        print("[CONTEXT] 上下文:")
        import json
        print(json.dumps(context, ensure_ascii=False, indent=2))

    return request_id


if __name__ == "__main__":
    demo_debug_flow()
