"""5 个标准 BenchmarkCase（手写结构化 fixture，Phase 3 D6）。

- 不依赖真实运行时采集，保证可复现、可离线运行。
- `lujo_context` 保持 `build_debug_context` 的字段契约（exception / code_snippets /
  network_trace / ui_events / git_blame / recent_diffs / runtime / spec_diffs ...）。
- 后续新增真实案例时，可在此追加 BenchmarkCase。
"""

from __future__ import annotations

from benchmark.schemas import BenchmarkCase


# ── Case 1：接口 500 错误 ────────────────────────────────────────────
_CASE_API_500 = BenchmarkCase(
    case_id="api_500_none_attribute",
    title="接口 500 错误：订单提交报 AttributeError",
    category="api_error",
    user_description=(
        "POST /api/orders 提交订单时返回 500，前端显示『服务器内部错误』，"
        "没有任何具体报错信息，多用户复现。"
    ),
    lujo_context={
        "trace_id": "api-500-0001",
        "trace_kind": "exception",
        "exception": {
            "type": "AttributeError",
            "message": "'NoneType' object has no attribute 'user_id'",
            "frames": [
                {
                    "file": "app/services/order_service.py",
                    "line": 142,
                    "function": "create_order",
                    "context": ["user = request.user", "if user.is_authenticated:"],
                },
                {
                    "file": "app/api/orders.py",
                    "line": 56,
                    "function": "create",
                },
            ],
            "frame_count": 2,
        },
        "code_snippets": [
            {
                "file": "app/services/order_service.py",
                "line": 142,
                "function": "create_order",
                "found": True,
                "error_line": 142,
                "link": "vscode://file/app/services/order_service.py:142",
            }
        ],
        "request": {
            "method": "POST",
            "path": "/api/orders",
            "body": {"items": [{"sku": "p1", "qty": 2}]},
        },
        "runtime": None,
        "fault_localization": {
            "suspicious_frames": [
                {"file": "app/services/order_service.py", "line": 142, "function": "create_order"},
            ],
            "likely_cause_candidate": "create_order",
        },
    },
    expected_root_cause=(
        "order_service.create_order 访问 request.user 时 user 为 None（请求未附带"
        "已认证用户上下文），访问 user.user_id 触发 AttributeError，未做判空。"
    ),
    expected_evidence=[
        "exception.type == AttributeError",
        "堆栈定位到 app/services/order_service.py:142",
        "请求体正常但缺 user 认证上下文",
    ],
)


# ── Case 2：前端白屏 ─────────────────────────────────────────────────
_CASE_FRONTEND_BLANK = BenchmarkCase(
    case_id="frontend_blank_fetch_error",
    title="前端白屏：bootstrap 接口失败被吞",
    category="frontend_blank",
    user_description=(
        "打开页面就是白屏，控制台没有明显报错，用户说『页面加载不出来，一片白』。"
    ),
    lujo_context={
        "trace_id": "fe-blank-0001",
        "trace_kind": "silent_failure",
        "exception": None,
        "ui_events": [
            {"event_type": "load", "target": "window", "timestamp": "2026-08-11T10:00:01Z"},
            {"event_type": "error", "target": "bodystart", "timestamp": "2026-08-11T10:00:01Z"},
        ],
        "console": [
            {"level": "error", "message": "Uncaught (in promise) fetch failed"},
        ],
        "network_trace": [
            {
                "method": "GET",
                "url": "/api/bootstrap",
                "status": 500,
                "duration_ms": 1200,
            }
        ],
        "code_snippets": [],
        "runtime": None,
        "fault_localization": None,
    },
    expected_root_cause=(
        "页面初始化依赖 /api/bootstrap，该接口返回 500 导致前端 bootstrap 数据拿不到，"
        "初始化异常被 try/catch 静默吞掉，形成白屏。"
    ),
    expected_evidence=[
        "network_trace 中 /api/bootstrap 返回 500",
        "console 中 Uncaught fetch failed",
        "ui_events 中 load 事件后无渲染数据",
    ],
)


# ── Case 3：数据库异常 ───────────────────────────────────────────────
_CASE_DB_ERROR = BenchmarkCase(
    case_id="db_error_null_column",
    title="数据库异常：增量导入因 NOT NULL 列失败",
    category="db_error",
    user_description=(
        "批量导入用户时出错，事务回滚，报错『Null value in column』，"
        "不知道是哪一列、哪条数据。"
    ),
    lujo_context={
        "trace_id": "db-err-0001",
        "trace_kind": "exception",
        "exception": {
            "type": "IntegrityError",
            "message": "Null value in column \"phone\" violates not-null constraint",
            "frames": [
                {
                    "file": "app/services/import_service.py",
                    "line": 88,
                    "function": "import_users",
                }
            ],
            "frame_count": 1,
        },
        "recent_diffs": [
            {
                "file": "migrations/0042_users_phone_not_null.py",
                "author": "alice",
                "summary": "users.phone 改为 NOT NULL 且未设默认值",
            }
        ],
        "git_blame": [
            {"file": "app/services/import_service.py", "line": 88, "author": "bob"},
        ],
        "trace": [
            {"step": "sql", "sql": "INSERT INTO users (phone, name) VALUES (?, ?)"},
        ],
        "code_snippets": [],
        "runtime": None,
        "fault_localization": None,
    },
    expected_root_cause=(
        "migration 0042 将 users.phone 改为 NOT NULL 且未设默认值，存量数据 phone 为空，"
        "导入时 INSERT 违反 not-null 约束；根因是最近一次 schema 变更而非数据本身。"
    ),
    expected_evidence=[
        "recent_diffs 中 master users.phone NOT NULL 变更",
        "trace 中 INSERT 语句",
        "IntegrityError 指向 phone 列",
    ],
)


# ── Case 4：权限错误 ─────────────────────────────────────────────────
_CASE_AUTH_403 = BenchmarkCase(
    case_id="auth_403_role_missing",
    title="权限错误：管理接口返回 403（角色不匹配）",
    category="auth_403",
    user_description=(
        "我登录了也能正常访问普通接口，但调用管理接口返回 403，"
        "『我有权限啊为什么不让调』。"
    ),
    lujo_context={
        "trace_id": "auth-403-0001",
        "trace_kind": "exception",
        "exception": None,
        "network_trace": [
            {
                "method": "GET",
                "url": "/api/admin/users",
                "status": 403,
                "duration_ms": 5,
            }
        ],
        "request": {
            "method": "GET",
            "path": "/api/admin/users",
            "headers": {"Authorization": "Bearer <token>"},
        },
        "auth_context": {
            "user_id": "u-42",
            "role": "viewer",
            "authenticated": True,
        },
        "spec_diffs": [
            {
                "matched": False,
                "silent_failure": False,
                "requirement": "仅 admin 角色可访问 /api/admin/*",
            }
        ],
        "related_specs": [
            {"id": "spec-rbac", "title": "RBAC 角色权限规范"},
        ],
        "runtime": None,
        "fault_localization": None,
    },
    expected_root_cause=(
        "接口 /api/admin/users 要求 admin 角色（RBAC 规范），而当前用户角色为 viewer，"
        "角色不匹配导致 403；非登录问题，而是角色分配/权限配置问题。"
    ),
    expected_evidence=[
        "network_trace 中 /api/admin/users 返回 403",
        "auth_context 中 role == viewer",
        "spec_diffs 中要求 admin 角色",
    ],
)


# ── Case 5：性能问题 ─────────────────────────────────────────────────
_CASE_PERF = BenchmarkCase(
    case_id="perf_slow_nplus1",
    title="性能问题：列表接口 N+1 查询变慢",
    category="perf_slow",
    user_description=(
        "首页列表接口响应从 300ms 变成 3s 以上，『最近没改这块，怎么就慢了呢』。"
    ),
    lujo_context={
        "trace_id": "perf-0001",
        "trace_kind": "trace",
        "exception": None,
        "network_trace": [
            {
                "method": "GET",
                "url": "/api/items",
                "status": 200,
                "duration_ms": 3200,
            }
        ],
        "runtime": {
            "process": {"pid": 1234, "num_threads": 40, "memory_rss_mb": 512},
            "system": {"cpu_percent": 65.0, "db_connection_count": 200},
        },
        "recent_diffs": [
            {
                "file": "app/services/item_service.py",
                "summary": "列表查询改为循环内逐条查库（引入 N+1）",
                "author": "carol",
            }
        ],
        "trace": [
            {"step": "sql", "sql": "SELECT * FROM items"},
            {"step": "sql", "sql": "SELECT * FROM item_detail WHERE item_id=?"},
            {"step": "sql", "sql": "SELECT * FROM item_detail WHERE item_id=?"},
            {"step": "sql", "sql": "SELECT * FROM item_detail WHERE item_id=?"},
        ],
        "code_snippets": [],
        "fault_localization": None,
    },
    expected_root_cause=(
        "item_service 列表查询 recent_diffs 引入循环内逐条查库（N+1 查询），"
        "对 N 个 item 各执行一次 detail 查询，导致单请求 3.2s。"
    ),
    expected_evidence=[
        "trace 中重复的 SELECT FROM item_detail（N+1 模式）",
        "recent_diffs 中循环内查询变更",
        "network_trace 中 /api/items 耗时 3200ms",
    ],
)


# ── Case 6：前端 minified 堆栈 Source Map 还原（v0.5.1）──────────────

# minified 帧与还原后的原始帧（还原后帧指向真实源码 src/orders/checkout.ts）
_FRONTEND_MINIFIED_FRAMES = [
    {
        "file": "https://cdn.example.com/static/js/app.9f3b2c.js",
        "line": 1,
        "column": 48213,
        "function": "t",
    },
    {
        "file": "https://cdn.example.com/static/js/app.9f3b2c.js",
        "line": 1,
        "column": 92176,
        "function": "a",
    },
]
_FRONTEND_RESOLVED_FRAMES = [
    {
        "file": "src/orders/checkout.ts",
        "line": 87,
        "column": 12,
        "function": "submitOrder",
        "resolved": True,
        "original": {
            "file": "https://cdn.example.com/static/js/app.9f3b2c.js",
            "line": 1,
            "column": 48213,
        },
    },
    {
        "file": "src/api/client.ts",
        "line": 23,
        "column": 5,
        "function": "postJson",
        "resolved": True,
        "original": {
            "file": "https://cdn.example.com/static/js/app.9f3b2c.js",
            "line": 1,
            "column": 92176,
        },
    },
]

# 还原前（无 source map）：Debug Context 只有 minified 帧，源码片段全部 miss
_FRONTEND_CONTEXT_BEFORE = {
    "trace_id": "fe-sm-0001",
    "trace_kind": "exception",
    "exception": {
        "type": "TypeError",
        "message": "Cannot read properties of undefined (reading 'price')",
        "frames": _FRONTEND_MINIFIED_FRAMES,
        "frame_count": 2,
    },
    "code_snippets": [],
    "runtime": None,
    "fault_localization": None,
}

# 还原后（source map 命中）：resolved_frames + 原始源码片段 + 故障定位候选
_FRONTEND_CONTEXT_AFTER = {
    **_FRONTEND_CONTEXT_BEFORE,
    "resolved_frames": _FRONTEND_RESOLVED_FRAMES,
    "code_snippets": [
        {
            "file": "src/orders/checkout.ts",
            "error_line": 87,
            "found": True,
            "snippet": ">>> 87: const total = items.reduce((s, i) => s + i.price, 0);",
            "link": None,
        },
        {
            "file": "src/api/client.ts",
            "error_line": 23,
            "found": True,
            "snippet": ">>> 23: return fetch(url, { method: 'POST', body: JSON.stringify(data) });",
            "link": None,
        },
    ],
    "fault_localization": {
        "suspicious_frames": [
            {"file": "src/orders/checkout.ts", "line": 87, "function": "submitOrder"},
        ],
        "likely_cause_candidate": "submitOrder",
    },
}

_CASE_FRONTEND_SOURCEMAP = BenchmarkCase(
    case_id="frontend_minified_sourcemap",
    title="前端 minified 堆栈：Source Map 还原前后对比",
    category="frontend_sourcemap",
    user_description=(
        "生产环境点击『提交订单』报 TypeError: Cannot read properties of undefined "
        "(reading 'price')，堆栈是压缩后的 app.9f3b2c.js:1:48213，看不到源码位置。"
    ),
    lujo_context=_FRONTEND_CONTEXT_AFTER,
    expected_root_cause=(
        "submitOrder（src/orders/checkout.ts:87）对 items 里的元素直接读 .price，"
        "某个 item 为 undefined（后端返回的列表中含空元素），未做判空导致 TypeError。"
    ),
    expected_evidence=[
        "resolved_frames 中 submitOrder @ src/orders/checkout.ts:87",
        "code_snippets 中 checkout.ts:87 的 reduce 读取 i.price",
        "original 字段保留 minified 原位置（app.9f3b2c.js:1:48213）可对账",
    ],
)


def frontend_sourcemap_ab() -> dict[str, dict]:
    """返回 Source Map 还原前后的两份 Debug Context（A/B 对照）。

    用途：QualityScorer 旁证评分对比（解析前 vs 解析后完整度提升），
    以及 Benchmark 主评分的额外对照组。纯数据，无 I/O。
    """
    return {"before": dict(_FRONTEND_CONTEXT_BEFORE), "after": dict(_FRONTEND_CONTEXT_AFTER)}


# 全部 6 个标准 Case
BENCHMARK_CASES: list[BenchmarkCase] = [
    _CASE_API_500,
    _CASE_FRONTEND_BLANK,
    _CASE_DB_ERROR,
    _CASE_AUTH_403,
    _CASE_PERF,
    _CASE_FRONTEND_SOURCEMAP,
]

# by case_id 索引
BENCHMARK_INDEX: dict[str, BenchmarkCase] = {c.case_id: c for c in BENCHMARK_CASES}


def get_case(case_id: str) -> BenchmarkCase | None:
    """按 case_id 取单个 Case，不存在返回 None。"""
    return BENCHMARK_INDEX.get(case_id)


def list_cases() -> list[BenchmarkCase]:
    """返回全部 Case。"""
    return list(BENCHMARK_CASES)


__all__ = ["BENCHMARK_CASES", "BENCHMARK_INDEX", "get_case", "list_cases", "frontend_sourcemap_ab"]
