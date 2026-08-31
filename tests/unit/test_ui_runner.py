"""单元测试：UI runner 与 verify_ui 工具"""
import pytest

from app.runtime.verifier import ui_runner


class TestUIRunner:

    def test_is_available(self):
        """is_available 不抛异常"""
        assert ui_runner.is_available() in (True, False)

    def test_no_playwright_returns_error(self):
        """未装 Playwright 时返回明确错误"""
        if ui_runner.is_available():
            pytest.skip("Playwright 已安装，跳过")

        result = ui_runner.run_ui_verification({
            "kind": "ui",
            "target": "http://example.com",
            "expect": {},
        })
        assert result["matched"] is False
        assert "playwright 未安装" in result.get("error", "")

    def test_no_target_returns_error(self):
        """无 target 时返回错误"""
        if not ui_runner.is_available():
            pytest.skip("Playwright 未安装，跳过")

        result = ui_runner.run_ui_verification({
            "kind": "ui",
            "target": "",
            "expect": {},
        })
        assert result["matched"] is False
        assert result["diffs"][0]["field"] == "target"

    def test_private_target_returns_structured_security_rejection(self, monkeypatch):
        """私网目标被拒绝时返回结构化 security 与留证信息。"""
        monkeypatch.setattr("app.config.settings.ui_url_allow_private", False)
        monkeypatch.setattr("app.config.settings.ui_url_allowlist", "")

        result = ui_runner.run_ui_verification({
            "kind": "ui",
            "target": "http://127.0.0.1:8765/demo",
            "expect": {},
        })

        assert result["matched"] is False
        assert result["silent_failure"] is False
        assert result["security"]["target"]["allowed"] is False
        assert result["security"]["target"]["rule"] == "private_network"
        assert result["security"]["target"]["private_network"] is True
        assert result["failure_evidence"]["stage"] == "security_check"

    def test_verify_state_adds_text_assertion_failure_evidence(self):
        """文本断言失败时输出结构化断言结果与 failure_evidence。"""

        class FakePage:
            url = "https://example.com/form"

            def text_content(self, selector, timeout=5000):
                assert selector == "#status"
                return "Pending"

        result = ui_runner._verify_state(
            FakePage(),
            {
                "assertions": [
                    {"type": "text", "selector": "#status", "equals": "Ready"}
                ]
            },
            action="click(#submit)",
        )

        assert result["matched"] is False
        assert result["diffs"] == [{
            "field": "click(#submit).text",
            "expected": "Ready",
            "actual": "Pending",
        }]
        assert result["assertions"][0]["type"] == "text"
        assert result["assertions"][0]["matched"] is False
        assert result["failure_evidence"]["stage"] == "assertion"
        assert result["failure_evidence"]["selector"] == "#status"

    def test_form_assertion_success(self):
        """表单断言成功时返回正确结果。"""

        class FakePage:
            def query_selector(self, selector):
                assert selector == "#user-form"
                return FakeFormElement()

        class FakeFormElement:
            def query_selector_all(self, selectors):
                if selectors == "input, textarea, select":
                    return [FakeInput("name", "John Doe"), FakeInput("email", "john@example.com"), FakeInput("age", "30")]

        class FakeInput:
            def __init__(self, name, value):
                self.name = name
                self.value = value

            def get_attribute(self, attr):
                if attr == "name":
                    return self.name
                elif attr == "id":
                    return self.name
                elif attr == "placeholder":
                    return self.name
                return None

            def input_value(self):
                return self.value

            @property
            def tag_name(self):
                return "input"

        result = ui_runner._evaluate_business_assertion(
            FakePage(),
            {
                "type": "form",
                "selector": "#user-form",
                "values": {
                    "name": "John Doe",
                    "email": "john@example.com",
                    "age": "30"
                }
            },
            action="fill_form"
        )

        assert result["type"] == "form"
        assert result["matched"] is True
        assert result["expected"] == {"name": "John Doe", "email": "john@example.com", "age": "30"}
        assert result["actual"]["name"] == "John Doe"
        assert result["actual"]["email"] == "john@example.com"
        assert result["actual"]["age"] == "30"
        assert result["diff"] is None

    def test_form_assertion_failure(self):
        """表单断言失败时返回正确错误信息。"""

        class FakePage:
            def query_selector(self, selector):
                assert selector == "#user-form"
                return FakeFormElement()

        class FakeFormElement:
            def query_selector_all(self, selectors):
                if selectors == "input, textarea, select":
                    return [FakeInput("name", "Jane Doe"), FakeInput("email", "jane@example.com"), FakeInput("age", "25")]

        class FakeInput:
            def __init__(self, name, value):
                self.name = name
                self.value = value

            def get_attribute(self, attr):
                if attr == "name":
                    return self.name
                elif attr == "id":
                    return self.name
                elif attr == "placeholder":
                    return self.name
                return None

            def input_value(self):
                return self.value

            @property
            def tag_name(self):
                return "input"

        result = ui_runner._evaluate_business_assertion(
            FakePage(),
            {
                "type": "form",
                "selector": "#user-form",
                "values": {
                    "name": "John Doe",
                    "email": "john@example.com",
                    "age": "30"
                }
            },
            action="fill_form"
        )

        assert result["type"] == "form"
        assert result["matched"] is False
        assert result["expected"] == {"name": "John Doe", "email": "john@example.com", "age": "30"}
        assert result["actual"]["name"] == "Jane Doe"
        assert result["actual"]["email"] == "jane@example.com"
        assert result["actual"]["age"] == "25"
        assert result["diff"] is not None
        assert result["diff"]["details"][0]["field"] == "name"
        assert result["diff"]["details"][0]["expected"] == "John Doe"
        assert result["diff"]["details"][0]["actual"] == "Jane Doe"

    def test_data_table_assertion_success(self):
        """数据表格断言成功时返回正确结果。"""

        class FakePage:
            def query_selector(self, selector):
                assert selector == "#data-table"
                return FakeTableElement()

        class FakeTableElement:
            def query_selector_all(self, selector):
                if selector == "tr":
                    # 返回4个tr元素：1个表头 + 3个数据行
                    return [FakeTrElement(is_header=True)] + [FakeTrElement(is_header=False) for _ in range(3)]
                elif selector == "th":
                    return [FakeThElement("Name"), FakeThElement("Age"), FakeThElement("City")]
                else:
                    return []

        class FakeTrElement:
            def __init__(self, is_header):
                self.is_header = is_header

        class FakeThElement:
            def __init__(self, text):
                self.text = text

            def text_content(self):
                return self.text

        result = ui_runner._evaluate_business_assertion(
            FakePage(),
            {
                "type": "data_table",
                "selector": "#data-table",
                "rows": 3,
                "columns": 3,
                "headers": ["Name", "Age", "City"]
            },
            action="validate_table"
        )

        assert result["type"] == "data_table"
        assert result["matched"] is True
        assert result["expected"]["rows"] == 3
        assert result["expected"]["columns"] == 3
        assert result["actual"]["rows"] == 3
        assert result["actual"]["columns"] == 3
        assert result["diff"] is None

    def test_numeric_range_assertion_success(self):
        """数值范围断言成功时返回正确结果。"""

        class FakePage:
            def text_content(self, selector, timeout=5000):
                if selector == "#price":
                    return "$29.99"
                elif selector == "#rating":
                    return "4.5"
                elif selector == "#quantity":
                    return "100"
                return ""

        result = ui_runner._evaluate_business_assertion(
            FakePage(),
            {
                "type": "numeric_range",
                "selector": "#quantity",
                "min": 50,
                "max": 200
            },
            action="validate_quantity"
        )

        assert result["type"] == "numeric_range"
        assert result["matched"] is True
        assert result["expected"]["min"] == 50
        assert result["expected"]["max"] == 200
        assert result["actual"] == 100.0
        assert result["diff"] is None

    def test_numeric_range_assertion_failure(self):
        """数值范围断言失败时返回正确错误信息。"""

        class FakePage:
            def text_content(self, selector, timeout=5000):
                if selector == "#price":
                    return "$29.99"
                elif selector == "#rating":
                    return "4.5"
                elif selector == "#quantity":
                    return "250"  # 超出范围
                return ""

        result = ui_runner._evaluate_business_assertion(
            FakePage(),
            {
                "type": "numeric_range",
                "selector": "#quantity",
                "min": 50,
                "max": 200
            },
            action="validate_quantity"
        )

        assert result["type"] == "numeric_range"
        assert result["matched"] is False
        assert result["expected"]["min"] == 50
        assert result["expected"]["max"] == 200
        assert result["actual"] == 250.0
        assert result["diff"] is not None


class TestVerifyUITool:

    def test_no_spec_returns_error(self):
        """不传 spec/spec_id 返回错误"""
        from app.mcp.tools.verify_ui_api import verify_ui_handler

        result = verify_ui_handler({})
        assert result["matched"] is False
        assert "must provide" in result["error"]

    def test_wrong_kind_returns_error(self):
        """非 ui kind 返回错误"""
        from app.mcp.tools.verify_ui_api import verify_ui_handler

        result = verify_ui_handler({
            "spec": {"kind": "api", "target": "GET /x", "expect": {}},
        })
        assert result["matched"] is False
        assert "must be 'ui'" in result["error"]

    def test_verify_ui_with_spec_id_not_found(self):
        """spec_id 不存在时返回错误"""
        from app.mcp.tools.verify_ui_api import verify_ui_handler

        result = verify_ui_handler({
            "spec_id": "no-such-spec",
        })
        assert result["matched"] is False
        assert "must provide" in result["error"]

    def test_registered(self):
        """verify_ui 已注册"""
        from app.mcp.protocol.server import _tool_registry
        from app.mcp.tools import register_all_tools
        register_all_tools()
        assert "verify_ui" in _tool_registry
        tool = _tool_registry["verify_ui"]
        assert callable(tool["handler"])
        assert tool["inputSchema"] is not None


class TestVerifyUIEndpoint:

    def test_verify_ui_endpoint_no_spec(self):
        """POST /api/debug/verify/ui 无参数返回错误"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.debug import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        resp = client.post("/api/debug/verify/ui", json={})
        # 可能返回 200（业务错误）或 422（校验失败）
        assert resp.status_code in (200, 422)


# ---------------------------------------------------------------------------
# FIX(v0.7.1-b12-1): SSRF 守卫放行浏览器内部 scheme（data/blob/about）
# ---------------------------------------------------------------------------


class _FakeRoute:
    def __init__(self, url):
        self.request = type("_Req", (), {"url": url})()
        self.continued = False
        self.aborted = False

    def continue_(self):
        self.continued = True

    def abort(self):
        self.aborted = True


class _FakeContext:
    def __init__(self):
        self.handler = None

    def route(self, pattern, handler):
        self.handler = handler


def _install_and_get_handler():
    ctx = _FakeContext()
    ui_runner._install_ssrf_guard(ctx)
    return ctx.handler


def test_ssrf_guard_allows_browser_internal_schemes():
    """data:/blob:/about: 不发起网络请求、不构成 SSRF，应放行（continue_）。"""
    handler = _install_and_get_handler()
    for url in ("data:image/png;base64,xxxx", "blob:https://example.com/abc", "about:blank"):
        route = _FakeRoute(url)
        handler(route)
        assert route.continued is True, f"{url} 应放行"
        assert route.aborted is False, f"{url} 不应被 abort"


def test_ssrf_guard_still_blocks_private_http():
    """http 内网地址仍被 abort（SSRF 防护不被内部 scheme 放行削弱）。"""
    handler = _install_and_get_handler()
    route = _FakeRoute("http://127.0.0.1:8080/secret")
    handler(route)
    assert route.aborted is True
