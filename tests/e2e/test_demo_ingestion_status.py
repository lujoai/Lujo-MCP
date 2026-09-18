"""M3-B：Demo 页面接入状态可见化 E2E。

验证 /demo 页面的接入状态面板（window.__lujoDemo）由真实证据驱动：
- SDK 初始化状态（AiDebug._inited）；
- "已发送"仅由 SDK 回调（onSilentFailureReport / onNetworkCapture）确认；
- "服务端已收到并可查询"仅由页面自身经公开 MCP /mcp 查询命中 marker 确认，
  不由前端 captureCount 推断；
- 查询失败（HTTP 错误 / 空结果 / 超时）必须显示明确失败原因，不得显示成功；
- 普通成功点击（200 响应）不误报 SilentFailure；
- 页面 DOM 不出现 API Key / Authorization 等敏感内容。
"""
import json
import time
import uuid

import pytest
from playwright.sync_api import sync_playwright, Page, Browser

from app.config import settings

BASE_URL = "http://127.0.0.1:8000"
API_KEY = settings.api_key or "test_secret_key_456"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture(scope="module")
def page(browser: Browser):
    page = browser.new_page()
    yield page
    page.close()


def _load_demo(page: Page) -> str:
    """加载 /demo 并返回页面生成的唯一 marker 探针句柄。"""
    page.goto(f"{BASE_URL}/demo")
    page.wait_for_load_state("networkidle")
    status = page.evaluate("window.__lujoDemo ? window.__lujoDemo.getStatus() : null")
    assert status is not None, (
        "页面缺少接入状态面板（window.__lujoDemo 未实现）——M3-B 状态可见化未落地"
    )
    return status


def _poll_status(page: Page, predicate, timeout_s: float = 20.0) -> dict:
    """条件轮询页面状态直至满足谓词或超时（禁固定长 sleep）。"""
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = page.evaluate("window.__lujoDemo.getStatus()")
        if predicate(last):
            return last
        time.sleep(0.4)
    return last


def test_demo_status_panel_visible_after_load(page: Page):
    """初始状态可见：SDK 已初始化、endpoint 展示、面板 DOM 存在且无敏感内容。"""
    status = _load_demo(page)

    assert status["sdkStatus"] == "initialized", f"SDK 初始状态异常: {status}"
    assert status["endpoint"].startswith("http://127.0.0.1:8000"), (
        f"endpoint 未展示或错误: {status}"
    )
    assert status["queryStatus"] in ("idle",), f"初始查询状态应为 idle: {status}"
    assert status["sent"] is None, f"初始不应有已发送事件: {status}"

    panel = page.query_selector("#ingestion-status")
    assert panel is not None, "页面缺少 #ingestion-status 状态面板元素"
    assert panel.is_visible(), "状态面板不可见"

    dom = page.content()
    assert "test_secret_key_456" not in dom, "页面 DOM 出现测试 API Key"
    assert "Authorization" not in dom, "页面 DOM 出现 Authorization 头"


def test_demo_status_probe_lifecycle_sent_then_queried(page: Page):
    """触发受控网络错误：状态经历 已发送(SDK 回调) → 服务端已收到并可查询(MCP 命中)。"""
    _load_demo(page)

    marker = f"e2e-m3b-{uuid.uuid4().hex[:12]}"
    page.evaluate(f"window.__lujoDemo.probeNetworkError('{marker}')")

    sent = _poll_status(page, lambda s: s["sent"] is not None, timeout_s=10.0)
    assert sent["sent"] is not None, f"10s 内未见 SDK 发送回调证据: {sent}"
    assert sent["sent"].get("marker") == marker, f"发送状态 marker 不匹配: {sent}"
    assert sent["queryStatus"] in ("querying", "queried", "failed"), (
        f"发送后查询状态异常: {sent}"
    )

    queried = _poll_status(
        page,
        lambda s: s["queryStatus"] in ("queried", "failed"),
        timeout_s=25.0,
    )
    assert queried["queryStatus"] == "queried", (
        f"应查询成功而非失败: {queried}"
    )
    assert queried["marker"] == marker, f"查询状态 marker 不匹配: {queried}"
    assert (queried["recordCount"] or 0) >= 1, (
        f"MCP 命中记录数应 ≥ 1: {queried}"
    )


def test_demo_status_failure_when_mcp_http_error(page: Page):
    """MCP 查询 HTTP 500：必须显示失败与原因，不得显示成功。"""
    _load_demo(page)

    marker = f"e2e-m3b-fail-{uuid.uuid4().hex[:8]}"

    def fail_mcp(route):
        route.fulfill(status=500, body="mocked server error")

    page.route("**/mcp", fail_mcp)
    try:
        page.evaluate(f"window.__lujoDemo.probeNetworkError('{marker}')")
        final = _poll_status(
            page,
            lambda s: s["queryStatus"] == "failed",
            timeout_s=25.0,
        )
    finally:
        page.unroute("**/mcp")

    assert final["queryStatus"] == "failed", f"HTTP 500 时必须显示失败: {final}"
    reason = final.get("queryReason") or ""
    assert "MCP 查询失败" in reason, f"失败原因应可理解: {reason}"
    assert final["marker"] == marker


def test_demo_status_failure_when_server_returns_no_record(page: Page):
    """MCP 200 但查询不到本次记录：显示“服务端未返回记录”，不得显示成功。"""
    _load_demo(page)

    marker = f"e2e-m3b-empty-{uuid.uuid4().hex[:8]}"

    def empty_tools_call(route):
        request = route.request.post_data or ""
        try:
            payload = json.loads(request)
        except ValueError:
            route.continue_()
            return
        if payload.get("method") == "tools/call":
            body = json.dumps({
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": {
                    "content": [{"type": "text", "text": json.dumps({
                        "found": False, "count": 0, "records": [],
                    })}],
                    "isError": False,
                },
            }).encode("utf-8")
            route.fulfill(
                status=200,
                body=body,
                headers={"content-type": "application/json"},
            )
            return
        route.continue_()

    page.route("**/mcp", empty_tools_call)
    try:
        page.evaluate(f"window.__lujoDemo.probeNetworkError('{marker}')")
        final = _poll_status(
            page,
            lambda s: s["queryStatus"] == "failed",
            timeout_s=25.0,
        )
    finally:
        page.unroute("**/mcp")

    assert final["queryStatus"] == "failed", f"空结果时必须显示失败: {final}"
    reason = final.get("queryReason") or ""
    assert "服务端未返回记录" in reason, f"失败原因应指明空结果: {reason}"
    assert final["marker"] == marker


def test_demo_status_normal_click_not_reported_as_silent_failure(page: Page):
    """普通成功点击（200 响应）不得被误报为 SilentFailure。"""
    _load_demo(page)

    page.evaluate("testXhrGet()")  # 页面既有按钮逻辑：GET /api/debug/health → 200
    time.sleep(2.0)  # V6 观察窗口（uiSilentFailureTimeoutMs≈1.4s）+ 上报间隔

    status = page.evaluate("window.__lujoDemo.getStatus()")
    assert (status.get("silentFailureCount") or 0) == 0, (
        f"普通成功点击被误报为静默失败: {status}"
    )
    assert status["queryStatus"] != "failed", (
        f"普通点击不应触发查询失败状态: {status}"
    )


def test_demo_status_no_secrets_during_probe(page: Page):
    """探针全流程中页面 DOM 不得出现 API Key / Authorization / 未脱敏标记。"""
    _load_demo(page)

    marker = f"e2e-m3b-secret-{uuid.uuid4().hex[:8]}"
    page.evaluate(f"window.__lujoDemo.probeNetworkError('{marker}')")
    _poll_status(page, lambda s: s["queryStatus"] in ("queried", "failed"), timeout_s=25.0)

    dom = page.content()
    assert "test_secret_key_456" not in dom, "DOM 泄漏测试 API Key"
    assert "Authorization" not in dom, "DOM 泄漏 Authorization 头"
    assert "supersecret" not in dom, "DOM 泄漏未脱敏标记"
