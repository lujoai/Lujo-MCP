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

    def test_literal_regex_metachars_escaped(self):
        """FIX(v0.7.1-b2-9) 回归：模板字面段的正则元字符必须转义。

        旧实现 /v1.2/ 的 ``.`` 匹配任意字符，/v1x2/ 等不相关路径被误命中。
        """
        from app.runtime.collectors.url_resolver import _path_to_regex

        pat = _path_to_regex("/api/v1.2/items")
        assert pat.match("/api/v1.2/items") is not None  # 正确路径命中
        assert pat.match("/api/v1x2/items") is None  # 元字符不再泛匹配

        # 带路径参数 + 元字符混合
        pat2 = _path_to_regex("/v1.0/{item_id}")
        assert pat2.match("/v1.0/abc") is not None
        assert pat2.match("/v1x0/abc") is None


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


class TestResolveExactBeatsTemplate:
    """FIX: 参数化模板路由不得「截胡」同前缀的精确路由。

    旧实现在单次循环中先遇到 /items/{id} 即正则命中 /items/search，
    返回参数化 handler 而非专门处理 /items/search 的 handler。
    """

    def _fake_route(self, path: str, methods, endpoint_name: str):
        from fastapi.routing import APIRoute

        async def _ep():  # pragma: no cover - 仅作路由对象占位
            return None

        _ep.__name__ = endpoint_name
        return APIRoute(path=path, endpoint=_ep, methods=list(methods))

    def test_exact_route_wins_over_template_prefix(self, monkeypatch):
        from app.runtime.collectors import url_resolver

        template_first = self._fake_route("/items/{item_id}", {"GET"}, "get_item")
        exact_second = self._fake_route("/items/search", {"GET"}, "search_items")

        original_describe = url_resolver._describe_endpoint
        try:
            def _describe(endpoint):
                return {"function": endpoint.__name__}

            # FastAPI.routes 是只读 property，改其内部 router.routes 列表
            from app.main import app

            monkeypatch.setattr(app.router, "routes", [template_first, exact_second])
            monkeypatch.setattr(url_resolver, "_describe_endpoint", _describe)

            result = url_resolver.resolve("GET", "/items/search")
            assert result is not None
            assert result["function"] == "search_items", (
                "精确路由 /items/search 被参数化模板 /items/{item_id} 截胡"
            )

            # 参数化路径仍然能匹配到模板 handler
            result_param = url_resolver.resolve("GET", "/items/abc-123")
            assert result_param is not None
            assert result_param["function"] == "get_item"
        finally:
            url_resolver._describe_endpoint = original_describe
