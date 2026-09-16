"""Benchmark 运行器 —— MCP Debug Context Quality Benchmark（Phase 3 D6）。

提供 CLI 入口：
- list      ：列出全部 BenchmarkCase
- show      ：导出单个 Case 的 Without / With 两版输入
- quality   ：对每个 Case 运行 QualityScorer 旁证评分（Context 完整度）
- init      ：生成 without_lujo / with_lujo 成对实验清单模板（M2-B1）
- validate  ：校验外部填写或导入的实验记录（M2-B1）
- summarize ：离线汇总已有测量结果（M2-B1）

定位：纯评估工具，独立于 app/ 生产 Layer，不引入 LLM 调用链。
默认评估方式为人工对照 4 指标打分（见 docs/internal/BENCHMARK.md）。
M2-B1 新增的三条命令为纯离线测量基础设施，不联网、不调用模型、不产出真实结果。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from benchmark.cases import get_case, list_cases
from benchmark.experiment import (
    build_manifest,
    coerce_records,
    summarize_records,
    validate_payload,
)


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


_INIT_OPTS_WITH_VALUE = (
    "--experiment-id",
    "--model",
    "--temperature",
    "--repo-sha",
    "--run-count",
)


def _parse_init_args(argv: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    """解析 init 子命令参数，返回 (options, error)。"""
    opts: dict[str, Any] = {"output": None, "force": False}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--force":
            opts["force"] = True
            i += 1
        elif arg in ("-o", "--output"):
            if i + 1 >= len(argv):
                return None, f"missing value for {arg}"
            opts["output"] = argv[i + 1]
            i += 2
        elif arg in _INIT_OPTS_WITH_VALUE:
            if i + 1 >= len(argv):
                return None, f"missing value for {arg}"
            opts[arg[2:].replace("-", "_")] = argv[i + 1]
            i += 2
        else:
            return None, f"unknown argument: {arg}"
    opts.setdefault("experiment_id", "default")
    opts.setdefault("model", "unspecified")
    opts.setdefault("temperature", 0.0)
    opts.setdefault("repo_sha", "")
    opts.setdefault("run_count", 1)
    try:
        opts["temperature"] = float(opts["temperature"])
    except (TypeError, ValueError):
        return None, "temperature must be a number"
    try:
        opts["run_count"] = int(opts["run_count"])
    except (TypeError, ValueError):
        return None, "run_count must be an integer"
    if opts["run_count"] < 1:
        return None, "run_count must be >= 1"
    return opts, None


def cmd_init(argv: list[str]) -> int:
    """生成成对实验清单模板，输出到 stdout 或 ``--output`` 文件（M2-B1）。"""
    opts, err = _parse_init_args(argv)
    if err is not None:
        print(f"init 参数错误: {err}", file=sys.stderr)
        return 1
    manifest = build_manifest(
        list_cases(),
        run_count=opts["run_count"],
        model=opts["model"],
        temperature=opts["temperature"],
        repo_sha=opts["repo_sha"],
        experiment_id=opts["experiment_id"],
    )
    blob = json.dumps(manifest, ensure_ascii=False, indent=2)
    if not opts["repo_sha"]:
        print("提示: 未提供 --repo-sha，生成的模板需回填 repo_sha 才能通过 validate", file=sys.stderr)
    output = opts["output"]
    if output is None:
        print(blob)
        return 0
    if os.path.exists(output) and not opts["force"]:
        print(f"目标文件已存在（未覆盖）：{output}", file=sys.stderr)
        return 1
    try:
        with open(output, "w", encoding="utf-8") as f:
            f.write(blob + "\n")
    except OSError as e:
        print(f"写入失败: {e}", file=sys.stderr)
        return 1
    print(f"已写入 {len(manifest['records'])} 条记录清单到 {output}")
    return 0


def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_payload(path: str) -> tuple[Any | None, str | None]:
    """读取 JSON 文件，返回 (payload, error)。"""
    try:
        return _load_json(path), None
    except (OSError, ValueError) as e:
        return None, f"读取失败: {e}"


def cmd_validate(path: str) -> int:
    """校验记录文件；合法返回 0，否则返回 1 并把错误写到 stderr（M2-B1）。"""
    payload, err = _load_payload(path)
    if err is not None:
        print(err, file=sys.stderr)
        return 1
    errors = validate_payload(payload)
    if errors:
        for message in errors:
            print(f"校验失败: {message}", file=sys.stderr)
        return 1
    records = coerce_records(payload)
    print(f"OK: {len(records)} 条记录校验通过")
    return 0


def cmd_summarize(path: str) -> int:
    """校验并汇总记录文件；失败返回非零，成功输出汇总 JSON（M2-B1）。"""
    payload, err = _load_payload(path)
    if err is not None:
        print(err, file=sys.stderr)
        return 1
    errors = validate_payload(payload)
    if errors:
        for message in errors:
            print(f"校验失败: {message}", file=sys.stderr)
        return 1
    try:
        summary = summarize_records(coerce_records(payload))
    except ValueError as e:
        print(f"校验失败:\n{e}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


_USAGE = """用法:
  python -m benchmark.runner list                         # 列出全部 Case
  python -m benchmark.runner show <case_id>               # 导出单个 Case 两版输入
  python -m benchmark.runner quality                      # QualityScorer 旁证评分
  python -m benchmark.runner init [--output F] [--force]  # 生成成对实验清单模板
  python -m benchmark.runner validate <file>              # 校验实验记录
  python -m benchmark.runner summarize <file>             # 离线汇总实验记录
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
    if cmd == "init":
        return cmd_init(argv[1:])
    if cmd == "validate":
        if len(argv) < 2:
            print(_USAGE)
            return 1
        return cmd_validate(argv[1])
    if cmd == "summarize":
        if len(argv) < 2:
            print(_USAGE)
            return 1
        return cmd_summarize(argv[1])
    print(f"未知命令: {cmd}", file=sys.stderr)
    print(_USAGE)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
