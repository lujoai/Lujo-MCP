"""
Browser SDK V3/V6 端到端联调测试

验证：
1. demo 页面与 SDK 静态文件可无鉴权访问
2. SDK V3 网络错误自动上报全链路（页面触发 → 服务端 → MCP 查询闭环）
3. SDK V6 UI 静默失败自动检测全链路（真实断言：必须真的产出上报）
4. trace_id 贯穿（SDK 生成 → payload → 服务端存储，真实断言）
5. /ingest/batch 批量入库
6. 知识库命中优先返回（受 LLM 环境门禁 + 端点契约限制，见该用例 docstring）

运行方式：
    .venv/Scripts/python.exe -m pytest tests/e2e/test_sdk_full_chain.py -v

前置条件：
    - Playwright 已安装：pip install playwright && playwright install chromium
    - 服务器由 tests/e2e/conftest.py 的 e2e_server fixture 负责：先校验
      127.0.0.1:8000 上对端的身份（本仓库版本 + memory 后端），不合规则自起
      （必要时退到随机空闲端口），并把本模块的 BASE_URL 常量同步到实际地址。
      因此**无需手工启动 uvicorn**（W1 之前需要，此段说明已随之更新）。
"""
import json
import time
import pytest
from playwright.sync_api import sync_playwright, Page, Browser

from app.config import settings

BASE_URL = "http://127.0.0.1:8000"
API_KEY = settings.api_key or "test_secret_key_456"


# ── 断言辅助（P1-TEST-1 / P1-TEST-2 / P1-TEST-6）──
# 抽成独立函数的原因：这些用例此前用 print / 条件断言表达结论，无论功能好坏
# 都是绿的。抽成函数后可以在测试内先对「人为构造的不合规值」调用它、证明它
# 真的会抛 AssertionError（红），再对真实响应调用它（绿）——即断言自检。


def _assert_silent_failure_report(report: dict, trace_id: str) -> None:
    """P1-TEST-1：V6 UI 静默失败上报 payload 的真实断言。"""
    assert "description" in report or "message" in report, (
        f"上报 payload 缺少描述字段（description / message）: {report}"
    )
    assert report.get("trace_id") == trace_id, (
        f"上报 trace_id 与 SDK trace_id 不一致: "
        f"{report.get('trace_id')!r} != {trace_id!r}"
    )
    assert "UI interaction" in (report.get("message") or ""), (
        f"首条上报不是 V6 UI 静默失败（可能被 V3 网络错误上报抢占）: {report}"
    )


def _assert_network_trace_payload(data: dict, sdk_trace_id: str, marker: str) -> None:
    """P1-TEST-2：GET /ingest/network/{trace_id} 响应的真实断言。"""
    assert data.get("found") is True, f"服务端未命中该 trace 的网络记录: {data}"
    records = data.get("records") or []
    assert records, f"found=true 但 records 为空: {data}"
    assert data.get("count") == len(records), f"count 与 records 长度不一致: {data}"
    for record in records:
        assert record.get("trace_id") == sdk_trace_id, (
            f"存储记录的 trace_id 与 SDK trace_id 不一致: "
            f"{record.get('trace_id')!r} != {sdk_trace_id!r}"
        )
    assert any(marker in (record.get("url") or "") for record in records), (
        f"未命中含 {marker!r} 的网络记录；共 {len(records)} 条: {records}"
    )


def _assert_kb_hit_priority(first: dict, second: dict) -> None:
    """P1-TEST-6：KB 命中优先级的无条件断言（先断言前置，再断言结论）。

    原实现把整段断言包在 `if "analysis" in data1` 里，字段缺失时静默跳过；
    改为无条件断言后，前置不成立会直接失败而不是"测了个寂寞"。
    """
    assert "analysis" in first, (
        f"首次响应缺少 analysis 字段，无法据此判定分析来源: {first}"
    )
    assert first.get("analysis_source") == "llm", f"首次应走 LLM 分析: {first}"
    assert first.get("knowledge_base_hit") is False, f"首次不应命中知识库: {first}"
    assert second.get("knowledge_base_hit") is True, f"第二次应命中知识库: {second}"
    assert second.get("analysis_source") == "knowledge_base", f"第二次应来自知识库: {second}"


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
    验证 SDK V6：UI 静默失败自动检测（P1-TEST-1：真实断言，不再只 print）

    步骤：
    1. 打开 /demo/silent-failure 演示页（app/web/silent_failure_demo.html）
    2. 断言 SDK 已 init、UI mutation observer 已安装、trace_id 已生成
    3. 点击 #silentButton（该按钮点击后刻意不更新 UI）
    4. 条件轮询等待 V6 检测窗口产出上报
       （演示页 init 传入 uiSilentFailureTimeoutMs = 1400，SDK 另有 100ms
       观察延迟；轮询窗口取 8s，杜绝固定 sleep 的余量不足问题）
    5. 断言上报真实发生，且 payload 描述字段与 trace_id 都正确

    此前本用例只 print 不断言：不触发、按钮缺失都只打印一行就绿。
    """
    page.goto(f"{BASE_URL}/demo/silent-failure")
    page.wait_for_load_state("networkidle")

    # SDK 加载与初始化（此前只 print，SDK 未 init 也会绿）
    assert page.evaluate("typeof AiDebug !== 'undefined'"), "SDK 未加载"
    assert page.evaluate("AiDebug._inited"), "SDK 未完成 init，V6 检测不可能启用"

    # UI hook 是否安装（此前只 print）
    assert page.evaluate("!!AiDebug._getUIMutationObserver()"), (
        "UI mutation observer 未安装：captureUI / autoDetectUISilentFailures 未生效，"
        "V6 静默失败检测不可能触发"
    )

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

    # 点击 silentButton（明确使用 ID）。此前按钮缺失只 print 一句便跳过，现为硬断言。
    silent_button = page.query_selector("#silentButton")
    assert silent_button is not None, (
        "#silentButton 缺失：演示页结构变更或页面未正确加载，V6 用例无从触发"
    )
    sdk_config = page.evaluate("JSON.stringify(AiDebug._getPublicConfig())")
    silent_button.click()

    # 点击后 V6 必须立刻进入待观察态（pending），否则检测窗口不会产出任何上报
    pending_state = page.evaluate("JSON.stringify(AiDebug._getPendingUISilentFailure() || null)")
    assert pending_state != "null", (
        f"点击后未进入 UI 静默失败待观察态（pending 为空）；config={sdk_config}"
    )

    # 点击必须被页面监听器捕获（此前只 print 条数，0 条也绿）
    ui_events = page.evaluate("window.uiEvents || []")
    assert ui_events, "点击未产生 UI 事件（点击未生效或监听器注册失败）"
    assert (ui_events[-1].get("target") or "") == "silentButton", (
        f"最后一次 UI 事件不是 silentButton: {ui_events[-1]}"
    )

    # 条件轮询等待上报（P2-TEST-2：替换原 0.5s + 2.0s 两段固定 sleep）。
    # 演示页检测窗口 1400ms + SDK 100ms 观察延迟，8s 窗口留足余量。
    deadline = time.time() + 8.0
    reports = []
    while time.time() < deadline:
        reports = page.evaluate("window.silentFailureReports || []")
        if reports:
            break
        time.sleep(0.5)

    assert reports, (
        "V6 未在 8s 内产出 UI 静默失败上报；诊断: "
        f"pending={page.evaluate('JSON.stringify(AiDebug._getPendingUISilentFailure() || null)')}, "
        f"lastDomMutation={page.evaluate('AiDebug._getLastDomMutationAt()')}, "
        f"config={sdk_config}, ui_events={len(ui_events)}"
    )

    # 断言自检（先红后绿）：证明 payload 断言对不合规值确实会失败
    with pytest.raises(AssertionError):
        _assert_silent_failure_report({"message": "unrelated", "trace_id": "bogus"}, trace_id)

    _assert_silent_failure_report(reports[0], trace_id)


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
    验证 trace_id 贯穿：SDK 生成 → payload → 服务端存储（P1-TEST-2：真实断言）

    步骤：
    1. 打开 demo 页面，获取 SDK 生成的 trace_id
    2. 手动上报一条网络记录（URL 带本轮唯一 marker）
    3. 条件轮询 GET /ingest/network/{trace_id}：断言状态码 200、命中本轮 marker，
       且落库记录的 trace_id 与 SDK 生成值逐条一致

    此前本用例对非 200 静默放行、200 时只 print，是零断言用例。
    """
    import uuid

    page.goto(f"{BASE_URL}/demo")
    page.wait_for_load_state("networkidle")

    # 获取 SDK 生成的 trace_id
    sdk_trace_id = page.evaluate("AiDebug.getTraceId()")
    assert sdk_trace_id and sdk_trace_id.startswith("sdk-trace-")

    # 手动上报一条网络记录（URL 带唯一 marker，避免命中历史记录）
    marker = f"e2e-traceid-{uuid.uuid4().hex[:12]}"
    marker_url = f"http://example.com/test-trace-{marker}"
    page.evaluate(
        "AiDebug.reportNetworkError({"
        "method: 'GET', url: " + json.dumps(marker_url) + ", "
        "status: 0, duration_ms: 0, error: 'Network error for trace test'});"
    )

    # 条件轮询（P2-TEST-2：替换原固定 sleep(1)）：SDK 上报 + 服务端入库存在真实
    # 延迟，固定 sleep 既不稳也无谓拖慢；沿用本文件 V3 用例的轮询范式。
    deadline = time.time() + 10.0
    data = None
    while time.time() < deadline:
        resp = page.request.get(
            f"{BASE_URL}/ingest/network/{sdk_trace_id}",
            headers={"X-API-Key": API_KEY}
        )
        # 非 200 此前静默通过，现为硬断言
        assert resp.status == 200, (
            f"GET /ingest/network/{{trace_id}} 应返回 200，实际 {resp.status}: "
            f"{resp.text()[:200]}"
        )
        data = resp.json()
        records = data.get("records") or []
        if data.get("found") and any(marker in (r.get("url") or "") for r in records):
            break
        time.sleep(0.5)

    # 断言自检（先红后绿）：证明上面的响应断言对不合规值确实会失败
    with pytest.raises(AssertionError):
        _assert_network_trace_payload(
            {"found": True, "count": 1, "records": [{"url": marker_url, "trace_id": "bogus"}]},
            sdk_trace_id,
            marker,
        )

    _assert_network_trace_payload(data, sdk_trace_id, marker)


def test_knowledge_base_hit_priority(page: Page):
    """
    验证知识库命中优先返回 + 自动沉淀（P1-TEST-6：无条件断言）

    步骤：
    1. 先通过 /ingest/error 上报一个错误，断言响应带 LLM 分析结果（analysis）
    2. 再次上报相同指纹的错误
    3. 断言第二次命中知识库（knowledge_base_hit=true / analysis_source=knowledge_base）

    改动说明：原实现把全部结论断言包在 `if "analysis" in data1` 里，字段缺失时
    整段静默跳过——用例无论功能好坏都绿。现改为无条件断言：先断言前置成立
    （首次响应必须含 analysis 三字段），再断言结论。

    注意（既有环境门禁，未新增）：需要 LLM 配置才能完整测试，否则跳过。

    ⚠️ 已知契约缺口（待裁定，未在本包抹平）：`POST /ingest/error`
    （app/api/ingest.py → app/mcp/tools/ingest_api.py::tool_ingest_error）只返回
    {trace_id, saved, frame_count}，不含 analysis / analysis_source /
    knowledge_base_hit——这三个字段只在 /api/debug/analyze 的分析链路上产生。
    因此「先断言前置成立」这一步在当前契约下必然失败，详见 W4 执行报告。
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

    # 上报第二个相同错误（第二次应命中知识库）
    resp2 = page.request.post(
        f"{BASE_URL}/ingest/error",
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
        data=json.dumps(payload1)
    )

    assert resp2.status == 200
    data2 = resp2.json()

    # 断言自检（先红后绿）：证明这套断言对不合规响应确实会失败
    with pytest.raises(AssertionError):
        _assert_kb_hit_priority(
            {"analysis": {}, "analysis_source": "knowledge_base", "knowledge_base_hit": True},
            {"analysis": {}, "analysis_source": "llm", "knowledge_base_hit": False},
        )

    # 无条件断言：前置（首次响应须含分析结论）+ 结论（第二次命中知识库）
    _assert_kb_hit_priority(data1, data2)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
