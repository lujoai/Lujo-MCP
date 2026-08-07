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


def test_tool_wrapper():
    res = spec_api.tool_get_related_specs("/no/such/file.py")
    assert res["found"] is False
    assert res["count"] == 0
    assert res["specs"] == []
