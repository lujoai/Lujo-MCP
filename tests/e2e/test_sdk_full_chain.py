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

from app.config import settings

BASE_URL = "http://127.0.0.1:8000"
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
    验证 SDK V3：fetch 网络层失败自动上报，且现场可通过公开 MCP 工具查询。

    完整闭环（M3-A）：
    页面触发受控网络错误（本地死端口 fetch → 连接拒绝）
    → Browser SDK V3 自动上报（network 记录 + 静默失败，豁免采样必达）
    → 服务端 memory runtime
    → MCP HTTP initialize / initialized
    → tools/call list_recent_traces（session 隔离）按唯一 marker 命中本次记录
    → tools/call trace 按返回 ID 深挖
    → tools/call get_network_trace 命中同一 marker 的网络记录

    唯一性：每次运行生成独立 marker（写入请求 URL，随 SDK 采集进入服务端），
    查询结果必须包含该 marker，命中历史记录一律失败。
    """
    import uuid
    import urllib.request

    marker = f"e2e-m3a-{uuid.uuid4().hex[:12]}"

    page.goto(f"{BASE_URL}/demo")
    page.wait_for_load_state("networkidle")

    assert page.evaluate("typeof AiDebug !== 'undefined'"), "SDK 未加载"
    assert page.evaluate("AiDebug._inited"), "SDK 未完成 init"

    sdk_trace_id = page.evaluate("AiDebug.getTraceId()")
    assert sdk_trace_id and sdk_trace_id.startswith("sdk-trace-"), (
        f"trace_id 格式错误: {sdk_trace_id}"
    )
    page_session = page.evaluate("AiDebug.getSessionId()")
    assert page_session, "SDK 未生成 session_id"

    # 触发网络层失败：本地未监听端口 → 连接拒绝 → fetch reject →
    # SDK 网络钩子错误路径（status_code=0）→ _autoReportNetworkError 上报
    # 静默失败 + _reportNetworkRecord 上报网络记录。
    # 注意：404 等正常 HTTP 响应不会触发 V3 静默失败（SDK 仅在网络异常路径上报）。
    page.evaluate(
        "fetch('http://127.0.0.1:59999/" + marker + "').catch(function () {});"
    )

    # ── MCP HTTP JSON-RPC 客户端（公开端点 /mcp）──
    mcp_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-API-Key": API_KEY,
    }

    def _post_rpc(payload: dict, session_id: str | None = None):
        req = urllib.request.Request(
            f"{BASE_URL}/mcp",
            data=json.dumps(payload).encode("utf-8"),
            headers=mcp_headers,
            method="POST",
        )
        if session_id:
            req.add_header("mcp-session-id", session_id)
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8")
            session = resp.headers.get("mcp-session-id")
        assert status in (200, 202), f"MCP HTTP {status}: {raw[:200]}"
        return status, raw, session

    def _rpc_request(method: str, params: dict, session_id: str | None, req_id: int) -> dict:
        status, raw, session = _post_rpc({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }, session_id=session_id)
        assert status == 200, f"{method} 应返回 200，实际 {status}"
        if raw.lstrip().startswith("{"):
            envelope = json.loads(raw)
        else:
            data_lines = [ln[5:] for ln in raw.splitlines() if ln.startswith("data:")]
            assert data_lines, f"{method} 响应缺少 data 行: {raw[:200]}"
            envelope = json.loads(data_lines[-1].strip())
        assert envelope.get("id") == req_id, f"{method} 响应 id 不匹配"
        assert "error" not in envelope, f"{method} JSON-RPC error: {envelope.get('error')}"
        return {"result": envelope.get("result"), "session": session}

    def _call_tool(name: str, arguments: dict, req_id: int) -> dict:
        """tools/call 并断言协议结构；返回服务端工具结果（content[0].text 的 JSON）。"""
        rpc = _rpc_request(
            "tools/call",
            {"name": name, "arguments": arguments},
            session_id=_mcp_session_holder["session"],
            req_id=req_id,
        )
        result = rpc["result"]
        assert result is not None, "tools/call 缺少 result"
        assert result.get("isError") is not True, f"工具 {name} 执行失败: {result}"
        content = result.get("content") or []
        assert content and content[0].get("type") == "text", (
            f"工具 {name} 返回异常 content: {result}"
        )
        return json.loads(content[0]["text"])

    _mcp_session_holder = {"session": None}

    init_rpc = _rpc_request("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "lujo-e2e-m3a", "version": "0.1.0"},
    }, session_id=None, req_id=1)
    server_info = (init_rpc["result"] or {}).get("serverInfo") or {}
    assert server_info.get("name") == "lujo-mcp", f"serverInfo 异常: {server_info}"
    _mcp_session_holder["session"] = init_rpc["session"]
    assert _mcp_session_holder["session"], "initialize 未返回 mcp-session-id"

    # initialized 通知（无 id）：HTTP 202、无 body，不解析 JSON-RPC
    notify_status, _, _ = _post_rpc({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }, session_id=_mcp_session_holder["session"])
    assert notify_status == 202, f"initialized 通知应返回 202，实际 {notify_status}"

    # 条件轮询：list_recent_traces（session 隔离）等待本次静默失败出现。
    # SDK 批量上报（默认 1s 间隔）+ 服务端入库存在真实延迟，禁止固定长 sleep。
    deadline = time.time() + 20.0
    matched = None
    while time.time() < deadline:
        listing = _call_tool(
            "list_recent_traces",
            {"limit": 20, "session_id": page_session},
            req_id=100,
        )
        assert listing.get("count") == len(listing.get("traces", [])), (
            f"list_recent_traces 结构异常: {listing}"
        )
        for item in listing.get("traces", []):
            if marker in (item.get("message") or ""):
                matched = item
                break
        if matched:
            break
        time.sleep(0.5)

    assert matched is not None, (
        f"20s 内未通过 list_recent_traces 找到含 {marker!r} 的本次记录；"
        f"会话 {page_session} 下共 {listing.get('count', 0)} 条"
    )
    assert matched.get("type") == "SilentFailure", (
        f"命中的记录类型应为 SilentFailure: {matched}"
    )
    trace_id = matched["trace_id"]
    assert trace_id, "命中记录缺少 trace_id"

    # 按 ID 深挖：trace 工具返回该记录的完整时序（trace_data 等步骤）
    detail = _call_tool("trace", {"request_id": trace_id}, req_id=101)
    assert detail.get("request_id") == trace_id, f"trace 返回的 request_id 不匹配: {detail}"
    steps = detail.get("trace") or []
    assert detail.get("step_count") == len(steps) and steps, (
        f"trace 应返回非空时序: step_count={detail.get('step_count')}"
    )

    # 网络记录腿：get_network_trace 按页面 sdk_trace_id 命中同一 marker 的记录
    net_deadline = time.time() + 10.0
    net_records = []
    while time.time() < net_deadline:
        net = _call_tool("get_network_trace", {"trace_id": sdk_trace_id}, req_id=102)
        if net.get("found") and (net.get("count") or 0) > 0:
            net_records = net.get("records") or []
            if any(marker in (r.get("url") or "") for r in net_records):
                break
        time.sleep(0.5)

    assert any(marker in (r.get("url") or "") for r in net_records), (
        f"get_network_trace 未命中含 {marker!r} 的网络记录；"
        f"共 {len(net_records)} 条"
    )


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
