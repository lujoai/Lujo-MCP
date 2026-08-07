"""git 归因模块单测"""
import os
import shutil
import subprocess

import pytest

from app.config import settings
from app.runtime.core import git as git_core
from app.mcp.tools import git_api


@pytest.fixture(autouse=True)
def _reset_git_config():
    saved = (settings.git_path_whitelist, settings.git_timeout)
    settings.git_path_whitelist = ""
    settings.git_timeout = 10
    yield
    settings.git_path_whitelist, settings.git_timeout = saved


def test_is_allowed_empty_whitelist_defaults_to_cwd():
    """空名单时默认收敛到 cwd：cwd 内路径放行、cwd 外路径拒绝（SEC-01）。"""
    cwd = os.getcwd()
    assert git_core._is_allowed(os.path.join(cwd, "app", "main.py")) is True
    assert git_core._is_allowed("/anywhere/file.py") is False


def test_is_allowed_whitelist_denies_outside():
    settings.git_path_whitelist = "C:/proj, /home/me/proj"
    assert git_core._is_allowed("/home/me/proj/app/x.py") is True
    assert git_core._is_allowed("/etc/passwd") is False


def test_blame_nonexistent_file_returns_none():
    assert git_core.get_blame_for_frame("/no/such/file_xyz.py", 1) is None


def test_diff_nonexistent_file_returns_none():
    assert git_core.get_recent_diff("/no/such/file_xyz.py") is None


def test_blame_denied_by_whitelist():
    settings.git_path_whitelist = "C:/only_this_proj"
    # 非白名单路径：直接返回 None，不执行 git
    assert git_core.get_blame_for_frame("/other/repo/file.py", 1) is None
    assert git_core.get_recent_diff("/other/repo/file.py") is None


def test_tool_wrappers_return_not_found_when_none():
    res = git_api.tool_get_blame_for_frame("/no/such/file.py", 1)
    assert res["found"] is False
    assert res["blame"] is None

    res = git_api.tool_get_recent_diff("/no/such/file.py")
    assert res["found"] is False


@pytest.mark.skipif(not shutil.which("git"), reason="git 未安装")
def test_real_blame_in_temp_repo(tmp_path):
    """真实 git 冒烟：初始化临时仓库，提交一个文件，blame 应返回作者。"""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    f = tmp_path / "sample.py"
    f.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=tester", "-c", "user.email=t@t.test",
         "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
        cwd=tmp_path, check=True,
    )

    settings.git_path_whitelist = str(tmp_path)  # 仅允许临时仓库
    res = git_core.get_blame_for_frame(str(f), 1)
    assert res is not None
    assert res["author"] == "tester"
    assert res["commit"]
    assert res["line_text"].strip() == "x = 1"

    # 工具封装
    wrapped = git_api.tool_get_blame_for_frame(str(f), 1)
    assert wrapped["found"] is True
    assert wrapped["blame"]["author"] == "tester"
