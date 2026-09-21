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


def _poll_eval(page: Page, js: str, predicate, timeout_s: float, poll_s: float = 0.25):
    """条件轮询页面表达式直至谓词满足或超时（P2-TEST-2；范式同 _poll_status）。

    返回最后一次取值（超时时为不满足谓词的末值，由调用方断言）。
    注意：表达式必须返回可序列化值（布尔/数字/字符串）——DOM 节点引用
    无法跨越 evaluate 边界，判断存在性时在 JS 侧收敛为布尔。"""
    deadline = time.time() + timeout_s
    value = None
    while True:
        value = page.evaluate(js)
        if predicate(value):
            return value
        if time.time() >= deadline:
            return value
        time.sleep(poll_s)


def _assert_endpoint_matches_base(*, endpoint, base_url: str) -> None:
    """W1 移交项：endpoint 为 window.location.origin，须与实际服务地址匹配。

    「8000 被异构服务占用 → e2e 自起随机空闲端口」场景下若仍硬编码
    http://127.0.0.1:8000，本断言必然假红（W1 场景 B 实测 23 passed /
    1 skipped / 1 failed，唯一失败即此条）。BASE_URL 由 conftest 的
    sync_e2e_base_urls() 在 session 起始重写为实际服务器地址。"""
    assert isinstance(endpoint, str) and endpoint.startswith(base_url), (
        f"endpoint 应与页面实际服务地址 {base_url!r} 匹配，实际 {endpoint!r}"
    )


def _assert_v6_window_observed(*, armed: bool, resolved: bool, window_ms) -> None:
    """V6 观察窗口的证据链：点击必须真实 armed，且窗口必须收口（定时器触发）。"""
    assert armed, (
        "点击未进入 V6 静默失败观察窗口（_getPendingUISilentFailure 始终未 armed）——"
        "「正常点击不误报」的检测前提不成立，检查 UI 钩子 / 检测 arming 链路"
    )
    assert resolved, (
        f"V6 观察窗口（{window_ms}ms）在等待期内未收口（_getPendingUISilentFailure 始终 armed）"
    )


def test_demo_status_panel_visible_after_load(page: Page):
    """初始状态可见：SDK 已初始化、endpoint 展示、面板 DOM 存在且无敏感内容。"""
    status = _load_demo(page)

    assert status["sdkStatus"] == "initialized", f"SDK 初始状态异常: {status}"
    # W1 移交项：endpoint 参数化为实际服务地址（BASE_URL 由 conftest 的
    # sync_e2e_base_urls() 重写；异构占用 8000 的自起随机端口场景不再假红）
    _assert_endpoint_matches_base(endpoint=status.get("endpoint"), base_url=BASE_URL)
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
    """普通成功点击（200 响应）不得被误报为 SilentFailure（条件等待 V6 观察窗口收口）。"""
    _load_demo(page)
    window_ms = page.evaluate("AiDebug._getPublicConfig().uiSilentFailureTimeoutMs")
    assert isinstance(window_ms, (int, float)) and window_ms > 0, (
        f"uiSilentFailureTimeoutMs 应为正数: {window_ms!r}"
    )

    # 真实 DOM 点击：V6 检测由 click 事件 arming；旧实现直接调用 testXhrGet()
    # 绕过 UI 钩子，观察窗口从未真正进入，「正常点击不误报」的前提不成立
    page.click('button[onclick="testXhrGet()"]')  # 页面既有按钮：GET /api/debug/health → 200

    # 条件轮询（P2-TEST-2，替代固定 sleep(2.0)）：
    # ① 点击必须进入 V6 观察窗口；② 窗口收口（定时器触发——正常路径经
    # sawNetwork/DOM 变化取消，误报路径则产生上报）
    armed = _poll_eval(
        page,
        "AiDebug._getPendingUISilentFailure() !== null",
        lambda v: v is True,
        timeout_s=3.0,
        poll_s=0.05,
    )
    resolved = _poll_eval(
        page,
        "AiDebug._getPendingUISilentFailure() === null",
        lambda v: v is True,
        timeout_s=window_ms / 1000 + 5.0,
        poll_s=0.1,
    )
    _assert_v6_window_observed(armed=armed is True, resolved=resolved is True, window_ms=window_ms)

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
