"""P1-D「跨项目 git 根授权」：GIT_PATH_WHITELIST 授权项目根语义单测。

背景（真实事故）：宿主 AI 调试另一个项目（car_project_java）时，
get_recent_diff / get_blame_for_frame 全部 found:false——旧前缀白名单把
Lujo 进程工作目录之外的路径一律拒绝，而「调试目标项目不是 Lujo 自己的
仓库」是主场景。本文件锁定升级后的授权根语义：

1. 授权根外的路径被拒，且拒绝带结构化原因（哪个根都没匹配上），不再静默；
2. 授权根内（含嵌套子目录）通过；
3. ``..`` 逃逸被 resolve 规范化后仍拒；
4. 未配置时默认收敛为进程工作目录（安全默认不放宽）；
5. Windows 大小写不敏感（os.path.normcase 归一后比较）；
6. 根内符号链接指向根外目标：resolve 跟随链接后仍拒（无权限环境跳过）。
"""
import os
import sys

import pytest

from app.config import settings
from app.runtime.core import git as git_core


@pytest.fixture(autouse=True)
def _reset_git_config():
    saved = (settings.git_path_whitelist, settings.git_timeout)
    settings.git_path_whitelist = ""
    settings.git_timeout = 10
    yield
    settings.git_path_whitelist, settings.git_timeout = saved


# ── ① 授权根外被拒，且带结构化原因 ──


def test_outside_roots_denied_with_structured_reason(tmp_path):
    root = tmp_path / "auth_proj"
    root.mkdir()
    other = tmp_path / "other_proj" / "src"
    other.mkdir(parents=True)
    settings.git_path_whitelist = str(root)

    check = git_core.check_path_allowed(str(other / "x.py"))
    assert check["allowed"] is False
    # 拒绝不再静默：结构化原因必须说明哪个根都没匹配上，并回显生效根。
    # reason 内路径为 normcase 规范化形式（Windows 下小写盘符），两侧同归一比较
    assert check["reason"]
    assert os.path.normcase(os.path.realpath(str(root))) in os.path.normcase(check["reason"])
    assert os.path.normcase(os.path.realpath(str(other / "x.py"))) in os.path.normcase(
        check["reason"]
    )
    assert check["roots"] == [os.path.realpath(str(root))]


def test_denied_reason_survives_blame_wrapper_logging(tmp_path):
    """core 层拒绝时 blame/diff 仍返回 None（对外契约不变），但走结构化校验。"""
    root = tmp_path / "auth_proj"
    root.mkdir()
    settings.git_path_whitelist = str(root)
    outside = tmp_path / "elsewhere" / "f.py"
    assert git_core.get_blame_for_frame(str(outside), 1) is None
    assert git_core.get_recent_diff(str(outside)) is None


# ── ② 授权根内（含嵌套子目录、多根）通过 ──


def test_inside_roots_allowed_including_nested(tmp_path):
    root1 = tmp_path / "proj1"
    nested = root1 / "src" / "deep"
    nested.mkdir(parents=True)
    f = nested / "x.py"
    f.write_text("x = 1\n", encoding="utf-8")
    settings.git_path_whitelist = str(root1)

    assert git_core.check_path_allowed(str(f))["allowed"] is True
    assert git_core.check_path_allowed(str(nested))["allowed"] is True
    # 根本身（边界相等）也放行
    assert git_core.check_path_allowed(str(root1))["allowed"] is True

    # 多根：逗号分隔，任一根命中即放行
    root2 = tmp_path / "proj2"
    root2.mkdir()
    settings.git_path_whitelist = f"{root1},{root2}"
    assert git_core.check_path_allowed(str(root2 / "a.py"))["allowed"] is True
    assert git_core.get_authorized_roots() == [
        os.path.realpath(str(root1)),
        os.path.realpath(str(root2)),
    ]


# ── ③ ../ 逃逸：resolve 规范化后仍拒 ──


def test_dotdot_escape_resolved_then_denied(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    settings.git_path_whitelist = str(root)

    sneaky = str(root / ".." / "elsewhere" / "secret.py")
    # 前置自检：该路径真实落点确实在授权根之外
    real = os.path.realpath(sneaky)
    assert real.startswith(os.path.realpath(str(tmp_path)))
    assert not real.startswith(os.path.realpath(str(root)) + os.sep)

    check = git_core.check_path_allowed(sneaky)
    assert check["allowed"] is False
    # resolved 字段必须是规范化后的真实路径，便于日志归因
    assert check["resolved"] == os.path.normcase(real)


# ── ④ 未配置时默认收敛为进程工作目录 ──


def test_default_cwd_when_unset():
    cwd = os.path.realpath(os.getcwd())
    assert git_core.check_path_allowed(os.path.join(cwd, "app", "config.py"))["allowed"] is True
    # cwd 的兄弟目录（如被调试的另一个项目）默认拒绝
    sibling = os.path.join(os.path.dirname(cwd), "some_other_project_debugged", "x.py")
    check = git_core.check_path_allowed(sibling)
    assert check["allowed"] is False
    assert cwd in check["reason"]
    assert git_core.get_authorized_roots() == [cwd]


# ── ⑤ Windows 大小写不敏感（normcase 归一） ──


@pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="Windows 文件系统大小写不敏感语义；POSIX 上大小写不同即不同路径",
)
def test_normcase_case_insensitive_on_windows(tmp_path):
    root = tmp_path / "MyProj"
    (root / "src").mkdir(parents=True)
    settings.git_path_whitelist = str(root)

    # 授权根与查询路径大小写拼写不同（同一物理路径），必须命中同一根
    flipped = str(root).swapcase()
    check = git_core.check_path_allowed(os.path.join(flipped, "src", "x.py"))
    assert check["allowed"] is True
    assert check["resolved"] == os.path.normcase(os.path.realpath(str(root))) + (
        os.sep + "src" + os.sep + "x.py"
    )


# ── ⑥ 根内 symlink 指向根外：resolve 跟随链接后仍拒 ──


def test_symlink_inside_root_pointing_outside_denied(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.py"
    secret.write_text("s = 1\n", encoding="utf-8")

    link = root / "link.py"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境无权限创建符号链接（Windows 非开发者模式常见）")

    settings.git_path_whitelist = str(root)
    check = git_core.check_path_allowed(str(link))
    assert check["allowed"] is False
    assert check["resolved"] == os.path.normcase(os.path.realpath(str(secret)))
