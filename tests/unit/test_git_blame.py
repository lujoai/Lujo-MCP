"""git blame/diff 工具单测（FIX v0.7.1-b4-3：blame 补 `--` 路径分隔符）。"""

from types import SimpleNamespace

import pytest

from app.runtime.core import git as git_module


def test_blame_uses_double_dash_path_separator(monkeypatch):
    """FIX(v0.7.1-b4-3): git blame 必须用 `--` 分隔路径。

    旧实现缺 `--`：路径以 `-` 开头（如被 cwd 相对解析的奇名文件）会被 git
    当作选项。用 mock subprocess 断言命令串中 `--` 出现在路径之前。
    """
    captured: dict = {}

    def _fake_run(cmd, **_kw):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="abc123 \nAUTHOR\n")

    monkeypatch.setattr(git_module.subprocess, "run", _fake_run)
    # 用仓库内真实存在且在白名单内的文件（避免路径白名单/存在性分支短路）
    real_path = git_module.Path("app/config.py").resolve()

    git_module.get_blame_for_frame(str(real_path), 10)

    cmd = captured["cmd"]
    assert "--" in cmd, "blame 命令必须含 `--` 路径分隔符"
    assert cmd.index("--") < cmd.index(str(real_path)), (
        f"路径必须在 `--` 之后，cmd={cmd}"
    )


def test_blame_returns_none_when_command_fails(monkeypatch):
    """git 命令失败 → 返回 None（不抛）。"""
    monkeypatch.setattr(
        git_module.subprocess, "run",
        lambda *a, **kw: SimpleNamespace(returncode=128, stdout=""),
    )
    real_path = git_module.Path("app/config.py").resolve()
    assert git_module.get_blame_for_frame(str(real_path), 10) is None