"""M3 StaticAnalyzer 静态分析单元测试。

覆盖 `analyze()` 堆栈帧分析入口：
- 空帧 / 单帧 / 多帧（不再伪造调用链，v0.7.1-b2-7）
- 行号匹配 / 无效帧跳过 / 缺失文件降级 / 超大文件跳过（v0.7.1-b2-8）
- 函数签名、内部调用、可疑输入推断

说明：无堆栈场景的 `analyze_handler()` 入口测试见 `test_url_resolver.py`。
"""

from __future__ import annotations

import os

import pytest

from app.config import settings
from app.runtime.collectors.static_analyzer import analyze


@pytest.fixture(autouse=True)
def _allow_tmp_source_paths(monkeypatch, tmp_path):
    """P0-2 LFI 修复后白名单默认收敛到项目根/CWD；测试在 tmp_path 写源码，
    需把 tmp_path 加入 whitelist_path_prefix 才能被静态分析器读取。"""
    prefix = (settings.whitelist_path_prefix or "").strip()
    roots = [p.strip() for p in prefix.split(",") if p.strip()]
    if not roots:
        roots = [os.path.abspath(os.getcwd())]
    roots.append(str(tmp_path))
    monkeypatch.setattr(settings, "whitelist_path_prefix", ",".join(roots))


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


def test_analyze_multiple_frames_no_fabricated_call_chain(tmp_path):
    path = _write_source(tmp_path, _SOURCE)
    frames = [
        {"file": path, "function": "process", "line": 9},
        {"file": path, "function": "get_user", "line": 6},
    ]
    results = analyze(frames)
    assert len(results) == 2
    # FIX(v0.7.1-b2-7): 旧实现把全部帧函数名列表塞给 results[0].call_chain
    # （"帧函数名序列"≠静态追溯的调用链），下游 fault_localizer 据此给首帧
    # 恒加 +10"call chain hub"虚构证据。现不再伪造填充。
    assert all(loc.call_chain == [] for loc in results)


# ---------------------------------------------------------------------------
# FIX(v0.7.1-b2-8): _read_source 大小上限——MB 级文件读入后 ast.parse 必失败
# ---------------------------------------------------------------------------


def test_read_source_skips_oversized_file(tmp_path, monkeypatch):
    from app.runtime.collectors import static_analyzer as sa

    path = _write_source(tmp_path, _SOURCE)
    monkeypatch.setattr(sa, "_MAX_SOURCE_BYTES", 10)  # 源码远超 10 字节

    assert sa._read_source(path) is None  # 超限直接跳过，不读入

    frames = [{"file": path, "function": "get_user", "line": 6}]
    assert analyze(frames) == []  # 帧被跳过而非报错


def test_read_source_reads_within_limit(tmp_path):
    from app.runtime.collectors.static_analyzer import _read_source

    path = _write_source(tmp_path, _SOURCE)
    content = _read_source(path)
    assert content is not None
    assert "def get_user" in content


# ---------------------------------------------------------------------------
# FIX(v0.7.1-b4-8): 同文件多帧复用 ast 解析（不再每帧重复 parse）
# ---------------------------------------------------------------------------


def test_analyze_same_file_multi_frame_parses_once(tmp_path, monkeypatch):
    """调用栈多帧同属一文件时，ast.parse 只执行一次。

    旧实现每帧 _read_source + ast.parse 一次；新实现按 resolved 路径缓存
    (source, tree)。用 monkeypatch 计数 ast.parse 调用验证。
    """
    import ast as ast_module

    from app.runtime.collectors import static_analyzer as sa

    path = _write_source(tmp_path, _SOURCE)
    count = {"n": 0}
    real_parse = ast_module.parse

    def _counting_parse(source, *a, **k):
        count["n"] += 1
        return real_parse(source, *a, **k)

    monkeypatch.setattr(sa.ast, "parse", _counting_parse)

    frames = [
        {"file": path, "function": "process", "line": 9},
        {"file": path, "function": "get_user", "line": 6},
        {"file": path, "function": "helper", "line": 3},  # 同文件第三帧
    ]
    results = sa.analyze(frames)

    assert len(results) == 3  # 三帧都分析成功
    assert count["n"] == 1, f"同文件 3 帧应只 parse 1 次，实际 {count['n']}"


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


def test_resolve_path_rejects_traversal_and_absolute_outside(tmp_path, monkeypatch):
    """FIX: P0-2 任意文件读取（LFI）—— 白名单外路径（../穿越 / 绝对路径）
    必须被 _resolve_path 拒绝返回 None。"""
    from app.runtime.collectors.static_analyzer import _resolve_path

    # 白名单收敛到 tmp_path，任何位于其外的路径均拒绝
    monkeypatch.setattr(settings, "whitelist_path_prefix", str(tmp_path))
    monkeypatch.setattr(settings, "source_path_map", "")

    assert _resolve_path("/etc/passwd") is None
    assert _resolve_path("../../../../etc/passwd") is None
    assert _resolve_path(os.path.join(str(tmp_path), "..", "outside.py")) is None


def test_resolve_path_allows_whitelisted_file(tmp_path, monkeypatch):
    """白名单内的源码文件仍可正常解析。"""
    from app.runtime.collectors.static_analyzer import _resolve_path

    path = _write_source(tmp_path, _SOURCE)
    monkeypatch.setattr(settings, "whitelist_path_prefix", str(tmp_path))
    monkeypatch.setattr(settings, "source_path_map", "")

    resolved = _resolve_path(path)
    assert resolved is not None
    assert os.path.realpath(resolved) == os.path.realpath(path)


def test_resolve_path_source_map_traversal_rejected(tmp_path, monkeypatch):
    """FIX: P0-2 source_path_map 分支同样受白名单约束 —— 映射目标拼接出
    /app/../../etc/passwd 式穿越必须拒绝。"""
    from app.runtime.collectors.static_analyzer import _resolve_path

    monkeypatch.setattr(settings, "whitelist_path_prefix", str(tmp_path))
    # 映射远端 /app → 本地 tmp_path，目标含 ../ 穿越到白名单外
    monkeypatch.setattr(
        settings,
        "source_path_map",
        f"/app:{os.path.join(str(tmp_path), '..')}",
    )

    assert _resolve_path("/app/../../../etc/passwd") is None
    assert _resolve_path("/app/module.py") is None
