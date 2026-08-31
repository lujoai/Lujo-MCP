"""单元测试：code_locator 源码定位（重点覆盖 SOURCE_PATH_MAP 映射后 linecache 读取本地文件）。"""

import os

import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    """记录并恢复会影响 code_locator 的全局设置。"""
    monkeypatch.setattr(settings, "whitelist_path_prefix", "")
    monkeypatch.setattr(settings, "source_path_map", "")
    monkeypatch.setattr(settings, "code_context_lines", 5)
    monkeypatch.setattr(settings, "ide_scheme", "vscode")


def test_get_code_snippet_respects_source_path_map(tmp_path, monkeypatch):
    """P3-2: SOURCE_PATH_MAP 映射后，linecache 应读取本地映射文件而非原始远程路径。

    修复前白名单校验通过映射路径，但 linecache 仍按原始远程路径读取 → 读不到本地文件，
    snippet 为空；修复后应能从本地映射文件读到源码。
    """
    from app.runtime.collectors.code_locator import get_code_snippet

    local_dir = str(tmp_path).replace(os.sep, "/")
    local_file = os.path.join(str(tmp_path), "remote_mod.py")
    with open(local_file, "w", encoding="utf-8") as f:
        f.write("line1\nline2\nerror_here()\nline4\nline5\n")

    monkeypatch.setattr(settings, "whitelist_path_prefix", str(tmp_path))
    monkeypatch.setattr(settings, "source_path_map", f"/remote:{local_dir}")

    snippet = get_code_snippet("/remote/remote_mod.py", 3, context_lines=1)

    assert snippet.found is True
    assert snippet.file == "/remote/remote_mod.py"
    assert "error_here()" in snippet.snippet
    assert ">>> 3: error_here()" in snippet.snippet
    assert snippet.link is not None


def test_get_code_snippet_rejects_mapped_path_outside_whitelist(tmp_path, monkeypatch):
    """映射后的路径仍在白名单之外时，必须被拒绝（防 SOURCE_PATH_MAP 绕过白名单）。"""
    from app.runtime.collectors.code_locator import get_code_snippet

    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    monkeypatch.setattr(settings, "whitelist_path_prefix", str(allowed_dir))
    # 映射目标 tmp_path 不在白名单 allowed_dir 内 → 拒绝
    monkeypatch.setattr(settings, "source_path_map", f"/remote:{str(tmp_path)}")

    snippet = get_code_snippet("/remote/nope.py", 1)

    assert snippet.found is False
    assert snippet.snippet == ""
    assert snippet.link is None


# ---------------------------------------------------------------------------
# FIX(v0.7.1-b2-6): 畸形帧逐帧跳过——单帧 line 缺失/None/非数值不再丢整批
# ---------------------------------------------------------------------------


def test_get_snippets_for_frames_skips_malformed_line_frames(tmp_path, monkeypatch):
    """畸形 line 帧（缺失/None/非数值/bool）被逐帧跳过，其余帧片段正常返回。

    旧实现 f["line"] 直索引：单帧缺 line 抛 KeyError、line=None 在
    start = max(1, line_no - context_lines) 抛 TypeError，builder 兜底
    吞掉后整批 code_snippets 全部丢失。
    """
    from app.runtime.collectors.code_locator import get_snippets_for_frames

    monkeypatch.setattr(settings, "whitelist_path_prefix", str(tmp_path))
    good_file = tmp_path / "good.py"
    good_file.write_text("line1\ndef boom():\n    raise\nline4\n", encoding="utf-8")

    frames = [
        {"file": str(good_file), "line": 3, "function": "boom"},   # 正常帧
        {"file": str(good_file), "function": "no_line"},            # 缺 line 键（原 KeyError）
        {"file": str(good_file), "line": None, "function": "n"},    # line=None（原 TypeError）
        {"file": str(good_file), "line": "3x", "function": "s"},    # 非数值（原 ValueError）
        {"file": str(good_file), "line": True, "function": "b"},    # bool（int 子类，须排除）
    ]
    snippets = get_snippets_for_frames(frames)

    assert len(snippets) == 1  # 只有正常帧产出片段，其余逐帧跳过
    assert snippets[0].found is True
    assert "raise" in snippets[0].snippet


def test_get_snippets_for_frames_string_numeric_line_accepted(tmp_path, monkeypatch):
    """数字字符串行号（SDK/浏览器帧常见形态）可正常转换，不误杀。"""
    from app.runtime.collectors.code_locator import get_snippets_for_frames

    monkeypatch.setattr(settings, "whitelist_path_prefix", str(tmp_path))
    good_file = tmp_path / "good.py"
    good_file.write_text("line1\ndef boom():\n    raise\nline4\n", encoding="utf-8")

    snippets = get_snippets_for_frames(
        [{"file": str(good_file), "line": "3", "function": "boom"}]
    )
    assert len(snippets) == 1
    assert snippets[0].found is True


# ---------------------------------------------------------------------------
# FIX(v0.7.1-b8-1): vscode:// URL Windows 反斜杠转正斜杠
# FIX(v0.7.1-b8-5): SOURCE_PATH_MAP 盘符感知拆分（Windows 盘符冒号不再当分隔符）
# ---------------------------------------------------------------------------


def test_split_remote_local_windows_drive_letter():
    """Windows 盘符冒号不作为 remote/local 分隔符。"""
    from app.runtime.collectors.code_locator import _split_remote_local

    # Windows remote + Windows local：第二个冒号才是分隔符
    assert _split_remote_local("C:\\app:C:\\local") == ("C:\\app", "C:\\local")
    # Unix remote + Windows local：首个冒号是分隔符
    assert _split_remote_local("/app:C:\\local") == ("/app", "C:\\local")
    # Windows remote + Unix local
    assert _split_remote_local("C:\\app:/Users/me") == ("C:\\app", "/Users/me")


def test_split_remote_local_unix():
    """纯 Unix 路径：首个冒号是分隔符。"""
    from app.runtime.collectors.code_locator import _split_remote_local

    assert _split_remote_local("/app:/Users/me") == ("/app", "/Users/me")
    assert _split_remote_local("no-colon") == ("", "")


def test_make_ide_link_escapes_backslashes(monkeypatch):
    """vscode:// URL 中不得残留反斜杠（Windows 路径）。"""
    from app.runtime.collectors import code_locator

    monkeypatch.setattr(code_locator, "_remap_path", lambda p: "C:\\project\\app.py")
    monkeypatch.setattr(code_locator, "_is_allowed", lambda p: True)

    link = code_locator.make_ide_link("C:\\project\\app.py", 42)

    assert link is not None
    assert link.startswith("vscode://file/")
    assert "\\" not in link
    assert ":42" in link
