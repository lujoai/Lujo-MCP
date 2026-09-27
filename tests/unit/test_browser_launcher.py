"""单元测试：浏览器回退链探测 browser_launcher（v0.9.8 浏览器能力进冻结包）。

覆盖 resolve_launch_kwargs() 回退链契约：

① playwright 管理的 chromium 优先；

② 系统 Chrome（channel="chrome"）兜底；

③ 系统 Edge（channel="msedge"）兜底（Windows 必有）；

④ 全部缺失返回 None（调用方回落 CAPABILITY_MISSING）；

⑤ 进程内缓存：第二次调用不重复探测。

密封策略：launcher 的全部环境探测面——sys.platform 分支、安装路径 env
（ProgramFiles / ProgramFiles(x86) / LocalAppData）、shutil.which、注册目录与
路径存在性——一律 monkeypatch / 临时目录伪造，不依赖 CI 上"恰好没有/恰好有"
的真实浏览器。探测本身是轻量路径存在性检查（不 launch、不 spawn playwright
driver），monkeypatch 安全。

系统浏览器用例按 sys.platform 参数化：win32 分支（固定安装路径 env）与 posix
分支（shutil.which）是 launcher 的两套真实实现，在任意 OS 上都被确定性覆盖；
fake registry 用例的断言跟随 launcher 当前平台的实际候选顺序，不做平台硬编码。
"""

import sys

import pytest

from app.runtime.verifier import browser_launcher as bl


@pytest.fixture(autouse=True)
def _fresh_cache():
    """每个用例独立缓存状态，避免用例间串味。"""
    bl.reset_launch_cache()
    yield
    bl.reset_launch_cache()


def _make_exe(root, *parts) -> str:
    exe = root.joinpath(*parts)
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"")
    return str(exe)


def _pin_platform(monkeypatch, platform: str) -> None:
    """固定 launcher 的 sys.platform 分支——它本身是探测面之一，必须密封。

    launcher 在调用点读取 sys.platform 选择候选实现（win32=env 固定安装路径，
    darwin/linux=shutil.which），不 pin 的话同一用例在 Windows 本地与 ubuntu CI
    走不同分支，密封面不同。
    """
    monkeypatch.setattr(sys, "platform", platform)


class TestFallbackChain:
    @pytest.mark.parametrize("platform", ["win32", "linux"])
    def test_system_chrome_hit(self, tmp_path, monkeypatch, platform):
        """①② 顺序：无 playwright chromium 时，系统 Chrome 命中 -> channel=chrome。

        win32 分支经 ProgramFiles 固定安装路径命中；linux 分支经 which 命中。
        which 全局封为 None，杜绝 CI 上真实安装的浏览器从任何名字漏入。
        """
        _pin_platform(monkeypatch, platform)
        monkeypatch.setattr(bl, "_probe_managed_chromium", lambda: None)
        monkeypatch.setattr(bl.shutil, "which", lambda name: None)
        if platform == "win32":
            _make_exe(tmp_path, "PF", "Google", "Chrome", "Application", "chrome.exe")
            monkeypatch.setenv("ProgramFiles", str(tmp_path / "PF"))
            monkeypatch.delenv("ProgramFiles(x86)", raising=False)
            monkeypatch.delenv("LocalAppData", raising=False)
        else:
            fake = _make_exe(tmp_path, "bin", "google-chrome")
            monkeypatch.setattr(
                bl.shutil,
                "which",
                lambda name: fake if name.startswith("google-chrome") else None,
            )

        assert bl.resolve_launch_kwargs() == {"channel": "chrome"}

    def test_managed_chromium_preferred_over_system(self, tmp_path, monkeypatch):
        """① playwright 管理的 chromium 命中时优先于系统浏览器（短路，与平台无关）。"""
        managed = _make_exe(tmp_path, "pw", "chrome.exe")
        _make_exe(tmp_path, "PF", "Google", "Chrome", "Application", "chrome.exe")
        monkeypatch.setenv("ProgramFiles", str(tmp_path / "PF"))
        monkeypatch.delenv("ProgramFiles(x86)", raising=False)
        monkeypatch.delenv("LocalAppData", raising=False)
        monkeypatch.setattr(bl, "_probe_managed_chromium", lambda: managed)

        assert bl.resolve_launch_kwargs() == {"executable_path": managed}

    @pytest.mark.parametrize("platform", ["win32", "linux"])
    def test_edge_fallback_when_chrome_missing(self, tmp_path, monkeypatch, platform):
        """③ Chrome 缺失时 Edge 兜底。

        "chrome 缺失"必须在探测面上显式成立：posix 分支 which 对 google-chrome*
        一律 None（CI runner 真实装了 google-chrome，不封则 chrome 抢先命中）；
        win32 分支不设任何 chrome 安装路径 env。
        """
        _pin_platform(monkeypatch, platform)
        monkeypatch.setattr(bl, "_probe_managed_chromium", lambda: None)
        monkeypatch.setattr(bl.shutil, "which", lambda name: None)
        if platform == "win32":
            _make_exe(
                tmp_path, "PF86", "Microsoft", "Edge", "Application", "msedge.exe"
            )
            monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "PF86"))
            monkeypatch.delenv("ProgramFiles", raising=False)
            monkeypatch.delenv("LocalAppData", raising=False)
        else:
            fake = _make_exe(tmp_path, "bin", "microsoft-edge")
            monkeypatch.setattr(
                bl.shutil,
                "which",
                lambda name: fake if name.startswith("microsoft-edge") else None,
            )

        assert bl.resolve_launch_kwargs() == {"channel": "msedge"}

    def test_none_when_nothing_found(self, monkeypatch):
        """④ 全部缺失：返回 None（调用方回落 CAPABILITY_MISSING）。"""
        _pin_platform(monkeypatch, "win32")
        for env in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
            monkeypatch.delenv(env, raising=False)
        monkeypatch.setattr(bl, "_probe_managed_chromium", lambda: None)
        monkeypatch.setattr(bl.shutil, "which", lambda name: None)

        assert bl.resolve_launch_kwargs() is None


class TestManagedChromiumProbe:
    def test_fake_registry_override(self, tmp_path, monkeypatch):
        """PLAYWRIGHT_BROWSERS_PATH 覆盖注册目录：伪造 chromium-* 布局可被 glob 兜底命中。

        断言不硬编码单一平台布局：全部平台候选目录都落盘，期望值取 launcher
        当前 sys.platform 的第一个候选（win: chrome-win64/chrome.exe；
        linux: chrome-linux/chrome；mac: chrome-mac/...），与探测顺序一致。
        """
        reg = tmp_path / "reg"
        for layout in (
            ("chrome-win64", "chrome.exe"),
            ("chrome-win", "chrome.exe"),
            ("chrome-linux", "chrome"),
            ("chrome-mac", "Chromium.app", "Contents", "MacOS", "Chromium"),
        ):
            _make_exe(reg, "chromium-9999", *layout)
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(reg))

        expected = reg.joinpath("chromium-9999", *bl._chromium_exe_relative()[0])

        assert bl._probe_managed_chromium() == str(expected)

    def test_env_zero_is_not_override(self, tmp_path, monkeypatch):
        """PLAYWRIGHT_BROWSERS_PATH=0 不是目录覆盖：回落默认注册目录（此处为空）。

        密封：pin win32 分支 + LOCALAPPDATA 指向空临时目录，默认注册目录
        即 tmp/ms-playwright（空）；否则 linux 上取决于 ~/.cache/ms-playwright
        "恰好没有"真实 chromium，属环境耦合。
        """
        _pin_platform(monkeypatch, "win32")
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

        assert bl._probe_managed_chromium() is None

    def test_result_shape_in_real_environment(self):
        """真实环境冒烟：结果形状恒为 {channel|executable_path} 或 None，不抛异常。"""
        bl.reset_launch_cache()
        kwargs = bl.resolve_launch_kwargs()

        assert kwargs is None or (
            kwargs
            and set(kwargs) <= {"channel", "executable_path"}
            and (kwargs.get("channel") in ("chrome", "msedge", None))
        )


class TestProbeCache:
    def test_second_call_does_not_reprobe(self, monkeypatch):
        """⑤ 缓存生效：第二次调用不重复探测（进程内一次）。"""
        calls = []

        def fake_probe():
            calls.append(1)
            return {"channel": "chrome"}

        monkeypatch.setattr(bl, "_probe_launch_kwargs", fake_probe)
        assert bl.resolve_launch_kwargs() == {"channel": "chrome"}
        assert bl.resolve_launch_kwargs() == {"channel": "chrome"}
        assert len(calls) == 1

    def test_reset_cache_forces_reprobe(self, monkeypatch):
        """reset_launch_cache 供测试/诊断强制重新探测。"""
        calls = []

        def fake_probe():
            calls.append(1)
            return None

        monkeypatch.setattr(bl, "_probe_launch_kwargs", fake_probe)
        assert bl.resolve_launch_kwargs() is None
        bl.reset_launch_cache()
        assert bl.resolve_launch_kwargs() is None
        assert len(calls) == 2
