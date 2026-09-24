"""spec 规范采集器单测"""
import pytest

from app.config import settings
from app.runtime.collectors import spec as spec_collector
from app.mcp.tools import spec_api


@pytest.fixture(autouse=True)
def _reset():
    saved = settings.redaction_enabled
    settings.redaction_enabled = True
    spec_collector.reload_specs()  # 清缓存
    yield
    spec_collector._spec_cache.update({"project_root": None, "specs": [], "mtime": 0})
    settings.redaction_enabled = saved


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_discover_and_match_by_extension(tmp_path):
    _write(tmp_path / "CONVENTION.md", "# 约定\n\n## API\n返回必须含 status 字段\n")
    _write(tmp_path / "STYLE_GUIDE.md", "# 样式\n\n按钮用 primary 色\n")
    py_file = tmp_path / "app" / "svc.py"
    _write(py_file, "x = 1")

    specs = spec_collector.get_related_specs(str(py_file), project_root=tmp_path)
    # CONVENTION 含 "api" 关键词 → 目标扩展名含 .py，应命中
    files = {s["file"] for s in specs}
    assert any("CONVENTION" in f for f in files)


def test_get_related_specs_returns_summary_and_content(tmp_path):
    _write(tmp_path / "API_SPEC.md", "# API 规范\n\n## 响应\n所有接口返回 {code,msg,data}\n")
    specs = spec_collector.get_related_specs(str(tmp_path / "a.py"), project_root=tmp_path)
    assert specs
    s = specs[0]
    assert s["summary"]
    assert "content" in s
    assert isinstance(s["tags"], list)


def test_redaction_applied_to_spec_content(tmp_path):
    _write(tmp_path / "CONVENTION.md", "# 约定\n\napi_key = \"sk-secret\" 用于鉴权\n")
    specs = spec_collector.get_related_specs(str(tmp_path / "a.py"), project_root=tmp_path)
    assert specs
    assert "sk-secret" not in specs[0]["content"]


def test_no_specs_returns_empty(tmp_path):
    py_file = tmp_path / "x.py"
    _write(py_file, "x=1")
    assert spec_collector.get_related_specs(str(py_file), project_root=tmp_path) == []


def test_skip_dirs_excluded(tmp_path):
    # node_modules 下的 md 应被跳过
    _write(tmp_path / "node_modules" / "lib" / "README.md", "# should be skipped\n")
    _write(tmp_path / "CONVENTION.md", "# real convention\n\n## api\nuse json\n")
    specs = spec_collector.get_project_specs(tmp_path)
    files = [s["file"] for s in specs]
    assert any("CONVENTION" in f for f in files)
    assert not any("node_modules" in f for f in files)


def test_caching(tmp_path):
    _write(tmp_path / "CONVENTION.md", "# c\n\n## api\nx\n")
    first = spec_collector.get_project_specs(tmp_path)
    assert len(first) == 1
    # 第二次应命中缓存（不重新扫描）
    second = spec_collector.get_project_specs(tmp_path)
    assert second is first  # 同一列表对象 → 缓存命中


def test_cache_refresh_limited_to_interval(tmp_path):
    """P2-E2：缓存命中时不做全项目 os.walk——interval 内对已缓存 project 直接放行。"""
    _write(tmp_path / "CONVENTION.md", "# c1\n\n## api\nx\n")
    first = spec_collector.get_project_specs(tmp_path)
    assert len(first) == 1

    # interval 内：修改文件也不触发重扫（仍返回缓存对象）
    _write(tmp_path / "CONVENTION.md", "# c2\n\n## api\nx\n")
    second = spec_collector.get_project_specs(tmp_path)
    assert second is first

    # 强制刷新：清除限频时间戳（间隔期满）→ 重新 walk 并构造新列表。
    # 同时清 mtime 避免依赖文件系统时间戳精度（rewrite 可能与首次 populate 同一秒）。
    spec_collector._spec_cache["checked_at"] = 0
    spec_collector._spec_cache["mtime"] = 0
    third = spec_collector.get_project_specs(tmp_path)
    assert third is not first


def test_tool_wrapper():
    res = spec_api.tool_get_related_specs("/no/such/file.py")
    assert res["found"] is False
    assert res["count"] == 0
    assert res["specs"] == []


# ---------------------------------------------------------------------------
# FIX(v0.7.1-b2-10): _find_project_root 的 home 边界改路径对象判定
# ---------------------------------------------------------------------------


def test_find_project_root_stops_at_home_boundary(tmp_path, monkeypatch):
    """home 前缀相似目录不再越过用户主目录边界（/home/us 不命中 /home/user2）。

    场景：文件位于 sibling（与 fake_home 字符串前缀相似）下、.git 在
    sibling 层。旧实现 ``str(parent).startswith(str(home))`` 前缀比较
    放行 sibling → 误把 sibling 当项目根；新实现按路径对象判定
    fake_home 非 ancestor 即停 → 返回文件所在目录。
    """
    from pathlib import Path

    fake_home = tmp_path / "home" / "us"
    fake_home.mkdir(parents=True)
    sibling = tmp_path / "home" / "us2"  # 与 fake_home 字符串前缀相似
    (sibling / ".git").mkdir(parents=True)
    project = sibling / "proj"  # 无 .git 标记
    project.mkdir(parents=True)

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    root = spec_collector._find_project_root(str(project / "a.py"))
    # fake_home 不是 project 的祖先：不得越过边界把 sibling 下的 .git 当项目根
    assert root == project


def test_find_project_root_within_home(tmp_path, monkeypatch):
    """正常场景：文件在 home 内时仍向上找到项目根（含 .git）。"""
    from pathlib import Path

    fake_home = tmp_path / "home" / "us"
    project = fake_home / "proj"
    project.mkdir(parents=True)
    (project / ".git").mkdir()

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    root = spec_collector._find_project_root(str(project / "src" / "a.py"))
    assert root == project


# ---------------------------------------------------------------------------
# FIX(v0.7.1-b3-3): _spec_cache 加锁——并发调用不崩、不互踢
# ---------------------------------------------------------------------------


def test_get_project_specs_concurrent_calls_no_crash(tmp_path):
    """并发调用：加锁后不崩、结果一致（轻量回归防惊群/互踢）。"""
    import threading

    _write(tmp_path / "API_SPEC.md", "# API 规范\n\n## 响应\n返回 code,msg,data\n")
    results = []
    errors = []

    def _call():
        try:
            results.append(len(spec_collector.get_project_specs(tmp_path)))
        except Exception as e:  # pragma: no cover - 失败时记录
            errors.append(e)

    threads = [threading.Thread(target=_call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert results == [1] * 8  # 同一项目在同一次刷新窗口内返回一致结果


# ---------------------------------------------------------------------------
# 虚拟/不存在帧守卫——不得进入项目根查找与规范目录遍历
# 根因：浏览器堆栈的伪帧路径（"eval at evaluate (" 等）被 _find_project_root
# 沿祖先链解析到用户主目录级 package.json，随后对整个主目录 os.walk。
# ---------------------------------------------------------------------------


def _forbid_root_discovery(monkeypatch):
    """守卫断言辅助：项目根查找/规范遍历一旦被触发即让用例失败。"""

    def _boom(*args, **kwargs):
        raise AssertionError("virtual/nonexistent frame must not reach root discovery or spec walk")

    monkeypatch.setattr(spec_collector, "_find_project_root", _boom)
    monkeypatch.setattr(spec_collector, "get_project_specs", _boom)
    # 冗余哨兵：discover_spec_files 目前只经 get_project_specs 间接调用，
    # 单独 patch 可防未来重构绕过被拦截的入口（如 _load_specs 直呼）。
    monkeypatch.setattr(spec_collector, "discover_spec_files", _boom)


def test_virtual_browser_frames_skip_spec_discovery(monkeypatch):
    """Chrome eval 踪迹帧 / <anonymous> / URL / 空串：返回空且不触发根查找与遍历。"""
    _forbid_root_discovery(monkeypatch)
    for virtual in ("eval at evaluate (", "<anonymous>", "http://site.test/app.js", ""):
        assert spec_collector.get_related_specs(virtual) == []


def test_nonexistent_local_frame_skips_spec_discovery(monkeypatch, tmp_path):
    """本地不存在的文件帧：同样安全跳过，不沿祖先链找根、不遍历目录。"""
    _forbid_root_discovery(monkeypatch)
    assert spec_collector.get_related_specs(str(tmp_path / "no_such_file.py")) == []


def test_unc_and_device_paths_skip_spec_discovery(monkeypatch):
    """UNC 共享 / 设备命名空间帧：存在性检查之前排除，不发起 SMB 网络访问。

    eval 帧名是任意字符串，可携带 ``\\\\server\\share`` 形态；``os.path.isfile``
    对其发起 SMB 访问（挂起 + 信息泄露面），下游 resolve/exists/os.walk 同样
    触网，必须整个挡在门外——isfile 一旦被触达即让用例失败。
    """
    _forbid_root_discovery(monkeypatch)

    def _isfile_boom(*args, **kwargs):
        raise AssertionError("UNC/device path must not reach os.path.isfile (SMB touch)")

    monkeypatch.setattr(spec_collector.os.path, "isfile", _isfile_boom)
    for non_local in (
        "\\\\evil.example.com\\share\\app.js",  # UNC（反斜杠形态）
        "//evil.example.com/share/app.js",  # UNC（正斜杠形态）
        "\\/evil.example.com\\share\\app.js",  # UNC（混合：反斜杠 + 正斜杠）
        "/\\evil.example.com/share/app.js",  # UNC（混合：正斜杠 + 反斜杠）
        "\\\\.\\PhysicalDrive0",  # 设备命名空间
        "\\\\?\\C:\\app.js",  # 扩展长度前缀
    ):
        assert spec_collector.get_related_specs(non_local) == []


def test_frame_shape_rejection_makes_no_filesystem_access(monkeypatch):
    """形态拒绝不依赖存在性检查：伪帧/URL/UNC/空路径一旦触达 isfile 即失败。

    与 get_related_specs 级用例互补——后者中 ``<anonymous>``、URL 之所以被拒
    也可能来自 isfile 返回 False；本用例把 isfile 钉死为失败，锁定字符串形态
    分支（``<`` 前缀 / ``://`` / 四种分隔符前缀）本身生效，防"靠 isfile=False
    兜底"的假绿。
    """

    def _isfile_boom(*args, **kwargs):
        raise AssertionError("shape-rejected frame must not reach os.path.isfile")

    monkeypatch.setattr(spec_collector.os.path, "isfile", _isfile_boom)
    for shape in (
        "",  # 空路径
        "<anonymous>",  # 伪帧
        "http://site.test/app.js",  # 页面 URL
        "\\\\srv\\share\\app.js",  # UNC（反斜杠）
        "//srv/share/app.js",  # UNC（正斜杠）
        "\\/srv\\share\\app.js",  # UNC（混合：反斜杠 + 正斜杠）
        "/\\srv/share/app.js",  # UNC（混合：正斜杠 + 反斜杠）
        "\\\\.\\PhysicalDrive0",  # 设备命名空间
    ):
        assert spec_collector._is_scannable_source_path(shape) is False


def test_explicit_project_root_bypasses_guard(monkeypatch, tmp_path):
    """显式传 project_root 时守卫不介入：虚拟帧只参与规范匹配，不触发根查找。

    锁定"显式 root 行为不变"契约——若守卫改为无条件拦截（丢掉
    ``project_root is None`` 前置条件），本用例即失败。
    """
    _write(tmp_path / "CONVENTION.md", "# 约定\n\n## api\n返回必须含 status 字段\n")

    def _boom(*args, **kwargs):
        raise AssertionError("explicit project_root must not trigger root discovery")

    monkeypatch.setattr(spec_collector, "_find_project_root", _boom)
    specs = spec_collector.get_related_specs("<anonymous>", project_root=tmp_path)
    assert any("CONVENTION" in s["file"] for s in specs)


def test_existing_local_frame_still_discovers_specs(tmp_path, monkeypatch):
    """有效本地源码帧：不传 project_root 时仍走根查找 → 规范发现行为保持。

    FIX: 平台无关夹具——用 monkeypatch 把逻辑 home 钉在 tmp_path 之下
    （范式同 test_find_project_root_within_home），避免依赖真实
    ``Path.home()`` 与宿主临时目录的相对位置：Linux runner 的
    ``/tmp/pytest-of-*`` 不在 home 之下，会使根查找在第一个祖先处停住、
    扫描根退化为文件父目录。
    """
    from pathlib import Path

    fake_home = tmp_path / "home" / "us"
    project = fake_home / "proj"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    (project / ".git").mkdir(parents=True)  # 项目根标记（位于逻辑 home 之下）
    _write(project / "CONVENTION.md", "# 约定\n\n## api\n返回必须含 status 字段\n")
    py_file = project / "src" / "svc.py"
    _write(py_file, "x = 1")

    specs = spec_collector.get_related_specs(str(py_file))
    assert any("CONVENTION" in s["file"] for s in specs)
