"""单元测试：浏览器回退链探测 browser_launcher（v0.9.8 浏览器能力进冻结包）。



覆盖 resolve_launch_kwargs() 回退链契约：

① playwright 管理的 chromium 优先；

② 系统 Chrome（channel="chrome"）兜底；

③ 系统 Edge（channel="msedge"）兜底（Windows 必有）；

④ 全部缺失返回 None（调用方回落 CAPABILITY_MISSING）；

⑤ 进程内缓存：第二次调用不重复探测。



探测必须是轻量路径存在性检查（不 launch、不 spawn playwright driver），

全部用例通过 monkeypatch env / 伪造路径存在性实现，不触碰真实浏览器。

"""

from app.runtime.verifier import browser_launcher as bl



import pytest





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





class TestFallbackChain:

    def test_system_chrome_hit(self, tmp_path, monkeypatch):

        """①② 顺序：无 playwright chromium 时，系统 Chrome 命中 -> channel=chrome。"""

        _make_exe(tmp_path, "PF", "Google", "Chrome", "Application", "chrome.exe")

        monkeypatch.setenv("ProgramFiles", str(tmp_path / "PF"))

        monkeypatch.delenv("ProgramFiles(x86)", raising=False)

        monkeypatch.delenv("LocalAppData", raising=False)

        monkeypatch.setattr(bl, "_probe_managed_chromium", lambda: None)

        monkeypatch.setattr(bl.shutil, "which", lambda name: None)



        assert bl.resolve_launch_kwargs() == {"channel": "chrome"}



    def test_managed_chromium_preferred_over_system(self, tmp_path, monkeypatch):

        """① playwright 管理的 chromium 命中时优先于系统浏览器。"""

        managed = _make_exe(tmp_path, "pw", "chrome.exe")

        _make_exe(tmp_path, "PF", "Google", "Chrome", "Application", "chrome.exe")

        monkeypatch.setenv("ProgramFiles", str(tmp_path / "PF"))

        monkeypatch.delenv("ProgramFiles(x86)", raising=False)

        monkeypatch.delenv("LocalAppData", raising=False)

        monkeypatch.setattr(bl, "_probe_managed_chromium", lambda: managed)



        assert bl.resolve_launch_kwargs() == {"executable_path": managed}



    def test_edge_fallback_when_chrome_missing(self, tmp_path, monkeypatch):

        """③ Chrome 缺失时 Edge 兜底（ProgramFiles(x86)，Windows 必有）。"""

        _make_exe(tmp_path, "PF86", "Microsoft", "Edge", "Application", "msedge.exe")

        monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "PF86"))

        monkeypatch.delenv("ProgramFiles", raising=False)

        monkeypatch.delenv("LocalAppData", raising=False)

        monkeypatch.setattr(bl, "_probe_managed_chromium", lambda: None)



        assert bl.resolve_launch_kwargs() == {"channel": "msedge"}



    def test_none_when_nothing_found(self, tmp_path, monkeypatch):

        """④ 全部缺失：返回 None（调用方回落 CAPABILITY_MISSING）。"""

        for env in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):

            monkeypatch.delenv(env, raising=False)

        monkeypatch.setattr(bl, "_probe_managed_chromium", lambda: None)



        monkeypatch.setattr(bl.shutil, "which", lambda name: None)
        assert bl.resolve_launch_kwargs() is None





class TestManagedChromiumProbe:

    def test_fake_registry_override(self, tmp_path, monkeypatch):

        """PLAYWRIGHT_BROWSERS_PATH 覆盖注册目录：伪造 chromium-* 布局可被 glob 兜底命中。"""

        # 双平台布局都创建：launcher 按 sys.platform 匹配自己的候选相对路径
        exe = _make_exe(tmp_path, "reg", "chromium-9999", "chrome-win64", "chrome.exe")
        _make_exe(tmp_path, "reg", "chromium-9999", "chrome-win", "chrome.exe")
        _make_exe(tmp_path, "reg", "chromium-9999", "chrome-linux", "chrome")

        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "reg"))



        assert bl._probe_managed_chromium() == exe



    def test_env_zero_is_not_override(self, tmp_path, monkeypatch):

        """PLAYWRIGHT_BROWSERS_PATH=0 不是目录覆盖：回落默认注册目录（此处为空）。"""

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
