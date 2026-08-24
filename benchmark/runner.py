"""Benchmark 运行器 —— MCP Debug Context Quality Benchmark（Phase 3 D6）。

提供 CLI 入口：
- list    ：列出全部 BenchmarkCase
- show    ：导出单个 Case 的 Without / With 两版输入
- quality ：对每个 Case 运行 QualityScorer 旁证评分（Context 完整度）

定位：纯评估工具，独立于 app/ 生产 Layer，不引入 LLM 调用链。
默认评估方式为人工对照 4 指标打分（见 docs/internal/BENCHMARK.md）。
"""

from __future__ import annotations

import json
import sys
from typing import Any

from benchmark.cases import get_case, list_cases


def cmd_list() -> int:
    """列出全部 Case 的元信息。"""
    print(f"共 {len(list_cases())} 个 BenchmarkCase：")
    for c in list_cases():
        print(f"  [{c.case_id}] ({c.category}) {c.title}")
    return 0


def cmd_show(case_id: str) -> int:
    """导出单个 Case 的 Without / With 两版输入（供喂给 AI 对照评估）。"""
    case = get_case(case_id)
    if case is None:
        print(f"未找到 Case: {case_id}", file=sys.stderr)
        return 1
    payload = {
        "case_id": case.case_id,
        "title": case.title,
        "expected_root_cause": case.expected_root_cause,
        "expected_evidence": case.expected_evidence,
        "without": case.without_context(),
        "with": case.with_context(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_quality() -> int:
    """（可选旁证）对每个 Case 运行 QualityScorer 打分，输出 Context 完整度。

    注意：QualityScorer 评价的是 Debug Context 本身的质量，与 Benchmark 主评分
    （AI Debug 能力提升）是两个独立体系，此处仅作旁证，不混入主评分。
    """
    try:
        from app.quality.scorer import evaluate
        from app.quality.scorer import is_enabled
    except Exception as e:  # pragma: no cover - 依赖 app/quality 异常时降级
        print(f"QualityScorer 不可用（跳过旁证）：{e}", file=sys.stderr)
        return 0

    if not is_enabled():
        print("QualityScorer 未启用（quality_scoring_enabled=False），跳过旁证。")
        return 0

    for c in list_cases():
        agent_context: dict[str, Any] = {
            "debug_context": c.lujo_context,
            "repair_context": {},
        }
        report = evaluate(agent_context)
        print(
            f"[{c.case_id}] completeness={report.context_completeness.overall_score} "
            f"confidence={report.analysis_confidence.overall_score} "
            f"overall={report.overall_score}"
        )
    return 0


_USAGE = """用法:
  python -m benchmark.runner list                         # 列出全部 Case
  python -m benchmark.runner show <case_id>               # 导出单个 Case 两版输入
  python -m benchmark.runner quality                      # QualityScorer 旁证评分
  python -m benchmark.runner                              # 显示本帮助
"""


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(_USAGE)
        return 0
    cmd = argv[0]
    if cmd == "list":
        return cmd_list()
    if cmd == "show":
        if len(argv) < 2:
            print(_USAGE)
            return 1
        return cmd_show(argv[1])
    if cmd == "quality":
        return cmd_quality()
    print(f"未知命令: {cmd}", file=sys.stderr)
    print(_USAGE)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
