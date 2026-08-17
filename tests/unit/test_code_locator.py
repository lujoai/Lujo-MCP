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
