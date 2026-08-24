"""单元测试：URL Resolver 路径模板正则 + 无堆栈 handler 静态分析（M3）。"""
import tempfile
import os

import pytest


@pytest.fixture(autouse=True)
def _allow_temp_source_paths(monkeypatch):
    """P0-2 LFI 修复后白名单默认收敛到项目根/CWD；测试用系统临时文件
    模拟 handler 源码，需把 temp 目录加入 whitelist_path_prefix。"""
    from app.config import settings

    prefix = (settings.whitelist_path_prefix or "").strip()
    roots = [p.strip() for p in prefix.split(",") if p.strip()]
    if not roots:
        roots = [os.path.abspath(os.getcwd())]
    roots.append(os.path.abspath(tempfile.gettempdir()))
    monkeypatch.setattr(settings, "whitelist_path_prefix", ",".join(roots))


class TestPathToRegex:
    def test_path_param_regex(self):
        from app.runtime.collectors.url_resolver import _path_to_regex

        pat = _path_to_regex("/debug/{request_id}")
        assert pat.match("/debug/abc-123") is not None
        assert pat.match("/debug/") is None

    def test_static_path_regex(self):
        from app.runtime.collectors.url_resolver import _path_to_regex

        pat = _path_to_regex("/health")
        assert pat.match("/health") is not None
        assert pat.match("/health/extra") is None

    def test_multiple_params(self):
        from app.runtime.collectors.url_resolver import _path_to_regex

        pat = _path_to_regex("/a/{x}/b/{y}")
        assert pat.match("/a/1/b/2") is not None
        assert pat.match("/a/1/b/2/c") is None


class TestAnalyzeHandler:
    def test_analyze_handler_locates_function(self):
        """analyze_handler 应解析 handler 源码并返回函数级静态分析。"""
        from app.runtime.collectors.static_analyzer import analyze_handler

        # 直接构造一个解析目标：用临时文件模拟源码，验证 analyze_handler 的解析链路
        # 通过 monkeypatch 注入 resolve 返回的端点信息
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(
                "def handle_debug(request_id: str):\n"
                "    data = request_id\n"
                "    return data\n"
            )
            tmp_path = f.name

        fake_endpoint = {"file": tmp_path, "function": "handle_debug", "module": "x"}
        import app.runtime.collectors.url_resolver as ur

        original = ur.resolve
        try:
            ur.resolve = lambda method, path: fake_endpoint

            loc = analyze_handler("GET", "/debug/abc")
            assert loc is not None
            assert loc.function == "handle_debug"
            assert loc.function_info is not None
            assert loc.function_info.name == "handle_debug"
        finally:
            ur.resolve = original
            os.unlink(tmp_path)

    def test_analyze_handler_none_on_missing(self):
        """无命中时返回 None，不抛异常。"""
        from app.runtime.collectors.static_analyzer import analyze_handler

        import app.runtime.collectors.url_resolver as ur

        original = ur.resolve
        try:
            ur.resolve = lambda method, path: None
            assert analyze_handler("GET", "/nope") is None
        finally:
            ur.resolve = original
