"""M2-M4 改进后 QualityScorer 评分对比 —— 展示各 Milestone 落地后的分数提升。

对比方式：
- M1 基线：原始 demo_quality_scorer.py 的 5 个场景（静态硬编码）
- M2-M4 改进：同样 5 个场景，但 repair_context 反映 M2-M4 管线产出的真实改进：
  - M2：KB 种子加载 → knowledge_base_hit=True / prior_analysis 来自 KB
  - M3：静态分析 → fault_locations + URL→handler code_snippets
  - M4：Verify Loop → case_confidence/verify_count 提升分析可信度
"""
import copy
from typing import Any

from app.quality.scorer import evaluate
from app.quality.schemas import ContextDimension

# 导入 M1 基线场景
from scripts.demo_quality_scorer import (
    scenario_a_full as _m1_a,
    scenario_b_typical as _m1_b,
    scenario_c_minimal as _m1_c,
    scenario_d_silent_failure as _m1_d,
    scenario_e_kb_hit as _m1_e,
)

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


# ═══════════════════════════════════════════════════════════════
# M2-M4 改进场景
# ═══════════════════════════════════════════════════════════════

def scenario_a_m4() -> dict[str, Any]:
    """场景 A（M4 增强）：M3 静态分析 + M4 Verify Loop 置信度提升。

    改进点：
    - M3 fault_locations：函数级静态分析输出（签名/复杂度/可疑输入）
    - M4 prior_analysis.case_confidence=0.95（verify_count=3 后递增）
    - M2 vector_recall 更丰富（种子知识向量同步后能召回）
    """
    ctx = copy.deepcopy(_m1_a())
    rc = ctx["repair_context"]

    # M3：静态分析 fault_locations
    rc["fault_locations"] = [
        {
            "file": "app/service/user.py",
            "function": "validate_input",
            "line": 98,
            "signature": "validate_input(data: dict) -> None",
            "complexity": 3,
            "suspicious_inputs": ["data (dict, 未校验 None 键)"],
            "analyzer": "static_analyzer.ast",
        },
        {
            "file": "app/service/user.py",
            "function": "process_user",
            "line": 142,
            "signature": "process_user(user_id: str) -> Any",
            "complexity": 5,
            "suspicious_inputs": ["user_id (str, 上游可能传 None)"],
            "analyzer": "static_analyzer.ast",
        },
    ]

    # M4：Verify Loop 验证后置信度提升
    rc["prior_analysis"]["case_confidence"] = 0.95
    rc["prior_analysis"]["verify_count"] = 3
    rc["prior_analysis"]["analysis_source"] = "llm_verified"

    # M2：种子向量同步后召回更丰富
    rc["vector_recall"] = [
        {"id": "kb-042", "summary": "类似 ValueError: None 参数导致数据库查询失败", "score": 0.92},
        {"id": "kb-017", "summary": "validate_input 缺少必填字段校验导致下游崩溃", "score": 0.88},
        {"id": "seed-003", "summary": "NoneType 参数传入 db.query 触发 ValueError", "score": 0.85},
    ]
    rc["sources"]["vector_recall"] = [
        {"id": "kb-042"}, {"id": "kb-017"}, {"id": "seed-003"}
    ]

    return ctx


def scenario_b_m4() -> dict[str, Any]:
    """场景 B（M4 增强）：M2 KB 种子命中 + M3 静态分析。

    改进点：
    - M2 knowledge_base_hit=True（KeyError 种子命中）
    - M2 prior_analysis 来自 KB（analysis_source=knowledge_base）
    - M3 fault_locations
    - M2 vector_recall 有种子召回
    """
    ctx = copy.deepcopy(_m1_b())
    rc = ctx["repair_context"]

    # M2：KB 种子命中
    rc["sources"]["knowledge_base_hit"] = True
    rc["prior_analysis"] = {
        "root_cause": "配置文件缺少 'config' 键，load_config() 未做 KeyError 防护",
        "impact": "服务启动失败",
        "fix": "使用 data.get('config', {}) 替代直接索引，并增加配置文件校验",
        "confidence": "high",
        "analysis_source": "knowledge_base",
        "knowledge_base_hit": True,
        "case_confidence": 0.85,
        "verify_count": 2,
    }
    rc["sources"]["vector_recall"] = [
        {"id": "seed-008", "summary": "KeyError: 'config' — 配置文件缺键", "score": 0.91},
    ]
    rc["vector_recall"] = list(rc["sources"]["vector_recall"])

    # M3：静态分析
    rc["fault_locations"] = [
        {
            "file": "app/core/settings.py",
            "function": "load_config",
            "line": 88,
            "signature": "load_config(path: str) -> dict",
            "complexity": 2,
            "suspicious_inputs": ["path (str, 文件可能缺键)"],
            "analyzer": "static_analyzer.ast",
        },
    ]

    return ctx


def scenario_c_m4() -> dict[str, Any]:
    """场景 C（M4 增强）：M2 KB 种子命中 + M3 静态分析。

    原始 C 最惨（0.08），M2-M4 提升最大：
    - M2 KB 种子命中 AttributeError: 'NoneType' object has no attribute 'split'
    - M3 静态分析从 1 帧堆栈提取函数签名
    - M2 prior_analysis 来自 KB
    """
    ctx = copy.deepcopy(_m1_c())
    dc = ctx["debug_context"]
    rc = ctx["repair_context"]

    # M3：静态分析从单帧提取源码
    dc["code_snippets"] = [
        {
            "file": "app/utils.py",
            "error_line": 200,
            "snippet": "def parse_version(version_str):\n    return version_str.split('.')",
            "found": True,
            "link": "vscode://file/app/utils.py:200",
        },
    ]

    # M2：KB 种子命中（AttributeError NoneType）
    rc["prior_analysis"] = {
        "root_cause": "version_str 为 None，parse_version() 未做 None 检查直接调用 .split()",
        "impact": "版本解析失败，影响功能判断",
        "fix": "在 parse_version() 开头增加 `if version_str is None: return ()`",
        "confidence": "high",
        "analysis_source": "knowledge_base",
        "knowledge_base_hit": True,
        "case_confidence": 0.80,
        "verify_count": 1,
    }
    rc["sources"]["knowledge_base_hit"] = True
    rc["sources"]["vector_recall"] = [
        {"id": "seed-012", "summary": "AttributeError: NoneType has no attribute split", "score": 0.89},
    ]
    rc["vector_recall"] = list(rc["sources"]["vector_recall"])

    # M3：静态分析 fault_locations
    rc["fault_locations"] = [
        {
            "file": "app/utils.py",
            "function": "parse_version",
            "line": 200,
            "signature": "parse_version(version_str: str) -> tuple",
            "complexity": 1,
            "suspicious_inputs": ["version_str (str, 可能为 None)"],
            "analyzer": "static_analyzer.ast",
        },
    ]

    return ctx


def scenario_d_m4() -> dict[str, Any]:
    """场景 D（M4 增强）：M3 URL→handler 反查 + 静态分析。

    原始 D 无异常堆栈，code_snippets 为空。
    - M3 URL→handler 反查：从 network_trace POST /api/order 定位 handler 源码
    - M3 静态分析：handler 函数签名/复杂度
    - M4 Verify Loop：case_confidence 提升
    """
    ctx = copy.deepcopy(_m1_d())
    dc = ctx["debug_context"]
    rc = ctx["repair_context"]

    # M3：URL→handler 反查 → code_snippets
    dc["code_snippets"] = [
        {
            "file": "app/handler/order.py",
            "error_line": 45,
            "snippet": "async def create_order(request):\n    order = await OrderModel.create(**data)\n    return {'success': True, 'order_id': order.id}",
            "found": True,
            "link": "vscode://file/app/handler/order.py:45",
        },
    ]

    # M3：静态分析 fault_locations
    rc["fault_locations"] = [
        {
            "file": "app/handler/order.py",
            "function": "create_order",
            "line": 45,
            "signature": "create_order(request: Request) -> dict",
            "complexity": 4,
            "suspicious_inputs": ["order.id (可能未回填)"],
            "analyzer": "static_analyzer.url_resolver",
        },
    ]

    # M4：Verify Loop 验证后置信度
    rc["prior_analysis"]["case_confidence"] = 0.90
    rc["prior_analysis"]["verify_count"] = 2
    rc["prior_analysis"]["analysis_source"] = "llm_verified"

    return ctx


def scenario_e_m4() -> dict[str, Any]:
    """场景 E（M4 增强）：M4 Verify Loop 置信度提升 + M2 向量召回。

    原始 E 可信度已 0.94（最高），M4 进一步提升：
    - M4 case_confidence=0.95（verify_count=5）
    - M2 vector_recall 有种子召回
    """
    ctx = copy.deepcopy(_m1_e())
    rc = ctx["repair_context"]

    # M4：多次验证后置信度提升
    rc["prior_analysis"]["case_confidence"] = 0.95
    rc["prior_analysis"]["verify_count"] = 5
    rc["prior_analysis"]["analysis_source"] = "knowledge_base_verified"

    # M2：向量召回有种子
    rc["sources"]["vector_recall"] = [
        {"id": "seed-015", "summary": "ConnectionError Redis 连接失败", "score": 0.93},
    ]
    rc["vector_recall"] = list(rc["sources"]["vector_recall"])

    # M3：静态分析 fault_locations
    rc["fault_locations"] = [
        {
            "file": "app/cache/redis.py",
            "function": "connect",
            "line": 30,
            "signature": "connect() -> Redis",
            "complexity": 1,
            "suspicious_inputs": ["host (默认 localhost)", "port (默认 6379)"],
            "analyzer": "static_analyzer.ast",
        },
    ]

    return ctx


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║   M1 基线 vs M2-M4 改进 — QualityScorer 评分对比                 ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")

    m1_scenarios = [
        ("A-完整上下文", _m1_a()),
        ("B-典型后端报错", _m1_b()),
        ("C-最简报错", _m1_c()),
        ("D-静默失败", _m1_d()),
        ("E-知识库命中", _m1_e()),
    ]

    m4_scenarios = [
        ("A-完整上下文", scenario_a_m4()),
        ("B-典型后端报错", scenario_b_m4()),
        ("C-最简报错", scenario_c_m4()),
        ("D-静默失败", scenario_d_m4()),
        ("E-知识库命中", scenario_e_m4()),
    ]

    # ── 逐场景对比 ──
    print(f"\n{'场景':20s} │ {'M1 综合':>8s} │ {'M4 综合':>8s} │ {'提升':>8s} │ 主要改进维度")
    print(f"{'─'*20}─┼─{'─'*8}─┼─{'─'*8}─┼─{'─'*8}─┼─{'─'*30}")

    improvements = []
    for (name_m1, ctx_m1), (name_m4, ctx_m4) in zip(m1_scenarios, m4_scenarios):
        r_m1 = evaluate(ctx_m1)
        r_m4 = evaluate(ctx_m4)

        delta = r_m4.overall_score - r_m1.overall_score
        improvements.append((name_m1, r_m1, r_m4, delta))

        # 找提升最大的维度
        dim_deltas = {}
        for dim in ContextDimension:
            d1 = r_m1.context_completeness.dimensions.get(dim)
            d2 = r_m4.context_completeness.dimensions.get(dim)
            if d1 and d2:
                dd = d2.score - d1.score
                if dd > 0.01:
                    dim_deltas[_DIMENSION_LABELS_CN.get(dim, dim.value)] = dd

        conf_delta = r_m4.analysis_confidence.overall_score - r_m1.analysis_confidence.overall_score
        if conf_delta > 0.01:
            dim_deltas["可信度"] = conf_delta

        top_dims = sorted(dim_deltas.items(), key=lambda x: -x[1])[:3]
        dims_str = " / ".join(f"{k}+{v:.2f}" for k, v in top_dims) if top_dims else "—"

        print(
            f"{name_m1:20s} │ {r_m1.overall_score:>8.2f} │ {r_m4.overall_score:>8.2f} │ "
            f"{delta:>+8.2f} │ {dims_str}"
        )

    # ── 汇总 ──
    avg_m1 = sum(r.overall_score for _, r, _, _ in improvements) / len(improvements) if improvements else 0
    # fix: need to extract properly
    m1_scores = [r_m1.overall_score for _, r_m1, _, _ in improvements]
    m4_scores = [r_m4.overall_score for _, _, r_m4, _ in improvements]
    avg_m1 = sum(m1_scores) / len(m1_scores)
    avg_m4 = sum(m4_scores) / len(m4_scores)

    print(f"{'─'*20}─┼─{'─'*8}─┼─{'─'*8}─┼─{'─'*8}─┼─{'─'*30}")
    print(
        f"{'平均':20s} │ {avg_m1:>8.2f} │ {avg_m4:>8.2f} │ "
        f"{avg_m4 - avg_m1:>+8.2f} │"
    )

    # ── 详细：M4 改进后各场景评分 ──
    print(f"\n\n{'='*70}")
    print("  M2-M4 改进后详细评分")
    print(f"{'='*70}")
    print(f"  {'场景':20s} {'完整度':>8s} {'可信度':>8s} {'综合':>8s} {'证据数':>6s}")
    print(f"  {'─'*20} {'─'*8} {'─'*8} {'─'*8} {'─'*6}")
    for name, ctx in m4_scenarios:
        r = evaluate(ctx)
        print(
            f"  {name:20s} "
            f"{r.context_completeness.overall_score:>8.2f} "
            f"{r.analysis_confidence.overall_score:>8.2f} "
            f"{r.overall_score:>8.2f} "
            f"{r.analysis_confidence.evidence_count:>6d}"
        )

    # ── PRD 预期对比 ──
    print(f"\n\n{'='*70}")
    print("  与 PRD §12.2 预期对比")
    print(f"{'='*70}")
    prd_expected = {
        "A-完整上下文": 0.90,
        "B-典型后端报错": 0.65,
        "C-最简报错": 0.46,
        "D-静默失败": 0.61,
        "E-知识库命中": 0.65,
    }
    print(f"  {'场景':20s} {'M1基线':>8s} {'M4实际':>8s} {'PRD预期':>8s} {'达成':>6s}")
    print(f"  {'─'*20} {'─'*8} {'─'*8} {'─'*8} {'─'*6}")
    for name, _, r_m4, _ in improvements:
        # re-evaluate for clarity
        m1_val = next(r_m1.overall_score for n, r_m1, _, _ in improvements if n == name)
        m4_val = r_m4.overall_score
        expected = prd_expected.get(name, 0)
        achieved = "✅" if m4_val >= expected - 0.05 else "🔲"
        print(
            f"  {name:20s} {m1_val:>8.2f} {m4_val:>8.2f} {expected:>8.2f} {achieved:>6s}"
        )

    print()


if __name__ == "__main__":
    main()
