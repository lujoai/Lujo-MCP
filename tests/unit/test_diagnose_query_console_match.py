"""diagnose_issue query 关键词对桶级 console 信号的消息匹配契约测试。

背景（v1.0.1 验收矩阵 B/F 用例实证 gap）：query 模式下，关键词对 error
实体匹配 type/message，但对桶级故障信号只匹配 signal 的 summary/kind
（_enumerate_fault_candidates 的 deferred_signals 分支）。console error
信号的 summary 固定为 "console error"（_bucket_fault_signal 内
_short_summary(["console error"])），不含消息文本——「只有 console error、
无异常实体」的现场用 query=<报错文本> 查不到（返回 not_found），而宿主 AI
最自然的行为恰是拿报错文本当关键词查。

本文件锁定修复后的契约：
1. 桶内合格 console error 条目（step=console、level=error、时间窗内、
   session 归属匹配——与 _bucket_fault_signal 故障判定同口径过滤）的
   message 参与 query 关键词匹配（小写包含，口径与 error 实体一致：
   kw in message.lower()），命中任一即保留该桶级候选；
2. summary/kind 既有匹配口径、network 信号匹配、无参模式候选枚举行为
   均不变（以候选数与 kind 断言锁定）；
3. level=info 的普通 console 日志不参与匹配（健康遥测不得伪命中）；
4. 新匹配路径同样受 session_id 归属过滤约束。

所有写入经真实生产入库路径（tool_ingest_console / save_network_record）；
查询断言直接走 diagnose_api.handler（无参 / query 模式业务入口）。
"""
import uuid

import pytest

from app.mcp.tools import console_api
from app.mcp.tools.diagnose_api import _enumerate_fault_candidates, handler
from app.runtime.core.trace_repo import save_network_record


def _marker(tag: str) -> str:
    """独特 marker（不含会被存储边界脱敏误伤的键值形态）。"""
    return f"QUERYGAP-MARKER-{tag}-{uuid.uuid4().hex[:8]}_boom"


# ── 修复目标：query=<console error 消息文本> 能找到现场（先红） ─────────────


@pytest.mark.asyncio
async def test_query_matches_console_error_message_text():
    """console error 条目的 message 文本参与 query 匹配：唯一命中返回现场。"""
    marker = _marker("core")
    console_api.tool_ingest_console(level="error", message=marker)

    result = handler({"query": marker, "since_minutes": 0})

    assert result["found"] is True, (
        f"query=<console error 消息文本> 应命中现场: {result.get('message')}"
    )
    assert result["source"] == "query"
    assert result["trace_id"]


@pytest.mark.asyncio
async def test_query_console_message_match_respects_session_isolation():
    """新增的 message 匹配路径同样受会话归属过滤约束（候选枚举层锁定）。

    说明：handler 层「带 session 的 query 命中桶级候选」的完整返回还受
    _finish → build_debug_context 的 R5 会话兜底限制（console/network-only
    桶在带会话查询下构不成上下文，network 信号同样如此，属既有行为），
    不属于本工作包的匹配口径范畴，故此处锁定枚举层的会话过滤。
    """
    marker = _marker("sess")
    console_api.tool_ingest_console(level="error", message=marker, session_id="sess-a")

    own, complete_own = _enumerate_fault_candidates("sess-a", 0, keyword=marker)
    other, complete_other = _enumerate_fault_candidates("sess-b", 0, keyword=marker)

    assert complete_own is True and complete_other is True
    assert len(own) == 1
    assert own[0]["kind"] == "console_error"
    assert other == [], "message 匹配不得看到其他会话的条目"


# ── 守卫：既有口径与无参行为不回归 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_unknown_word_still_not_found():
    """query 一个不存在的词仍 not_found（含 setup_hint/next_step）。"""
    console_api.tool_ingest_console(level="error", message="boom happened once")

    result = handler({"query": "绝不存在的关键词xyz", "since_minutes": 0})

    assert result["found"] is False
    assert result["setup_hint"]
    assert result["next_step"]


@pytest.mark.asyncio
async def test_query_ignores_info_level_console_log():
    """level=info 的普通 console 日志不参与关键词匹配（健康遥测不伪命中）。"""
    marker = _marker("info")
    console_api.tool_ingest_console(level="info", message=marker)

    result = handler({"query": marker, "since_minutes": 0})

    assert result["found"] is False


@pytest.mark.asyncio
async def test_no_arg_mode_bucket_signal_response_unchanged():
    """无参模式既有行为不变：唯一桶级 console 信号直接返回桶级现场。"""
    console_api.tool_ingest_console(level="error", message="no-arg boom")

    result = handler({})

    assert result["found"] is True
    assert result.get("granularity") == "bucket"
    assert [c.get("message") for c in result.get("console_logs") or []] == ["no-arg boom"]


def test_enumerate_candidates_count_and_kind_unchanged():
    """候选枚举行为锁定：无 kw 全量；summary/kind 命中既有口径不变；
    message 命中是新增的第三条命中路径。"""
    console_api.tool_ingest_console(level="error", message="alpha boom one")
    console_api.tool_ingest_console(level="error", message="beta boom two")

    all_cands, complete = _enumerate_fault_candidates(None, 0)
    assert complete is True
    assert len(all_cands) == 2
    assert {c["kind"] for c in all_cands} == {"console_error"}
    assert all(c["granularity"] == "bucket" for c in all_cands)

    # 既有口径 1：summary（"console error"）命中，两条桶级候选都保留
    by_summary, _ = _enumerate_fault_candidates(None, 0, keyword="console error")
    assert len(by_summary) == 2

    # 既有口径 2：kind（"console_error"）命中，两条桶级候选都保留
    by_kind, _ = _enumerate_fault_candidates(None, 0, keyword="console_error")
    assert len(by_kind) == 2

    # 新增口径：message 文本命中，只保留命中的那个桶
    by_message, complete_kw = _enumerate_fault_candidates(None, 0, keyword="alpha")
    assert complete_kw is True
    assert len(by_message) == 1
    assert by_message[0]["kind"] == "console_error"


@pytest.mark.asyncio
async def test_query_network_signal_matching_unchanged():
    """network 信号匹配不受本次扩展影响：kind 命中仍保留，未命中不进候选。"""
    save_network_record(
        {"method": "POST", "url": "http://x/api/login", "status_code": 500},
        trace_id=None,
    )

    hit = handler({"query": "network", "since_minutes": 0})
    assert hit["found"] is True

    miss = handler({"query": "绝不存在的关键词xyz", "since_minutes": 0})
    assert miss["found"] is False
