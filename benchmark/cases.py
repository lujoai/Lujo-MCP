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
        "页面初始化依赖 /api/bootstrap，该接口返回 500 导致 bootstrap 数据拿不到，"
        "初始化链路抛出 fetch 异常，页面未渲染出内容形成白屏。"
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
                "commits_back": 2,
                "diff": (
                    "--- a/migrations/0042_users_phone_not_null.py\n"
                    "+++ b/migrations/0042_users_phone_not_null.py\n"
                    "@@ -1,4 +1,4 @@\n"
                    " ALTER TABLE users\n"
                    "-    ALTER COLUMN phone DROP NOT NULL;\n"
                    "+    ALTER COLUMN phone SET NOT NULL;\n"
                ),
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
        "导入时 INSERT 违反 not-null 约束。"
    ),
    expected_evidence=[
        "recent_diffs 中 migration 0042 对 users.phone 的 DDL 变更",
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
                "commits_back": 1,
                "diff": (
                    "--- a/app/services/item_service.py\n"
                    "+++ b/app/services/item_service.py\n"
                    "@@ -31,6 +31,9 @@ def list_items(self, item_ids):\n"
                    "     items = Item.objects.filter(id__in=item_ids)\n"
                    "-    details = ItemDetail.objects.filter(item_id__in=item_ids)\n"
                    "+    for item in items:\n"
                    "+        item.detail = ItemDetail.objects.get(item_id=item.id)\n"
                    "     return items\n"
                ),
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
        "item_service 列表查询在循环内逐条查库（N+1 查询），"
        "对 N 个 item 各执行一次 detail 查询，导致单请求 3.2s。"
    ),
    expected_evidence=[
        "trace 中重复的 SELECT FROM item_detail（N+1 模式）",
        "recent_diffs 中 item_service.py 的循环查询变更",
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
        "某个 item 为 undefined（列表内含空元素），未做判空导致 TypeError。"
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


# ── Case 7：WebSocket 长连接空闲断开（扩展批 2026-09-26，来源：KB 连接断开家族）──
_CASE_WS_IDLE_DROP = BenchmarkCase(
    case_id="ws_connection_idle_drop",
    title="WebSocket 长连接约 60 秒必断开重连",
    category="connection_drop",
    user_description=(
        "实时通知面板连上 WebSocket 后大约一分钟就断开，然后自动重连，"
        "反复循环，后端日志一条报错都翻不到。"
    ),
    lujo_context={
        "trace_id": "ws-drop-0001",
        "trace_kind": "silent_failure",
        "exception": None,
        "console": [
            {"level": "warn", "message": "WebSocket closed (code=1006), reconnecting..."},
            {"level": "info", "message": "WebSocket connected"},
            {"level": "warn", "message": "WebSocket closed (code=1006), reconnecting..."},
        ],
        "network_trace": [
            {"method": "GET", "url": "wss://app.example.com/ws/notifications", "status": 101, "duration_ms": 61190},
            {"method": "GET", "url": "wss://app.example.com/ws/notifications", "status": 101, "duration_ms": 60210},
        ],
        "ui_events": [
            {"event_type": "websocket_open", "target": "notifications", "timestamp": "2026-09-26T09:00:00Z"},
            {"event_type": "websocket_close", "target": "notifications", "timestamp": "2026-09-26T09:01:01Z"},
            {"event_type": "websocket_open", "target": "notifications", "timestamp": "2026-09-26T09:01:02Z"},
            {"event_type": "websocket_close", "target": "notifications", "timestamp": "2026-09-26T09:02:02Z"},
        ],
        "recent_diffs": [
            {
                "file": "web/realtime/socket.ts",
                "commits_back": 1,
                "diff": (
                    "--- a/web/realtime/socket.ts\n"
                    "+++ b/web/realtime/socket.ts\n"
                    "@@ -10,6 +10,8 @@\n"
                    "+export function connect() {\n"
                    "+  const ws = new WebSocket(url);\n"
                    "+  ws.onclose = () => setTimeout(connect, 1000);\n"
                    "+}\n"
                    "# 无 ping/heartbeat 实现"
                ),
            }
        ],
        "runtime": None,
        "fault_localization": None,
    },
    expected_root_cause=(
        "反向代理（如 Nginx）默认 60s 空闲读超时关闭无数据的 WebSocket 连接"
        "（close code 1006），前端只写了重连逻辑、缺少保活心跳帧，于是每约 60 "
        "秒被服务端掐断并进入重连循环；属基础设施超时而非代码缺陷。"
    ),
    expected_evidence=[
        "network_trace 中每条 ws 连接 duration 均约 60s（101 后被关闭，close 1006）",
        "console 中重复出现 WebSocket closed (1006) reconnecting，形成周期性循环",
        "console 仅含 warn/info 级连接与重连日志，无任何异常痕迹（排除业务异常导致断开）",
        "recent_diffs 中 ws 客户端仅有重连逻辑，无 ping/heartbeat",
    ],
)


# ── Case 8：下游 429 限流透传（扩展批 2026-09-26，来源：KB 下游 HTTP 状态家族）──
_CASE_API_429 = BenchmarkCase(
    case_id="api_429_downstream_ratelimit",
    title="导出接口高峰期 500：下游限流未处理",
    category="downstream_error",
    user_description=(
        "导出功能一到高峰期就报『服务器内部错误』，平峰期正常，重启也没用，"
        "完全不知道哪里错了。"
    ),
    lujo_context={
        "trace_id": "api-429-0001",
        "trace_kind": "exception",
        "exception": {
            "type": "httpx.HTTPStatusError",
            "message": "Client error '429 Too Many Requests' for url 'https://api.partner.example.com/v2/quotes'",
            "frames": [
                {"file": "app/services/quote_service.py", "line": 210, "function": "fetch_quotes"},
                {"file": "app/api/export.py", "line": 75, "function": "export"},
            ],
            "frame_count": 2,
        },
        "network_trace": [
            {"method": "GET", "url": "https://api.partner.example.com/v2/quotes", "status": 429, "duration_ms": 180},
            {"method": "GET", "url": "https://api.partner.example.com/v2/quotes", "status": 429, "duration_ms": 175},
            {"method": "POST", "url": "/api/export", "status": 500, "duration_ms": 5100},
        ],
        "trace": [
            {"step": "http", "url": "https://api.partner.example.com/v2/quotes", "status": 429, "retry": 0},
            {"step": "http", "url": "https://api.partner.example.com/v2/quotes", "status": 429, "retry": 0},
        ],
        "recent_diffs": [
            {
                "file": "app/services/quote_service.py",
                "commits_back": 1,
                "diff": (
                    "--- a/app/services/quote_service.py\n"
                    "+++ b/app/services/quote_service.py\n"
                    "@@ -200,7 +200,9 @@ def export(self, rows):\n"
                    "-    quotes = get_quotes_bulk(rows)\n"
                    "+    for row in rows:\n"
                    "+        quotes.append(self.fetch_quotes(row.sku))\n"
                    "     return quotes"
                ),
            }
        ],
        "runtime": None,
        "fault_localization": {
            "suspicious_frames": [
                {"file": "app/services/quote_service.py", "line": 210, "function": "fetch_quotes"},
            ],
            "likely_cause_candidate": "fetch_quotes",
        },
    },
    expected_root_cause=(
        "上一次改动把批量取报价改为循环逐条同步调用第三方接口，调用量激增触发"
        "对方 429 限流；fetch_quotes 对 raise_for_status 抛出的 HTTPStatusError "
        "无捕获、无退避重试，异常向上传播导致导出接口自身 500。"
    ),
    expected_evidence=[
        "exception.type == httpx.HTTPStatusError 且状态码为 429",
        "network_trace/trace 中第三方 /v2/quotes 连续返回 429 且 retry == 0",
        "recent_diffs 中导出从批量调用改为循环逐条调用下游（调用量激增）",
        "堆栈中 quote_service.fetch_quotes 无异常处理，错误透传为自身 500",
    ],
)


# ── Case 9：非 JSON 响应被解析（扩展批 2026-09-26，来源：KB JSON/序列化家族）──
_CASE_JSON_DECODE = BenchmarkCase(
    case_id="json_decode_html_response",
    title="同步任务报 JSONDecodeError：响应根本不是 JSON",
    category="json_error",
    user_description=(
        "行情同步脚本每次跑到一半就崩，报『Expecting value: line 1 column 1 "
        "(char 0)』，这段代码很久没改过，之前一直跑得好好的。"
    ),
    lujo_context={
        "trace_id": "json-err-0001",
        "trace_kind": "exception",
        "exception": {
            "type": "json.JSONDecodeError",
            "message": "Expecting value: line 1 column 1 (char 0)",
            "frames": [
                {
                    "file": "app/jobs/sync_quotes.py",
                    "line": 96,
                    "function": "pull_snapshot",
                    "context": ["resp = http.get(url, timeout=30)", "data = resp.json()"],
                }
            ],
            "frame_count": 1,
        },
        "network_trace": [
            {"method": "GET", "url": "http://internal-gw.example.local/v1/snapshot", "status": 502, "duration_ms": 3000}
        ],
        "trace": [
            {"step": "http", "url": "/v1/snapshot", "status": 502, "content_type": "text/html"}
        ],
        "request": {"method": "GET", "path": "/v1/snapshot"},
        "code_snippets": [],
        "runtime": None,
        "fault_localization": {
            "suspicious_frames": [
                {"file": "app/jobs/sync_quotes.py", "line": 96, "function": "pull_snapshot"},
            ],
            "likely_cause_candidate": "pull_snapshot",
        },
    },
    expected_root_cause=(
        "内部网关在上游快照服务超时/故障时返回 502 的 HTML 错误页，"
        "sync_quotes.pull_snapshot 对响应无条件调用 resp.json()（未检查状态码与 "
        "Content-Type），把 HTML 当 JSON 解析抛出 JSONDecodeError；真正的故障源"
        "是网关上游 502，而非解析代码本身被改动。"
    ),
    expected_evidence=[
        "exception.type == JSONDecodeError 且 char 0（响应体首字节即非 JSON）",
        "network_trace 中 /v1/snapshot 返回 502",
        "trace 中该响应 content_type == text/html（网关错误页）",
        "堆栈顶帧 sync_quotes.py:96 的 resp.json() 调用前无状态码/Content-Type 校验",
    ],
)


# ── Case 10：TLS 证书验证失败（扩展批 2026-09-26，来源：KB 网络/SSL 家族）──
_CASE_SSL_CERT = BenchmarkCase(
    case_id="ssl_cert_verify_failure",
    title="调用支付网关突然全部报证书验证失败",
    category="network_ssl",
    user_description=(
        "从昨晚部署之后，所有调用支付网关的请求都失败，报『certificate "
        "verify failed』，我们业务代码和网关证书都没动过。"
    ),
    lujo_context={
        "trace_id": "ssl-0001",
        "trace_kind": "exception",
        "exception": {
            "type": "ssl.SSLCertVerificationError",
            "message": "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate (_ssl.c:1006)",
            "frames": [
                {"file": "app/services/payment_client.py", "line": 58, "function": "charge"},
            ],
            "frame_count": 1,
        },
        "network_trace": [
            {"method": "POST", "url": "https://pay-gw.internal.example.com/v1/charge", "status": 0, "duration_ms": 230}
        ],
        "request": {"method": "POST", "path": "/v1/charge", "body": {"order_id": "ord-9182", "amount": 12900}},
        "recent_diffs": [
            {
                "file": "deploy/Dockerfile",
                "commits_back": 1,
                "diff": (
                    "--- a/deploy/Dockerfile\n"
                    "+++ b/deploy/Dockerfile\n"
                    "@@ -4,7 +4,6 @@\n"
                    " FROM python:3.12-slim\n"
                    "-RUN apt-get update && apt-get install -y ca-certificates && update-ca-certificates\n"
                    " COPY . /app"
                ),
            }
        ],
        "runtime": None,
        "fault_localization": None,
    },
    expected_root_cause=(
        "最近一次镜像变更删除了 ca-certificates 安装层，容器内缺少系统根证书，"
        "导致对外部服务的 TLS 证书链在握手阶段即验证失败"
        "（SSLCertVerificationError）；属部署环境变更所致，代码逻辑本身无改动。"
    ),
    expected_evidence=[
        "exception.type == ssl.SSLCertVerificationError（unable to get local issuer certificate）",
        "recent_diffs 中 Dockerfile 删除了 ca-certificates 安装/更新步骤",
        "network_trace 中外呼 status == 0（TLS 握手失败，未到达应用层）",
        "recent_diffs 显示最近一次部署仅删除了 ca-certificates 安装层，其余零改动",
    ],
)


# ── Case 11：fire-and-forget 任务静默丢失（扩展批 2026-09-26，来源：KB 异步任务家族）──
_CASE_ASYNC_LOST = BenchmarkCase(
    case_id="async_task_fire_forget_lost",
    title="后台通知任务静默丢失：偶发不发短信",
    category="async_task",
    user_description=(
        "用户偶尔收不到订单完成的短信通知，大概百分之几的单子会丢，"
        "没有重试也没有任何报错日志，无从查起。"
    ),
    lujo_context={
        "trace_id": "task-lost-0001",
        "trace_kind": "silent_failure",
        "exception": None,
        "recent_diffs": [
            {
                "file": "app/services/notify_service.py",
                "commits_back": 1,
                "diff": (
                    "--- a/app/services/notify_service.py\n"
                    "+++ b/app/services/notify_service.py\n"
                    "@@ -40,5 +40,5 @@ def on_order_done(order):\n"
                    "-    await send_order_sms(order)\n"
                    "+    asyncio.create_task(send_order_sms(order))\n"
                    "     return result"
                ),
            }
        ],
        "trace": [
            {"step": "task", "name": "send_order_sms", "order_id": "ord-9182", "status": "created"},
            {"step": "task", "name": "send_order_sms", "order_id": "ord-9182", "status": "cancelled", "detail": "CancelledError swallowed, no retry"},
            {"step": "sms_gateway", "called": False},
        ],
        "network_trace": [
            {"method": "POST", "url": "/api/orders", "status": 200, "duration_ms": 210}
        ],
        "runtime": None,
        "fault_localization": None,
    },
    expected_root_cause=(
        "通知服务把原来的 await send_order_sms(order) 改成了 "
        "asyncio.create_task(...) 的 fire-and-forget：任务引用未被保存，请求协程"
        "结束后任务被取消且 CancelledError 被吞掉，短信任务从未真正执行，因此"
        "偶发丢通知且服务端全程无感知。"
    ),
    expected_evidence=[
        "recent_diffs 中由 await 改为 asyncio.create_task 且未保存任务引用",
        "trace 中任务 created 后以 cancelled 结束，且 sms_gateway 从未被调用",
        "trace 中任务的结束 detail 标注 CancelledError swallowed，且无重试记录",
        "network_trace 中 /api/orders 返回 200（丢通知与下单主链路无关）",
    ],
)


# ── Case 12：CORS 预检拦截（扩展批 2026-09-26，来源：KB CORS 家族）──
_CASE_CORS_PREFLIGHT = BenchmarkCase(
    case_id="cors_preflight_error_path",
    title="新接口被浏览器 CORS 拦截，curl 却是通的",
    category="cors_error",
    user_description=(
        "前端调新上的『发票上传』接口，浏览器控制台报 CORS 错误，但用 curl "
        "直接调完全正常，其他老接口也都没问题。"
    ),
    lujo_context={
        "trace_id": "cors-0001",
        "trace_kind": "silent_failure",
        "exception": None,
        "console": [
            {
                "level": "error",
                "message": "Access to fetch at '/api/invoices' from origin 'https://app.example.com' has been blocked by CORS policy: No 'Access-Control-Allow-Origin' header is present on the requested resource.",
            }
        ],
        "network_trace": [
            {"method": "OPTIONS", "url": "/api/invoices", "status": 405, "duration_ms": 8},
            {"method": "POST", "url": "/api/invoices", "status": 400, "duration_ms": 45},
        ],
        "request": {
            "method": "POST",
            "path": "/api/invoices",
            "headers": {"Origin": "https://app.example.com", "Access-Control-Request-Headers": "content-type"},
        },
        "recent_diffs": [
            {
                "file": "app/main.py",
                "commits_back": 1,
                "diff": (
                    "--- a/app/main.py\n"
                    "+++ b/app/main.py\n"
                    "@@ -18,6 +18,7 @@\n"
                    "+app.mount(\"/api/invoices\", invoices_app)\n"
                    " app.add_middleware(CORSMiddleware, allow_origins=[\"https://app.example.com\"])"
                ),
            }
        ],
        "runtime": None,
        "fault_localization": None,
    },
    expected_root_cause=(
        "新发票接口以独立子应用 mount 到 /api/invoices，子应用没有安装 CORS "
        "中间件（主应用的 CORSMiddleware 管不到它），其所有响应（含 OPTIONS "
        "预检 405 与业务 400）都不带 Access-Control-Allow-Origin，浏览器按同源"
        "策略拦截响应并报 CORS 错误；curl 不经过浏览器同源策略所以『通』。"
    ),
    expected_evidence=[
        "console 中 CORS 报错：响应缺 Access-Control-Allow-Origin",
        "network_trace 中 OPTIONS 预检返回 405（子应用未处理预检/CORS）",
        "请求头带 Origin，但响应无任何 Access-Control-* 头",
        "recent_diffs 中新路由以子应用 mount，绕过了主应用 CORS 中间件",
    ],
)


# 全部 12 个标准 Case（6 冻结基础 + 2026-09-26 扩展 6，扩展来源见各 case 注释）
BENCHMARK_CASES: list[BenchmarkCase] = [
    _CASE_API_500,
    _CASE_FRONTEND_BLANK,
    _CASE_DB_ERROR,
    _CASE_AUTH_403,
    _CASE_PERF,
    _CASE_FRONTEND_SOURCEMAP,
    _CASE_WS_IDLE_DROP,
    _CASE_API_429,
    _CASE_JSON_DECODE,
    _CASE_SSL_CERT,
    _CASE_ASYNC_LOST,
    _CASE_CORS_PREFLIGHT,
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
