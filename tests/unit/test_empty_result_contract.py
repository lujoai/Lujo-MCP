"""P0-A 工具契约（下半块）：查询工具「无数据」返回体必须携带可操作信息。

金标准是 diagnose_issue 的 found:false 结构（message/setup_hint/next_step）。
本文件锁定以下工具在无数据时新增的契约字段（不抛异常、isError 语义不变，
只在返回 dict 里补字段，宿主 AI 据此能分辨「成功但没数据」的原因与下一步）：

- get_network_trace:      scope + next_step（先经 ingest_network 上报并传 trace_id）
- get_blame_for_frame /
  get_recent_diff:        next_step（GIT_PATH_WHITELIST / 本地存在性语义）
- get_related_specs:      next_step（项目规范扫描与扩展名匹配语义）
- search_logs:            queried_scope + next_step
- list_recent_traces:     queried_scope + next_step
"""
from app.mcp.tools.git_api import tool_get_blame_for_frame, tool_get_recent_diff
from app.mcp.tools.network_api import tool_get_network_trace
from app.mcp.tools.spec_api import tool_get_related_specs
from app.mcp.tools.trace_api import list_recent_traces_handler, search_logs_handler


# ---------------------------------------------------------------------------
# get_network_trace：found:false 时带 scope + next_step
# ---------------------------------------------------------------------------

def test_get_network_trace_empty_includes_scope_and_next_step():
    res = tool_get_network_trace("no-such-trace", session_id="sess-1")
    assert res["found"] is False
    assert res["count"] == 0
    assert res["records"] == []
    assert res["scope"] == {"trace_id": "no-such-trace", "session_id": "sess-1"}
    step = res["next_step"]
    assert "ingest_network" in step
    # 关联语义：只有 ingest_network 传 trace_id 参数，记录才会挂到该 trace 下
    assert "trace_id" in step


def test_get_network_trace_empty_scope_without_session():
    res = tool_get_network_trace("no-such-trace")
    assert res["found"] is False
    assert res["scope"] == {"trace_id": "no-such-trace", "session_id": None}


# ---------------------------------------------------------------------------
# get_blame_for_frame / get_recent_diff：found:false 时带 next_step
# ---------------------------------------------------------------------------

def test_get_blame_for_frame_empty_includes_next_step():
    res = tool_get_blame_for_frame("/no/such/file.py", 1)
    assert res["found"] is False
    assert res["blame"] is None
    step = res["next_step"]
    assert step
    # 路径解析语义：白名单（缺省收敛到本服务进程工作目录）
    assert "GIT_PATH_WHITELIST" in step
    assert "工作目录" in step


def test_get_recent_diff_empty_includes_next_step():
    res = tool_get_recent_diff("/no/such/file.py")
    assert res["found"] is False
    assert res["diff"] is None
    step = res["next_step"]
    assert "GIT_PATH_WHITELIST" in step
    assert "工作目录" in step


# ---------------------------------------------------------------------------
# get_related_specs：count:0 时带 next_step
# ---------------------------------------------------------------------------

def test_get_related_specs_empty_includes_next_step():
    res = tool_get_related_specs("/no/such/file.py")
    assert res["found"] is False
    assert res["count"] == 0
    assert res["specs"] == []
    assert res["next_step"]


# ---------------------------------------------------------------------------
# search_logs / list_recent_traces：count:0 时带 queried_scope + next_step
# ---------------------------------------------------------------------------

def test_search_logs_empty_includes_queried_scope_and_next_step():
    res = search_logs_handler({"keyword": "zz-no-such-keyword-zz", "since_minutes": "7"})
    assert res["count"] == 0
    assert res["results"] == []
    scope = res["queried_scope"]
    assert scope["keyword"] == "zz-no-such-keyword-zz"
    assert scope["since_minutes"] == 7
    assert res["next_step"]


def test_list_recent_traces_empty_includes_queried_scope_and_next_step():
    res = list_recent_traces_handler({"limit": "5"})
    assert res["count"] == 0
    assert res["traces"] == []
    assert res["queried_scope"]["limit"] == 5
    assert res["next_step"]
