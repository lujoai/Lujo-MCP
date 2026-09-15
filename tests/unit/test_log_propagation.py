"""日志双行噪音回归测试。

验证 setup_logging() 后：
1. lujo-mcp logger 的 propagate 为 False（不传播到 root basicConfig handler）；
2. 同一条日志不会同时产生 JSON 和文本两行；
3. stdout 没有日志污染；
4. 重复调用 setup_logging() 不会新增重复 handler。

测试隔离：保存/恢复 lujo-mcp logger 的 handlers、level 和 propagate，
以及 root logger 的 handlers，避免污染其他测试。
"""
from __future__ import annotations

import io
import logging
import sys

import pytest

from app.utils.logging import setup_logging


@pytest.fixture
def _isolated_logging(monkeypatch):
    """隔离 logger 全局状态，测试结束后恢复。"""
    app_logger = logging.getLogger("lujo-mcp")
    saved_handlers = app_logger.handlers[:]
    saved_level = app_logger.level
    saved_propagate = app_logger.propagate

    root_logger = logging.getLogger()
    saved_root_handlers = root_logger.handlers[:]
    saved_root_level = root_logger.level

    monkeypatch.setattr("app.utils.logging.settings.log_format", "json")
    monkeypatch.setattr("app.utils.logging.settings.log_level", "INFO")

    try:
        yield
    finally:
        app_logger.handlers.clear()
        app_logger.handlers.extend(saved_handlers)
        app_logger.setLevel(saved_level)
        app_logger.propagate = saved_propagate

        root_logger.handlers.clear()
        root_logger.handlers.extend(saved_root_handlers)
        root_logger.setLevel(saved_root_level)


def test_propagate_false_after_setup(_isolated_logging):
    """setup_logging() 后 lujo-mcp logger 的 propagate 必须为 False。"""
    app_logger = logging.getLogger("lujo-mcp")
    # 清空已有 handler，模拟首次调用
    app_logger.handlers.clear()

    setup_logging()

    assert app_logger.propagate is False, (
        "lujo-mcp logger 必须设置 propagate=False，否则同一条记录会同时被 "
        "lujo-mcp handler 和 root basicConfig handler 输出，产生双行噪音"
    )


def test_no_duplicate_output(_isolated_logging):
    """同一条日志记录只产生一行输出，不出现 JSON + 文本双行。"""
    app_logger = logging.getLogger("lujo-mcp")
    app_logger.handlers.clear()

    # 模拟 mcp_server.py 的 basicConfig（root logger 上有文本 handler）
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_text_handler = logging.StreamHandler(io.StringIO())
    root_text_handler.setFormatter(
        logging.Formatter("%(levelname)s:%(name)s:%(message)s")
    )
    root_logger.addHandler(root_text_handler)
    root_logger.setLevel(logging.DEBUG)

    setup_logging()

    # 捕获 lujo-mcp logger 的 handler 输出
    lujo_stream = io.StringIO()
    for h in app_logger.handlers:
        h.stream = lujo_stream

    # 重置 root handler 的 stream
    root_text_handler.stream = io.StringIO()

    app_logger.info("test message for dedup")

    lujo_output = lujo_stream.getvalue()
    root_output = root_text_handler.stream.getvalue()

    # lujo-mcp logger 应该有输出（JSON 格式）
    assert "test message for dedup" in lujo_output, "lujo-mcp handler 应输出日志"

    # root logger 不应该收到传播的记录（propagate=False）
    assert "test message for dedup" not in root_output, (
        "root logger 不应收到 lujo-mcp 的传播记录——propagate=False 必须生效"
    )


def test_stdout_not_polluted(_isolated_logging):
    """setup_logging() 在 stdio 模式下日志走 stderr，不污染 stdout。"""
    import os

    app_logger = logging.getLogger("lujo-mcp")
    app_logger.handlers.clear()

    # 模拟 stdio 模式
    os.environ["LUJO_MCP_STDIO_MODE"] = "1"
    try:
        setup_logging()

        # 确认 handler 的 stream 是 stderr
        for h in app_logger.handlers:
            assert h.stream is sys.stderr, (
                f"stdio 模式下 handler stream 必须是 sys.stderr，实际: {h.stream}"
            )
    finally:
        os.environ.pop("LUJO_MCP_STDIO_MODE", None)


def test_no_duplicate_handlers_on_repeated_setup(_isolated_logging):
    """重复调用 setup_logging() 不会新增重复 handler。"""
    app_logger = logging.getLogger("lujo-mcp")
    app_logger.handlers.clear()

    setup_logging()
    count_after_first = len(app_logger.handlers)

    setup_logging()
    count_after_second = len(app_logger.handlers)

    setup_logging()
    count_after_third = len(app_logger.handlers)

    assert count_after_first == 1, f"首次调用后应有 1 个 handler，实际 {count_after_first}"
    assert count_after_second == 1, (
        f"第二次调用后不应新增 handler，实际 {count_after_second}"
    )
    assert count_after_third == 1, (
        f"第三次调用后不应新增 handler，实际 {count_after_third}"
    )
