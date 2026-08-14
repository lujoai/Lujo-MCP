"""QualityScorer 模拟演示 —— 用几个真实场景跑一遍评分引擎，观察输出。

场景覆盖：
- 场景 A：完整上下文（所有采集维度齐备 + LLM 高置信度分析）
- 场景 B：典型后端报错（堆栈 + 源码 + git，缺少前端/网络/规范）
- 场景 C：最简报错（仅堆栈 + 运行时，其他全缺）
- 场景 D：静默失败（spec_diffs 偏离但无异常）
- 场景 E：知识库精确命中（复用历史修复，跳过 LLM）
"""

import time
from typing import Any

from app.quality.schemas import ContextDimension
from app.quality.scorer import evaluate


# ═══════════════════════════════════════════════════════════════
# 场景构造
# ═══════════════════════════════════════════════════════════════


def scenario_a_full() -> dict[str, Any]:
    """场景 A：完整上下文，所有维度齐备。"""
    return {
        "debug_context": {
            "request_id": "req-full-001",
            "trace_id": "trace-full-001",
            "trace_kind": "exception",
            "exception": {
                "type": "ValueError",
                "message": "user_id 参数不能为 None，在调用链中上游未做空值校验",
                "frames": [
                    {"file": "app/service/user.py", "line": 142, "function": "process_user"},
                    {"file": "app/service/user.py", "line": 98, "function": "validate_input"},
                    {"file": "app/handler/api.py", "line": 56, "function": "handle_create_user"},
                    {"file": "app/handler/api.py", "line": 30, "function": "dispatch"},
                    {"file": "app/middleware.py", "line": 15, "function": "__call__"},
                ],
                "frame_count": 5,
            },
            "code_snippets": [
                {
                    "file": "app/service/user.py",
                    "error_line": 142,
                    "snippet": 'def process_user(user_id: str):\n    name = db.query("SELECT name FROM users WHERE id=?", user_id)\n    ...',
                    "found": True,
                    "link": "vscode://file/app/service/user.py:142",
                },
                {
                    "file": "app/service/user.py",
                    "error_line": 98,
                    "snippet": 'def validate_input(data: dict):\n    user_id = data.get("user_id")  # 未校验 None\n    ...',
                    "found": True,
                    "link": "vscode://file/app/service/user.py:98",
                },
                {
                    "file": "app/handler/api.py",
                    "error_line": 56,
                    "snippet": 'def handle_create_user(request):\n    return process_user(request.json.get("user_id"))\n    ...',
                    "found": True,
                    "link": "vscode://file/app/handler/api.py:56",
                },
            ],
            "git_blame": [
                {"file": "app/service/user.py", "line": 98, "author": "zhangsan", "commit": "abc123"},
                {"file": "app/handler/api.py", "line": 56, "author": "lisi", "commit": "def456"},
            ],
            "recent_diffs": [
                {
                    "file": "app/service/user.py",
                    "diff": "-    user_id = data.get('user_id')\n+    user_id = data.get('user_id')  # 未校验 None",
                },
            ],
            "network_trace": [
                {"url": "POST /api/user", "status": 500, "duration_ms": 120},
                {"url": "GET /api/user/123", "status": 200, "duration_ms": 45},
            ],
            "ui_events": [
                {"event": "click", "target": "#create-user-btn", "timestamp": 1700000000.0},
                {"event": "input", "target": "#user-id-input", "value": "", "timestamp": 1700000001.0},
            ],
            "spec_diffs": None,
            "related_specs": [
                {"file": "specs/user_api.md", "content": "## POST /api/user\n- user_id 必填，不可为 null"},
            ],
            "runtime": {
                "pid": 12345,
                "cpu_percent": 23.5,
                "memory_mb": 512.0,
                "thread_count": 12,
                "python_version": "3.12.5",
                "open_files": 45,
            },
        },
        "repair_context": {
            "prior_analysis": {
                "root_cause": "validate_input() 未对 user_id 做 None 检查，直接传入 process_user() 的数据库查询导致 ValueError",
                "impact": "所有 user_id 为空的请求均返回 500，影响用户注册流程",
                "fix": "在 validate_input() 中增加 `if user_id is None: raise ValueError('user_id is required')` 提前校验",
                "confidence": "high",
                "analysis_source": "llm",
            },
            "vector_recall": [
                {"id": "kb-042", "summary": "类似 ValueError: None 参数导致数据库查询失败"},
                {"id": "kb-017", "summary": "validate_input 缺少必填字段校验导致下游崩溃"},
            ],
            "git_context": [
                {"file": "app/service/user.py", "diff": "最近 3 次提交均涉及 user_id 处理逻辑"},
            ],
            "sources": {
                "vector_recall": [{"id": "kb-042"}, {"id": "kb-017"}],
                "git_context": [{"file": "app/service/user.py"}],
                "knowledge_base_hit": False,
            },
        },
    }


def scenario_b_typical() -> dict[str, Any]:
    """场景 B：典型后端报错 —— 堆栈 + 源码 + git，其他维度缺。"""
    return {
        "debug_context": {
            "request_id": "req-typ-002",
            "trace_id": "trace-typ-002",
            "trace_kind": "exception",
            "exception": {
                "type": "KeyError",
                "message": "'config'",
                "frames": [
                    {"file": "app/core/settings.py", "line": 88, "function": "load_config"},
                    {"file": "app/core/settings.py", "line": 45, "function": "__init__"},
                    {"file": "app/main.py", "line": 12, "function": "startup"},
                ],
                "frame_count": 3,
            },
            "code_snippets": [
                {
                    "file": "app/core/settings.py",
                    "error_line": 88,
                    "snippet": "def load_config(path):\n    data = json.load(open(path))\n    return data['config']",
                    "found": True,
                    "link": "vscode://file/app/core/settings.py:88",
                },
            ],
            "git_blame": [
                {"file": "app/core/settings.py", "line": 88, "author": "wangwu", "commit": "ghi789"},
            ],
            "recent_diffs": None,
            "network_trace": None,
            "ui_events": None,
            "spec_diffs": None,
            "related_specs": [],
            "runtime": {
                "pid": 12346,
                "cpu_percent": 5.0,
                "memory_mb": 128.0,
                "thread_count": 4,
            },
        },
        "repair_context": {
            "prior_analysis": {
                "root_cause": "配置文件缺少 'config' 键，load_config() 未做 KeyError 防护",
                "impact": "服务启动失败",
                "fix": "使用 data.get('config', {}) 替代直接索引，并增加配置文件校验",
                "confidence": "medium",
                "analysis_source": "llm",
            },
            "vector_recall": [],
            "git_context": [],
            "sources": {
                "vector_recall": [],
                "git_context": [],
                "knowledge_base_hit": False,
            },
        },
    }


def scenario_c_minimal() -> dict[str, Any]:
    """场景 C：最简报错 —— 仅堆栈 + 运行时，其他全缺。"""
    return {
        "debug_context": {
            "request_id": "req-min-003",
            "trace_id": "trace-min-003",
            "trace_kind": "exception",
            "exception": {
                "type": "AttributeError",
                "message": "'NoneType' object has no attribute 'split'",
                "frames": [
                    {"file": "app/utils.py", "line": 200, "function": "parse_version"},
                ],
                "frame_count": 1,
            },
            "code_snippets": [],
            "git_blame": None,
            "recent_diffs": None,
            "network_trace": None,
            "ui_events": None,
            "spec_diffs": None,
            "related_specs": [],
            "runtime": {
                "pid": 12347,
                "cpu_percent": 2.0,
                "memory_mb": 64.0,
                "thread_count": 2,
            },
        },
        "repair_context": {
            "prior_analysis": None,
            "vector_recall": [],
            "git_context": [],
            "sources": {
                "vector_recall": [],
                "git_context": [],
                "knowledge_base_hit": False,
            },
        },
    }


def scenario_d_silent_failure() -> dict[str, Any]:
    """场景 D：静默失败 —— 200 OK 但规范校验偏离，无异常。"""
    return {
        "debug_context": {
            "request_id": "req-silent-004",
            "trace_id": "trace-silent-004",
            "trace_kind": "debug",
            "exception": None,
            "code_snippets": [],
            "git_blame": None,
            "recent_diffs": None,
            "network_trace": [
                {"url": "POST /api/order", "status": 200, "body": {"success": True, "order_id": None}},
            ],
            "ui_events": None,
            "spec_diffs": [
                {"field": "body.order_id", "expected": "非空字符串", "actual": None},
                {"field": "body.success", "expected": True, "actual": True},
            ],
            "related_specs": [
                {"file": "specs/order_api.md", "content": "## POST /api/order\n- 返回 order_id 必填，不可为 null"},
            ],
            "runtime": {
                "pid": 12348,
                "cpu_percent": 8.0,
                "memory_mb": 200.0,
                "thread_count": 6,
            },
        },
        "repair_context": {
            "prior_analysis": {
                "root_cause": "订单创建成功但 order_id 未正确返回，可能是数据库写入后未回填 ID",
                "impact": "前端无法获取订单号，用户体验受损但无报错",
                "fix": "检查 ORM 的 insert 返回值，确保 order_id 回填到响应中",
                "confidence": "medium",
                "analysis_source": "llm",
            },
            "vector_recall": [
                {"id": "kb-099", "summary": "order_id 为空导致前端轮询失败"},
            ],
            "git_context": [],
            "sources": {
                "vector_recall": [{"id": "kb-099"}],
                "git_context": [],
                "knowledge_base_hit": True,
            },
        },
    }


def scenario_e_kb_hit() -> dict[str, Any]:
    """场景 E：知识库精确命中 —— 复用历史修复，跳过 LLM 分析。"""
    return {
        "debug_context": {
            "request_id": "req-kb-005",
            "trace_id": "trace-kb-005",
            "trace_kind": "exception",
            "exception": {
                "type": "ConnectionError",
                "message": "Failed to connect to Redis at localhost:6379",
                "frames": [
                    {"file": "app/cache/redis.py", "line": 30, "function": "connect"},
                    {"file": "app/main.py", "line": 20, "function": "startup"},
                ],
                "frame_count": 2,
            },
            "code_snippets": [
                {
                    "file": "app/cache/redis.py",
                    "error_line": 30,
                    "snippet": "def connect():\n    return redis.Redis(host='localhost', port=6379)",
                    "found": True,
                    "link": "vscode://file/app/cache/redis.py:30",
                },
            ],
            "git_blame": None,
            "recent_diffs": None,
            "network_trace": None,
            "ui_events": None,
            "spec_diffs": None,
            "related_specs": [],
            "runtime": {
                "pid": 12349,
                "cpu_percent": 1.0,
                "memory_mb": 48.0,
                "thread_count": 1,
            },
        },
        "repair_context": {
            "prior_analysis": {
                "root_cause": "Redis 服务未启动或端口被占用",
                "impact": "缓存不可用，服务降级到直接查询数据库",
                "fix": "启动 Redis 服务，或配置 REDIS_URL 环境变量指向正确的地址",
                "confidence": "high",
                "analysis_source": "knowledge_base",
                "knowledge_base_hit": True,
                "cached": True,
            },
            "vector_recall": [],
            "git_context": [],
            "sources": {
                "vector_recall": [],
                "git_context": [],
                "knowledge_base_hit": True,
            },
        },
    }


# ═══════════════════════════════════════════════════════════════
# 输出格式化
# ═══════════════════════════════════════════════════════════════


def _bar(label: str, value: float, width: int = 30) -> str:
    """绘制简单的 ASCII 进度条。"""
    filled = int(value * width)
    empty = width - filled
    bar = "█" * filled + "░" * empty
    return f"{label:20s} │{bar}│ {value:.2f}"


def _print_report(scenario_name: str, scenario_desc: str, agent_ctx: dict[str, Any]):
    """格式化输出单份 QualityReport。"""
    report = evaluate(agent_ctx)

    print(f"\n{'='*70}")
    print(f"  场景: {scenario_name}")
    print(f"  描述: {scenario_desc}")
    print(f"{'='*70}")

    # ── 综合评分 ──
    print(f"\n  📊 综合评分: {report.overall_score:.2f}")
    print(f"      = 完整度 {report.context_completeness.overall_score:.2f} × 可信度 {report.analysis_confidence.overall_score:.2f}")

    # ── 完整度进度条 ──
    print(f"\n  ── 上下文完整度 ({report.context_completeness.overall_score:.2f}) ──")
    print(f"     各维度: {report.context_completeness.missing_count}/{report.context_completeness.total_dimensions} 缺失")
    for dim in ContextDimension:
        ds = report.context_completeness.dimensions[dim]
        icon = "✅" if ds.present else "❌"
        label = _DIMENSION_LABELS_CN.get(dim, dim.value)
        print(f"     {icon} {label:12s} {ds.score:.2f}  {ds.reason}")

    # ── 可信度 ──
    print(f"\n  ── 分析可信度 ({report.analysis_confidence.overall_score:.2f}) ──")
    conf = report.analysis_confidence
    print(f"     证据总数: {conf.evidence_count} 条")
    print(f"     高相关度: {conf.high_relevance_count} 条")
    print(f"     已覆盖维度: {', '.join(conf.coverage_aspects) if conf.coverage_aspects else '(无)'}")
    print(f"     缺失维度:   {', '.join(conf.missing_aspects) if conf.missing_aspects else '(无)'}")

    # ── 证据明细 ──
    print(f"\n  ── 证据明细 ({len(report.evidence_items)} 条) ──")
    for i, item in enumerate(report.evidence_items, 1):
        rel_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(item.relevance.value, "⚪")
        print(f"     {i:2d}. {rel_icon} [{item.type.value:20s}] {item.description[:80]}")
        if item.location:
            print(f"         📍 {item.location}")

    # ── 改进建议 ──
    if report.suggestions:
        print("\n  ── 改进建议 ──")
        for s in report.suggestions:
            print(f"     💡 {s}")
    else:
        print("\n  ── 改进建议 ──")
        print("     ✨ 无需改进，上下文质量优秀")

    print(f"\n  ⏱️  评分时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(report.scored_at))}")


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

_DIMENSION_LABELS_CN: dict[ContextDimension, str] = {
    ContextDimension.TRACE: "异常堆栈",
    ContextDimension.CODE_SNIPPET: "源码片段",
    ContextDimension.RUNTIME: "运行时快照",
    ContextDimension.GIT_CONTEXT: "Git 归因",
    ContextDimension.NETWORK: "网络请求",
    ContextDimension.UI_EVENT: "前端 UI 事件",
    ContextDimension.SPEC: "规范校验",
    ContextDimension.KNOWLEDGE_BASE: "知识库/向量",
    ContextDimension.LLM_ANALYSIS: "LLM 分析",
}


def main():
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║       QualityScorer 模拟演示 —— 5 个真实场景评分对比            ║")
    print("╚══════════════════════════════════════════════════════════════════╝")

    scenarios = [
        ("A-完整上下文", "所有采集维度齐备 + LLM 高置信度 + 向量召回", scenario_a_full()),
        ("B-典型后端报错", "堆栈 + 源码 + git blame，缺少前端/网络/规范", scenario_b_typical()),
        ("C-最简报错", "仅堆栈 + 运行时，其他全部缺失", scenario_c_minimal()),
        ("D-静默失败", "200 OK 但 spec_diffs 偏离，无异常堆栈", scenario_d_silent_failure()),
        ("E-知识库命中", "知识库精确命中，复用历史修复，跳过 LLM", scenario_e_kb_hit()),
    ]

    for name, desc, ctx in scenarios:
        _print_report(name, desc, ctx)

    # ── 汇总对比 ──
    print(f"\n\n{'='*70}")
    print("  汇总对比")
    print(f"{'='*70}")
    print(f"  {'场景':20s} {'完整度':>8s} {'可信度':>8s} {'综合':>8s} {'证据数':>6s}")
    print(f"  {'─'*20} {'─'*8} {'─'*8} {'─'*8} {'─'*6}")
    for name, _, ctx in scenarios:
        report = evaluate(ctx)
        print(
            f"  {name:20s} "
            f"{report.context_completeness.overall_score:>8.2f} "
            f"{report.analysis_confidence.overall_score:>8.2f} "
            f"{report.overall_score:>8.2f} "
            f"{report.analysis_confidence.evidence_count:>6d}"
        )
    print()


if __name__ == "__main__":
    main()