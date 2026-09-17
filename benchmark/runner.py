"""Benchmark 运行器 —— MCP Debug Context Quality Benchmark（Phase 3 D6）。

提供 CLI 入口：
- list      ：列出全部 BenchmarkCase
- show      ：导出单个 Case 的 Without / With 两版输入
- quality   ：对每个 Case 运行 QualityScorer 旁证评分（Context 完整度）
- init      ：生成 without_lujo / with_lujo 成对实验清单模板（M2-B1）
- validate  ：校验外部填写或导入的实验记录（M2-B1）
- summarize ：离线汇总已有测量结果（M2-B1）
- run       ：对已有清单执行真实 LLM 成对调用并回收响应（M2-B2）

定位：纯评估工具，独立于 app/ 生产 Layer。默认评估方式为人工对照打分
（见 docs/internal/BENCHMARK.md）。除 `run` 外全部命令纯离线；`run` 需要显式
配置 `BENCHMARK_LLM_*` 才会发起网络调用，且只采集响应、不评分。
"""

from __future__ import annotations

import json
import math
import os
import sys
from typing import Any

from benchmark.cases import get_case, list_cases
from benchmark import experiment as exp
from benchmark import llm_experiment as llm_exp
from benchmark import llm_provider
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


def cmd_show(case_id: str, *, include_gold: bool = False) -> int:
    """导出单个 Case 的 Without / With 两版输入（供喂给 AI 对照评估）。

    默认**只输出模型可见输入**（`case_id` / `without` / `with`），绝不包含
    `expected_*`、`title`、`category`——否则把标准答案连同输入一起交给被测模型，
    使对照失效。
    `include_gold=True`（CLI：`--include-gold`）为 **evaluator-only** 通道，额外
    输出 gold label，并在 stderr 明确警告不得发给被测模型。
    """
    case = get_case(case_id)
    if case is None:
        print(f"未找到 Case: {case_id}", file=sys.stderr)
        return 1
    payload: dict[str, Any] = {
        "case_id": case.case_id,
        "without": case.without_context(),
        "with": case.with_context(),
    }
    if include_gold:
        payload["title"] = case.title
        payload["expected_root_cause"] = case.expected_root_cause
        payload["expected_evidence"] = case.expected_evidence
        print(
            "警告：--include-gold 输出含标准答案（gold label），仅供评估者对照打分；"
            "严禁把本内容发给被测模型，否则 Benchmark 对照失效。",
            file=sys.stderr,
        )
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
    if not math.isfinite(opts["temperature"]):
        return None, "temperature must be a finite number"
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
    if summary.get("result_status") == "no_measurements":
        print(
            "提示: 全部指标均未测量（result_status=no_measurements）——上面是空汇总，"
            "不是实验结果。",
            file=sys.stderr,
        )
    return 0


# ── run：真实 LLM 成对实验（M2-B2）──

_RUN_OPTS_WITH_VALUE = (
    "--case-id",
    "--run-index",
    "--group",
    "--experiment-id",
    "--output",
    "--raw-dir",
    "--repo-sha",
    "--max-tokens",
    "--timeout-seconds",
    "--max-retries",
    "--context-mode",
)
_RUN_MULTI_OPTS = ("--case-id", "--run-index")


def _parse_run_args(argv: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    """解析 run 子命令参数，返回 (options, error)。"""
    if not argv:
        return None, "missing <manifest> path"
    opts: dict[str, Any] = {
        "manifest": argv[0],
        "force_rerun": False,
        "retry_failed": False,
        "dry_run": False,
    }
    multi: dict[str, list[str]] = {name: [] for name in _RUN_MULTI_OPTS}
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in ("--force-rerun", "--retry-failed", "--dry-run"):
            opts[arg[2:].replace("-", "_")] = True
            i += 1
        elif arg in _RUN_OPTS_WITH_VALUE:
            if i + 1 >= len(argv):
                return None, f"missing value for {arg}"
            if arg in multi:
                multi[arg].append(argv[i + 1])
            else:
                opts[arg[2:].replace("-", "_")] = argv[i + 1]
            i += 2
        else:
            return None, f"unknown argument: {arg}"
    opts["case_ids"] = multi["--case-id"]
    try:
        opts["run_indices"] = [int(v) for v in multi["--run-index"]]
    except ValueError:
        return None, "--run-index must be an integer"
    try:
        opts["max_tokens"] = int(opts.get("max_tokens", 1024))
        opts["max_retries"] = int(opts.get("max_retries", 0))
        opts["timeout_seconds"] = float(opts.get("timeout_seconds", 60.0))
    except (TypeError, ValueError):
        return None, "--max-tokens / --max-retries / --timeout-seconds must be numbers"
    if opts["max_tokens"] < 1:
        return None, "--max-tokens must be >= 1"
    if opts["max_retries"] < 0:
        return None, "--max-retries must be >= 0"
    if opts["timeout_seconds"] <= 0:
        return None, "--timeout-seconds must be > 0"
    group = opts.get("group", "both")
    if group not in ("both", "without", "with"):
        return None, "--group must be both | without | with"
    context_mode = opts.get("context_mode", exp.CONTEXT_MODE_FIXTURE)
    if context_mode not in exp.VALID_CONTEXT_MODES:
        return None, f"--context-mode must be one of {exp.VALID_CONTEXT_MODES}"
    opts["group"] = group
    opts["context_mode"] = context_mode
    return opts, None


def _groups_for(group: str) -> tuple[str, ...]:
    if group == "without":
        return (exp.GROUP_WITHOUT,)
    if group == "with":
        return (exp.GROUP_WITH,)
    return (exp.GROUP_WITHOUT, exp.GROUP_WITH)


def cmd_run(argv: list[str]) -> int:
    """对已有清单执行真实 LLM 成对调用（M2-B2）。"""
    opts, err = _parse_run_args(argv)
    if err is not None:
        print(f"run 参数错误: {err}", file=sys.stderr)
        return 1

    manifest_path = opts["manifest"]
    payload, load_err = _load_payload(manifest_path)
    if load_err is not None:
        print(load_err, file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print("run 需要 manifest 对象（含 records 数组），不支持裸数组", file=sys.stderr)
        return 1
    manifest: dict[str, Any] = payload

    # 全新模板可能 repo_sha 为空（init 未传 --repo-sha）；显式 --repo-sha 先回填，
    # 否则会被 validate 拒绝。已有执行结果的 Manifest 不会被此处改动。
    llm_exp.prefill_fresh_repo_sha(manifest, opts.get("repo_sha"))

    errors = exp.validate_payload(manifest)
    if errors:
        for message in errors:
            print(f"校验失败: {message}", file=sys.stderr)
        return 1

    plan = llm_exp.build_plan(
        manifest,
        case_ids=opts["case_ids"] or None,
        run_indices=opts["run_indices"] or None,
        groups=_groups_for(opts["group"]),
        experiment_id=opts.get("experiment_id"),
        skip_measured=not opts["force_rerun"],
        retry_failed=opts["retry_failed"],
    )
    summary = llm_exp.plan_summary(plan)
    print(
        f"计划执行 {summary['planned_records']} 条记录"
        f"（without={summary['by_group'][exp.GROUP_WITHOUT]},"
        f" with={summary['by_group'][exp.GROUP_WITH]}）",
        file=sys.stderr,
    )
    if opts["dry_run"]:
        print("dry-run：未发起任何模型调用，未写入任何结果。", file=sys.stderr)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if not plan:
        print("没有需要执行的记录（全部已完成，或筛选条件为空）。", file=sys.stderr)
        return 0

    # context-mode=none 与 with_lujo 冲突：在加载 provider / 联网前即拒绝，
    # 避免写出「带 Context 却标记为 none」的无效 Manifest。
    if opts["context_mode"] == exp.CONTEXT_MODE_NONE and any(
        r.get("group") == exp.GROUP_WITH for r in plan
    ):
        print(
            "context-mode='none' 只允许 --group without；with_lujo 必须使用 "
            "fixture_replay Context。",
            file=sys.stderr,
        )
        return 1

    try:
        config = llm_provider.load_config_from_env(
            override={
                "max_tokens": opts["max_tokens"],
                "max_retries": opts["max_retries"],
                "timeout_s": opts["timeout_seconds"],
            }
        )
    except llm_provider.LLMNotConfiguredError as e:
        print(f"provider 未配置: {e}", file=sys.stderr)
        return 1

    # 控制变量对齐：全新 Manifest 回填，resume 则校验一致；不一致在任何调用前失败。
    control_errors = llm_exp.reconcile_control_variables(
        manifest,
        provider_model=config.model,
        temperature=config.temperature,
        repo_sha=opts.get("repo_sha"),
    )
    if control_errors:
        for message in control_errors:
            print(f"控制变量不一致: {message}", file=sys.stderr)
        return 1

    provider = llm_provider.LLMProvider(config)
    output_path = opts.get("output") or manifest_path

    def _save(_record: dict[str, Any], _updated: dict[str, Any]) -> None:
        llm_exp.write_manifest_validated(output_path, manifest)

    try:
        llm_exp.execute_plan(
            manifest,
            plan,
            provider=provider,
            provider_model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            timeout_s=config.timeout_s,
            endpoint_host=config.endpoint_host(),
            context_mode=opts["context_mode"],
            raw_dir=opts.get("raw_dir"),
            repo_sha=opts.get("repo_sha"),
            on_record=_save,
        )
        llm_exp.write_manifest_validated(output_path, manifest)
    except ValueError as e:
        print(f"写盘被拒绝（保留既有 Manifest）: {e}", file=sys.stderr)
        return 1

    # 统计只针对**本次 plan 实际执行的槽位**，不混入历史结果；历史失败不得
    # 令本次过滤运行返回非零。
    plan_keys = {
        (r.get("experiment_id"), r.get("case_id"), r.get("run_index"), r.get("group"))
        for r in plan
    }
    executed = [
        r
        for r in manifest.get("records", [])
        if isinstance(r, dict)
        and (r.get("experiment_id"), r.get("case_id"), r.get("run_index"), r.get("group"))
        in plan_keys
    ]
    ok_count = sum(
        1 for r in executed if (r.get("execution") or {}).get("status") == "ok"
    )
    err_count = sum(
        1 for r in executed if (r.get("execution") or {}).get("error_class")
    )
    print(
        f"完成：{ok_count} 条成功，{err_count} 条失败；写入 {output_path}",
        file=sys.stderr,
    )
    print("注意：metrics 仍全部未测量，本次只采集响应，不是实验结论。", file=sys.stderr)
    return 0 if err_count == 0 else 1


_USAGE = """用法:
  python -m benchmark.runner list                         # 列出全部 Case
  python -m benchmark.runner show <case_id> [--include-gold]
                                                          # 导出两版输入（默认不含 gold）
                                                          # --include-gold 仅评估者用
  python -m benchmark.runner quality                      # QualityScorer 旁证评分
  python -m benchmark.runner init [--output F] [--force]  # 生成成对实验清单模板
  python -m benchmark.runner validate <file>              # 校验实验记录
  python -m benchmark.runner summarize <file>             # 离线汇总实验记录
  python -m benchmark.runner run <manifest> [选项]         # 执行真实 LLM 成对调用
                                                          #   --dry-run 只列计划

run 选项:
  --case-id ID        只跑指定 case（可重复；缺省全部）
  --run-index N       只跑指定 run_index（可重复；缺省全部）
  --group G           both | without | with（缺省 all）
  --experiment-id ID  只跑指定 experiment_id（缺省全部）
  --output F          输出 manifest（缺省原地更新）
  --raw-dir D         原始响应输出目录（缺省不保存原文）
  --repo-sha SHA      覆盖 records 的 repo_sha 并回填顶层
  --max-tokens N      缺省 1024      --timeout-seconds S  缺省 60
  --max-retries N     缺省 0（不做自动重试）
  --context-mode M    fixture_replay（缺省）| none
  --force-rerun       重跑已测槽位（缺省跳过）
  --retry-failed      重跑上次报错的槽位（缺省跳过）
  --dry-run           只打印计划，不联网、不写盘

provider 配置（环境变量，缺一不可；不经 .env）:
  BENCHMARK_LLM_BASE_URL / BENCHMARK_LLM_API_KEY / BENCHMARK_LLM_MODEL
  BENCHMARK_LLM_TIMEOUT（秒，缺省 60）/ BENCHMARK_LLM_TEMPERATURE（缺省 0）
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
        rest = argv[1:]
        include_gold = "--include-gold" in rest
        positional = [a for a in rest if a != "--include-gold"]
        if len(positional) != 1:
            print(_USAGE)
            return 1
        return cmd_show(positional[0], include_gold=include_gold)
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
    if cmd == "run":
        return cmd_run(argv[1:])
    print(f"未知命令: {cmd}", file=sys.stderr)
    print(_USAGE)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
