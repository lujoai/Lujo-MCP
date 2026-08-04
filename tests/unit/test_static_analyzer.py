"""M3 StaticAnalyzer 静态分析单元测试。

覆盖 `analyze()` 堆栈帧分析入口：
- 空帧 / 单帧 / 多帧调用链
- 行号匹配 / 无效帧跳过 / 缺失文件降级
- 函数签名、内部调用、可疑输入推断

说明：无堆栈场景的 `analyze_handler()` 入口测试见 `test_url_resolver.py`。
"""

from __future__ import annotations

import os

import pytest

from app.mcp.collectors.static_analyzer import analyze


def _write_source(tmp_path: pytest.TempPathFactory, source: str) -> str:
    path = os.path.join(str(tmp_path), "module.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(source)
    return path


_SOURCE = """\
from typing import Optional

def helper():
    return 1

def get_user(user_id):
    return db.get(user_id)

def process(user_id: Optional[int] = None):
    data = get_user(user_id)
    for item in data:
        if item is None:
            break
    return data
"""


def test_analyze_empty_frames_returns_empty_list():
    assert analyze([]) == []


def test_analyze_frame_line_hit_returns_fault_location(tmp_path):
    path = _write_source(tmp_path, _SOURCE)
    frames = [
        {"file": path, "function": "get_user", "line": 6},
    ]
    results = analyze(frames)
    assert len(results) == 1
    loc = results[0]
    assert loc.function == "get_user"
    assert loc.line_number == 6
    assert loc.function_info is not None
    assert loc.function_info.name == "get_user"


def test_analyze_multiple_frames_builds_call_chain(tmp_path):
    path = _write_source(tmp_path, _SOURCE)
    frames = [
        {"file": path, "function": "process", "line": 9},
        {"file": path, "function": "get_user", "line": 6},
    ]
    results = analyze(frames)
    assert len(results) == 2
    assert results[0].call_chain == ["process", "get_user"]


def test_analyze_frame_skips_invalid_frame(tmp_path):
    path = _write_source(tmp_path, _SOURCE)
    frames = [
        {"file": "", "function": "", "line": 0},
        {"file": path, "function": "helper", "line": 3},
    ]
    results = analyze(frames)
    assert len(results) == 1
    assert results[0].function == "helper"


def test_analyze_missing_file_returns_empty(tmp_path):
    frames = [
        {"file": os.path.join(str(tmp_path), "does_not_exist.py"), "function": "x", "line": 1},
    ]
    assert analyze(frames) == []


def test_analyze_extracts_internal_calls(tmp_path):
    path = _write_source(tmp_path, _SOURCE)
    frames = [{"file": path, "function": "process", "line": 9}]
    results = analyze(frames)
    assert len(results) == 1
    calls = results[0].function_info.internal_calls
    assert "get_user" in calls


def test_analyze_infers_suspicious_input(tmp_path):
    src = (
        "def render(user_id: Optional[str]):\n"
        "    return user_id.upper()\n"
    )
    path = _write_source(tmp_path, src)
    frames = [{"file": path, "function": "render", "line": 1}]
    results = analyze(frames)
    assert len(results) == 1
    assert any(
        s["variable"] == "user_id" for s in results[0].suspicious_inputs
    )


def test_frame_missing_function_returns_none(tmp_path):
    path = _write_source(tmp_path, _SOURCE)
    frames = [{"file": path, "function": "not_defined", "line": 1}]
    assert analyze(frames) == []