"""单元测试：diagnose_issue 冷启动闭环（v0.9.8）。

背景（dogfooding 实证）：宿主 AI 首次调用 diagnose_issue 时存储为空（冷启动），
旧的「暂无数据」返回让宿主直接放弃本服务器。本文件锁定升级后的契约：

1. 完全无上报：found=false，next_step 含 auto_test 降级指引（保留 setup_hint）
2. 存储里有历史页面 URL（console/network 上报）：next_step 变成可直接执行的
   auto_test 精确调用（含真实 URL）——无错误 trace 且带会话过滤的查询是
   「有上报记录但无错误现场」的真实路径（errors 缓冲与存储摘要均不命中）
3. 会话隔离：指定其他 session_id 时不得泄漏别的会话 URL
4. 多条历史 URL 取最新；真实错误 trace 仍 found=true（回归保护）

说明：无会话过滤时，仅 console/network 记录会经 list_recent_traces 存储摘要
命中 found=true 路径（builder 存储兜底合成最小上下文，既有行为），因此 URL
提取对「无会话查询」以 _latest_page_url 单元级锁定。
"""
import json
import time

import pytest

from app.mcp.protocol.jsonrpc import JSONRPCRequest
from app.mcp.protocol.server import _handle_tools_call
from app.mcp.tools import register_all_tools
from app.mcp.tools.diagnose_api import _latest_page_url


@pytest.fixture(autouse=True)
def _registered_tools():
    register_all_tools()
    # memory trace 存储是进程级单例：每个用例前重置为全新 memory 后端，
    # 保证「冷启动」语义不被前序测试残留污染（errors._recent 由 conftest 清）。
    from app.runtime.core.storage import factory as _storage_factory

    _storage_factory._trace_store = None
    yield
    _storage_factory._trace_store = None


async def _call_diagnose(arguments: dict | None = None) -> dict:
    req = JSONRPCRequest(
        id="cold-1",
        method="tools/call",
        params={"name": "diagnose_issue", "arguments": arguments or {}},
    )
    resp = await _handle_tools_call(req)
    assert resp.get("error") is None, f"协议层报错: {resp.get('error')}"
    return json.loads(resp["result"]["content"][0]["text"])


# ── 完全无上报（任务项 3：setup_hint 保留 + 补降级句） ────────────────────


@pytest.mark.asyncio
async def test_cold_start_empty_storage_next_step_guides_auto_test():
    """完全无上报：next_step 含 auto_test 降级指引，setup_hint 保留。"""
    result = await _call_diagnose({})

    assert result["found"] is False
    assert result["message"]
    assert result["setup_hint"], "SDK 接入指引必须保留"
    assert "auto_test" in result["next_step"]
    assert "若用户描述了具体页面" in result["next_step"]


@pytest.mark.asyncio
async def test_cold_start_empty_storage_with_session_same_guidance():
    """带会话的完全无上报：同样给 auto_test 降级指引（不泄漏任何 URL）。"""
    result = await _call_diagnose({"session_id": "sess-none"})

    assert result["found"] is False
    assert "auto_test" in result["next_step"]
    assert "若用户描述了具体页面" in result["next_step"]


# ── 有历史页面 URL（任务项 2：可执行的精确 auto_test 指引） ────────────────


@pytest.mark.asyncio
async def test_cold_start_with_stored_url_next_step_contains_url():
    """有上报记录但无错误现场（会话过滤查询）：next_step 含真实 URL 的精确调用。"""
    from app.runtime.core.trace_repo import save_network_record

    page_url = "http://localhost:5173/settings/profile"
    save_network_record(
        {"method": "GET", "url": page_url, "status_code": 200},
        trace_id="sdk-trace-cold-url",
        session_id="sess-a",
    )

    result = await _call_diagnose({"session_id": "sess-a"})

    assert result["found"] is False
    assert page_url in result["next_step"]
    assert "auto_test" in result["next_step"]
    # 可直接执行的调用形态：max_actions 参数随指引给出
    assert '"max_actions": 10' in result["next_step"]


@pytest.mark.asyncio
async def test_cold_start_console_extra_url_end_to_end():
    """会话内 console error 现按故障信号返回桶级现场（会话故障识别契约）。

    语义变更说明（2026-09-28 修复批次）：本用例曾断言 console error 只进
    冷启动 URL 指引（found=false）；自「会话内 network/console 故障识别」
    生效起，console error 是该会话内的真实故障信号，直接以桶级现场返回，
    冷启动指引仅保留给「范围内确无故障信号」的查询（空存储 / 仅健康遥测
    用例继续锁定该契约）。
    """
    from app.runtime.core.trace_repo import save_console_log

    save_console_log("error", "boom", extra={"url": "http://localhost:3000/checkout"},
                     session_id="sess-c")

    result = await _call_diagnose({"session_id": "sess-c"})

    assert result["found"] is True
    assert result.get("granularity") == "bucket"
    assert [c.get("message") for c in result.get("console_logs") or []] == ["boom"]
    assert result.get("evidence_trust") == "untrusted"


@pytest.mark.asyncio
async def test_cold_start_url_session_isolation():
    """会话隔离：B 会话查询不得拿到 A 会话的 URL（回退为无 URL 降级指引）。"""
    from app.runtime.core.trace_repo import save_network_record

    save_network_record(
        {"method": "GET", "url": "http://localhost:5173/session-a-page"},
        trace_id="sdk-trace-cold-sess",
        session_id="sess-a",
    )

    other = await _call_diagnose({"session_id": "sess-b"})
    own = await _call_diagnose({"session_id": "sess-a"})

    assert other["found"] is False
    assert "session-a-page" not in other["next_step"]
    assert "auto_test" in other["next_step"]
    assert own["found"] is False
    assert "session-a-page" in own["next_step"]


# ── _latest_page_url 提取规则（单元级） ───────────────────────────────────


def test_latest_page_url_none_on_empty_storage():
    """空存储：返回 None。"""
    assert _latest_page_url() is None


def test_latest_page_url_network_direct_field():
    """network 记录的 data.url 直存字段可提取。"""
    from app.runtime.core.trace_repo import save_network_record

    save_network_record(
        {"method": "GET", "url": "http://xdirect/a"}, trace_id="sdk-trace-url-dir"
    )
    assert _latest_page_url() == "http://xdirect/a"


def test_latest_page_url_latest_wins():
    """多条历史 URL：取最新一条（按条目时间戳）。"""
    from app.runtime.core.trace_repo import save_network_record

    save_network_record(
        {"method": "GET", "url": "http://old.example.com/page"},
        trace_id="sdk-trace-cold-old",
    )
    time.sleep(0.01)
    save_network_record(
        {"method": "GET", "url": "http://new.example.com/page"},
        trace_id="sdk-trace-cold-new",
    )

    assert _latest_page_url() == "http://new.example.com/page"


def test_latest_page_url_session_filter():
    """提取同样受会话过滤：指定会话时缺失/异会话归属不可见。"""
    from app.runtime.core.trace_repo import save_network_record

    save_network_record(
        {"method": "GET", "url": "http://x sess-a"},
        trace_id="sdk-trace-url-sess",
        session_id="sess-a",
    )

    assert _latest_page_url("sess-a") == "http://x sess-a"
    assert _latest_page_url("sess-b") is None


# ── 回归保护 ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cold_start_real_error_still_found_true():
    """有真实错误 trace 时仍返回 found=true，冷启动逻辑不误伤正常路径。"""
    from app.runtime.core.trace_repo import save_trace

    error_id = save_trace(
        exc_type="TypeError",
        message="cold start regression",
        frames=[{"file": "src/a.js", "line": 1, "function": "f"}],
    )

    result = await _call_diagnose({})

    assert result["found"] is True
    assert result["trace_id"] == error_id
