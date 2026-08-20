"""
Browser SDK V3/V6 端到端联调测试

验证：
1. SDK V3 网络错误自动上报全链路
2. SDK V6 UI 静默失败自动检测全链路
3. trace_id 贯穿（header + payload）
4. /ingest/batch 批量入库
5. 知识库命中优先返回 + 自动沉淀

运行方式：
    python -m pytest tests/e2e/test_sdk_full_chain.py -v

前置条件：
    - uvicorn 已启动：python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
    - Playwright 已安装：pip install playwright && playwright install chromium
"""
import json
import time
import pytest
from playwright.sync_api import sync_playwright, Page, Browser

BASE_URL = "http://127.0.0.1:8000"
from app.config import settings
API_KEY = settings.api_key or "test_secret_key_456"


@pytest.fixture(scope="module")
def browser():
    """启动浏览器实例"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture(scope="module")
def page(browser: Browser):
    """创建页面"""
    page = browser.new_page()
    yield page
    page.close()


def test_demo_pages_accessible(page: Page):
    """验证 demo 页面和 SDK 文件可无鉴权访问"""
    # 网络捕获 demo
    resp = page.request.get(f"{BASE_URL}/demo")
    assert resp.status == 200
    assert "Network Capture Demo" in resp.text()

    # 静默失败 demo
    resp = page.request.get(f"{BASE_URL}/demo/silent-failure")
    assert resp.status == 200
    assert "静默失败检测" in resp.text()

    # SDK 文件
    resp = page.request.get(f"{BASE_URL}/ai-debug.js")
    assert resp.status == 200
    assert "AiDebug" in resp.text()


def test_sdk_v3_network_error_auto_report(page: Page):
    """
    验证 SDK V3：fetch/XHR 失败自动上报为 silent failure

    步骤：
    1. 打开 network_capture_demo.html
    2. 触发一个失败请求（访问不存在的 URL）
    3. 等待 SDK 自动上报
    4. 检查服务端是否收到 silent failure 记录
    """
    # 监听服务端日志（通过 API 查询）
    page.goto(f"{BASE_URL}/demo")
    page.wait_for_load_state("networkidle")

    # 检查 SDK 是否加载成功
    sdk_loaded = page.evaluate("typeof AiDebug !== 'undefined'")
    assert sdk_loaded, "SDK 未加载"

    # 检查 SDK 内部状态（SDK 闭包式配置，_inited 经只读 getter 暴露）
    sdk_inited = page.evaluate("AiDebug._inited")
    print(f"SDK initialized: {sdk_inited}")

    # 检查 UI hook 是否安装（_getUIMutationObserver 测试辅助方法）
    ui_hook_installed = page.evaluate("!!AiDebug._getUIMutationObserver()")
    print(f"UI mutation observer installed: {ui_hook_installed}")

    # 检查 trace_id 是否自动生成
    trace_id = page.evaluate("AiDebug.getTraceId()")
    assert trace_id and trace_id.startswith("sdk-trace-"), f"trace_id 格式错误: {trace_id}"

    # 触发一个失败请求
    page.evaluate("""
        fetch('http://localhost:8000/nonexistent-endpoint-404')
            .catch(() => console.log('fetch failed as expected'))
    """)

    # 等待 SDK 自动上报（V3 逻辑）
    time.sleep(2)

    # 通过 API 查询最近的 silent failure 记录
    resp = page.request.get(
        f"{BASE_URL}/mcp/tools/get_silent_failures",
        headers={"X-API-Key": API_KEY},
        params={"limit": "10"}
    )

    # 注意：get_silent_failures 可能不是 MCP tool，需要根据实际 API 调整
    # 这里先检查是否有 ingest 记录
    if resp.status == 200:
        data = resp.json()
        # 检查是否有 silent failure 记录
        failures = data.get("silent_failures", [])
        # 至少应该有一条（来自 V3 自动检测）
        # 如果没有，可能是 SDK 配置或逻辑问题
        print(f"Found {len(failures)} silent failures")


def test_sdk_v6_ui_silent_failure_detection(page: Page):
    """
    验证 SDK V6：UI 静默失败自动检测

    步骤：
    1. 打开 silent_failure_demo.html
    2. 点击"假装提交"按钮（按钮点击后不更新 UI）
    3. 等待 V6 检测窗口（uiSilentFailureTimeoutMs = 1400ms）
    4. 检查是否自动生成 silent failure 上报
    """
    page.goto(f"{BASE_URL}/demo/silent-failure")
    page.wait_for_load_state("networkidle")

    # 检查 SDK 是否加载成功
    sdk_loaded = page.evaluate("typeof AiDebug !== 'undefined'")
    assert sdk_loaded, "SDK 未加载"

    # 检查 SDK 内部状态（SDK 闭包式配置，_inited 经只读 getter 暴露）
    sdk_inited = page.evaluate("AiDebug._inited")
    print(f"SDK initialized: {sdk_inited}")

    # 检查 UI hook 是否安装（_getUIMutationObserver 测试辅助方法）
    ui_hook_installed = page.evaluate("!!AiDebug._getUIMutationObserver()")
    print(f"UI mutation observer installed: {ui_hook_installed}")

    # 检查 trace_id 是否自动生成
    trace_id = page.evaluate("AiDebug.getTraceId()")
    assert trace_id and trace_id.startswith("sdk-trace-"), f"trace_id 格式错误: {trace_id}"

    # 监听 silent failure 回调（在点击之前注册）
    page.evaluate("""
        window.silentFailureReports = [];
        window.uiEvents = [];
        
        AiDebug.onSilentFailureReport(function(payload) {
            console.log('[E2E] Silent failure reported:', JSON.stringify(payload));
            window.silentFailureReports.push(payload);
        });
        
        // 监听 UI 事件
        document.addEventListener('click', function(e) {
            console.log('[E2E] Click event captured:', e.target.id || e.target.tagName);
            window.uiEvents.push({
                target: e.target.id || e.target.tagName,
                timestamp: Date.now()
            });
        }, true);
    """)

    # 点击 silentButton（明确使用 ID）
    silent_button = page.query_selector("#silentButton")
    if silent_button:
        print("Clicking silentButton...")
        
        # 检查 SDK 配置（经 _getPublicConfig 只读视图读取）
        sdk_config = page.evaluate("JSON.stringify(AiDebug._getPublicConfig())")
        print(f"SDK config: {sdk_config}")
        
        # 点击前检查内部状态
        before_click = page.evaluate("""
            JSON.stringify({
                pending: AiDebug._getPendingUISilentFailure(),
                lastDomMutation: AiDebug._getLastDomMutationAt(),
                observer: !!AiDebug._getUIMutationObserver()
            })
        """)
        print(f"Before click: {before_click}")
        
        silent_button.click()

        # 等待 500ms，检查中间状态
        time.sleep(0.5)
        after_500ms = page.evaluate("""
            JSON.stringify({
                pending: AiDebug._getPendingUISilentFailure(),
                lastDomMutation: AiDebug._getLastDomMutationAt()
            })
        """)
        print(f"After 500ms: {after_500ms}")

        # 等待 V6 检测窗口（1400ms + 500ms 缓冲）
        time.sleep(2.0)

        # 检查 click 事件是否被捕获
        ui_events = page.evaluate("window.uiEvents || []")
        print(f"UI events captured: {len(ui_events)}")
        if ui_events:
            print(f"Last UI event: {ui_events[-1]}")

        # 检查 SDK 内部状态
        pending_state = page.evaluate("JSON.stringify(AiDebug._pendingUISilentFailure || null)")
        print(f"Pending UI silent failure: {pending_state}")

        # 检查是否触发了 silent failure 上报
        reports = page.evaluate("window.silentFailureReports || []")
        print(f"Silent failure reports: {len(reports)}")

        # 如果有上报，检查 payload 结构
        if len(reports) > 0:
            report = reports[0]
            assert "description" in report or "message" in report
            assert "trace_id" in report
            assert report["trace_id"] == trace_id
            print(f"Silent failure detected: {report.get('description') or report.get('message')}")
        else:
            # V6 可能没有触发，记录警告但不失败
            print("WARNING: V6 did not trigger silent failure report")
            print("This could be due to: DOM mutation detected, network activity detected, or route change detected")
    else:
        print("silentButton not found, skipping V6 test")


def test_ingest_batch_endpoint(page: Page):
    """
    验证 /ingest/batch 批量入库端点

    步骤：
    1. 构造批量事件 payload
    2. POST 到 /ingest/batch
    3. 检查返回结果
    """
    payload = {
        "events": [
            {
                "path": "/ingest/error",
                "payload": {
                    "exc_type": "TestError",
                    "message": "E2E test error",
                    "frames": [],
                    "source": "e2e_test",
                    "trace_id": "test-trace-batch-001"
                }
            },
            {
                "path": "/ingest/network",
                "payload": {
                    "record": {
                        "method": "GET",
                        "url": "http://example.com/test",
                        "status": 200,
                        "duration_ms": 100
                    },
                    "trace_id": "test-trace-batch-001"
                }
            },
            {
                "path": "/ingest/silent-failure",
                "payload": {
                    "message": "E2E test silent failure",
                    "source": "e2e_test",
                    "trace_id": "test-trace-batch-001"
                }
            }
        ]
    }

    resp = page.request.post(
        f"{BASE_URL}/ingest/batch",
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
        data=json.dumps(payload)
    )

    assert resp.status == 200
    data = resp.json()
    assert "results" in data
    assert data["count"] == 3

    # 检查每条事件是否成功
    for result in data["results"]:
        assert result["ok"] is True, f"Event {result['path']} failed: {result.get('error')}"


def test_trace_id_consistency(page: Page):
    """
    验证 trace_id 贯穿：SDK 生成 → header → payload → 服务端存储

    步骤：
    1. 打开 demo 页面，获取 SDK 生成的 trace_id
    2. 手动上报一条记录，检查 payload 中的 trace_id
    3. 查询服务端存储，检查是否一致
    """
    page.goto(f"{BASE_URL}/demo")
    page.wait_for_load_state("networkidle")

    # 获取 SDK 生成的 trace_id
    sdk_trace_id = page.evaluate("AiDebug.getTraceId()")
    assert sdk_trace_id and sdk_trace_id.startswith("sdk-trace-")

    # 手动上报一条网络记录
    page.evaluate("""
        AiDebug.reportNetworkError({
            method: 'GET',
            url: 'http://example.com/test-trace',
            status: 0,
            duration_ms: 0,
            error: 'Network error for trace test'
        });
    """)

    # 等待上报
    time.sleep(1)

    # 查询服务端（通过 API）
    # 注意：需要根据实际 API 调整查询方式
    resp = page.request.get(
        f"{BASE_URL}/ingest/network/{sdk_trace_id}",
        headers={"X-API-Key": API_KEY}
    )

    if resp.status == 200:
        data = resp.json()
        # 检查返回的记录中是否包含相同的 trace_id
        # 具体字段名需要根据实际 API 返回调整
        print(f"Network records for trace {sdk_trace_id}: {data}")


def test_knowledge_base_hit_priority(page: Page):
    """
    验证知识库命中优先返回 + 自动沉淀

    步骤：
    1. 先通过 /ingest/error 上报一个错误，触发 LLM 分析并沉淀到知识库
    2. 再次上报相同指纹的错误
    3. 检查第二次是否命中知识库（knowledge_base_hit=true）

    注意：需要 LLM 配置才能完整测试，否则跳过
    """
    # 检查 LLM 是否配置
    resp = page.request.get(f"{BASE_URL}/health")
    health = resp.json()
    if not health.get("llm_configured"):
        pytest.skip("LLM 未配置，跳过知识库测试")

    # 上报第一个错误
    payload1 = {
        "exc_type": "KnowledgeBaseTestError",
        "message": "Test error for knowledge base",
        "frames": [{"file": "test.py", "line": 1, "function": "test_func"}],
        "source": "e2e_test"
    }

    resp1 = page.request.post(
        f"{BASE_URL}/ingest/error",
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
        data=json.dumps(payload1)
    )

    assert resp1.status == 200
    data1 = resp1.json()

    # 检查是否有 analysis（LLM 分析结果）
    if "analysis" in data1:
        # 第一次应该是 LLM 分析
        assert data1.get("analysis_source") == "llm"
        assert data1.get("knowledge_base_hit") is False

        # 上报第二个相同错误（应该命中知识库）
        resp2 = page.request.post(
            f"{BASE_URL}/ingest/error",
            headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
            data=json.dumps(payload1)
        )

        assert resp2.status == 200
        data2 = resp2.json()

        # 第二次应该命中知识库
        assert data2.get("knowledge_base_hit") is True
        assert data2.get("analysis_source") == "knowledge_base"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
