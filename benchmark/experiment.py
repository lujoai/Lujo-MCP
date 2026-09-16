"""可复现的离线 Benchmark 对照框架（M2-B1）。

纯离线、可复现、可校验的「测量基础设施」，只建设对照能力，不执行真实 AI 实验、
不产生/伪造任何真实 Benchmark 结果。

能力：
- `build_manifest`   生成 without_lujo / with_lujo 成对实验清单模板
- `validate_records` 校验外部填写或导入的实验记录
- `summarize_records` 离线汇总已有测量结果（区分 None 与 0，报告覆盖率）
- `stable_hash` / `compute_input_hash` 稳定输入哈希

设计约束（对照 Architecture Frozen / AGENTS.md）：
- 仅依赖 Python 标准库 + `benchmark.cases`，不 import `app/`（避免触发 .env 读取、
  LLM、Redis、数据库、网络等副作用）。
- 未测量一律用 None 表达；真实 0 保留为 0。
- 汇总只对已测量值计算统计，无样本时统计值为 null，绝不产出虚假均值。
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from benchmark.cases import get_case

SCHEMA_VERSION = "1.0"

GROUP_WITHOUT = "without_lujo"
GROUP_WITH = "with_lujo"
VALID_GROUPS = (GROUP_WITHOUT, GROUP_WITH)

# 指标口径：6 项比率/分数类指标闭区间 [0,1]；1 项时间类指标非负。
METRIC_NAMES: tuple[str, ...] = (
    "root_cause_accuracy",
    "evidence_completeness",
    "debug_success_rate",
    "verification_success_rate",
    "time_to_diagnosis",
    "repeated_bug_recall",
    "unsupported_guess_rate",
)
_RATIO_METRICS = frozenset(
    {
        "root_cause_accuracy",
        "evidence_completeness",
        "debug_success_rate",
        "verification_success_rate",
        "repeated_bug_recall",
        "unsupported_guess_rate",
    }
)
_DURATION_METRICS = frozenset({"time_to_diagnosis"})

# 控制变量：同一配对（experiment_id + case_id + run_index）的 two 侧必须逐字一致。
# tool_policy 是实验变量（without/with 使用 Lujo 工具的策略不同），不在此列。
_CONTROL_FIELDS = ("model", "temperature", "repo_sha", "input_hash")

DEFAULT_TOOL_POLICY_WITHOUT = "no_lujo_tools"
DEFAULT_TOOL_POLICY_WITH = "lujo_mcp_tools"


def stable_hash(payload: Any) -> str:
    """结构化输入（dict/list/scalar）的稳定 sha256 摘要。

    采用 `sort_keys=True` + `ensure_ascii=False` 保证键顺序与平台无关，返回完整
    64 位 hexdigest（不截断，参考 `app/llm/cache.py` 的碰撞教训）。
    """
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_input_hash(case_id: str, user_description: str) -> str:
    """计算「基础现场」输入哈希（不含 lujo_context）。

    without 与 with 组共享同一 case 的基础输入（user_description），故两侧
    input_hash 相同，可用于校验配对两侧是否基于同一现场。
    """
    return stable_hash({"case_id": case_id, "user_description": user_description})


def _empty_metrics() -> dict[str, None]:
    return dict.fromkeys(METRIC_NAMES)


def build_manifest(
    cases: Any,
    *,
    run_count: int = 1,
    model: str = "unspecified",
    temperature: float = 0.0,
    repo_sha: str = "",
    experiment_id: str = "default",
    tool_policy_without: str = DEFAULT_TOOL_POLICY_WITHOUT,
    tool_policy_with: str = DEFAULT_TOOL_POLICY_WITH,
) -> dict[str, Any]:
    """生成成对实验清单模板。

    对每个 case × run_index 生成 without_lujo 与 with_lujo 各一条记录模板，
    metrics 全为 None（未测量），供人工/外部填写后回填。
    """
    records: list[dict[str, Any]] = []
    case_ids: list[str] = []
    for case in cases:
        case_ids.append(case.case_id)
        input_hash = compute_input_hash(case.case_id, case.user_description)
        for run_index in range(1, run_count + 1):
            for group, tool_policy in (
                (GROUP_WITHOUT, tool_policy_without),
                (GROUP_WITH, tool_policy_with),
            ):
                records.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "experiment_id": experiment_id,
                        "case_id": case.case_id,
                        "group": group,
                        "run_index": run_index,
                        "model": model,
                        "temperature": temperature,
                        "tool_policy": tool_policy,
                        "repo_sha": repo_sha,
                        "input_hash": input_hash,
                        "created_at": None,
                        "metrics": _empty_metrics(),
                        "notes": "",
                    }
                )
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "model": model,
        "temperature": temperature,
        "repo_sha": repo_sha,
        "tool_policy_without": tool_policy_without,
        "tool_policy_with": tool_policy_with,
        "run_count": run_count,
        "case_ids": case_ids,
        "records": records,
    }


def coerce_records(payload: Any) -> list[Any]:
    """把文件内容归一化为记录数组：接受 list 或 ``{"records": [...]}``。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        return payload["records"]
    raise ValueError("expected a JSON array of records or an object with a 'records' array")


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_real_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def validate_records(records: list[Any]) -> list[str]:
    """校验记录，返回错误信息列表（空列表 = 全部合法）。

    校验维度：
    - 单条字段：schema_version / experiment_id / case_id 存在且合法、group 枚举、
      run_index 非负整数、model / tool_policy / repo_sha / input_hash 非空、
      temperature 为实数、metrics 键合法且值 None 或符合范围。
    - 指标范围：比率 [0,1]，时间非负；未测量用 None，真实 0 合法。
    - 配对：配对键 (experiment_id, case_id, run_index) 下恰好 1 条 without + 1 条 with。
    - 重复：同一配对键 + 同 group 出现多次即重复。
    - 控制变量：同一配对的 model / temperature / repo_sha / input_hash 一致。
    """
    errors: list[str] = []
    pairable: list[tuple[tuple, str, dict]] = []
    seen: set[tuple] = set()

    for i, record in enumerate(records):
        if not isinstance(record, dict):
            errors.append(f"record[{i}] must be an object")
            continue
        idx = f"record[{i}]"

        if record.get("schema_version") != SCHEMA_VERSION:
            errors.append(f"{idx} unsupported schema_version: {record.get('schema_version')!r}")
        if not _non_empty_str(record.get("experiment_id")):
            errors.append(f"{idx} missing experiment_id")
        case_id = record.get("case_id")
        if not _non_empty_str(case_id):
            errors.append(f"{idx} missing case_id")
        elif get_case(case_id) is None:
            errors.append(f"{idx} unknown case_id: {case_id!r}")
        if record.get("group") not in VALID_GROUPS:
            errors.append(f"{idx} invalid group: {record.get('group')!r}")
        if (
            not isinstance(record.get("run_index"), int)
            or isinstance(record.get("run_index"), bool)
            or record.get("run_index") < 0
        ):
            errors.append(f"{idx} invalid run_index: {record.get('run_index')!r}")
        if not _non_empty_str(record.get("model")):
            errors.append(f"{idx} missing model")
        if not _is_real_number(record.get("temperature")):
            errors.append(f"{idx} invalid temperature: {record.get('temperature')!r}")
        if not _non_empty_str(record.get("tool_policy")):
            errors.append(f"{idx} missing tool_policy")
        if not _non_empty_str(record.get("repo_sha")):
            errors.append(f"{idx} missing repo_sha")
        if not _non_empty_str(record.get("input_hash")):
            errors.append(f"{idx} missing input_hash")

        metrics = record.get("metrics")
        if metrics is None:
            metrics = {}
        elif not isinstance(metrics, dict):
            errors.append(f"{idx} metrics must be an object")
            metrics = {}
        for key, value in metrics.items():
            if key not in METRIC_NAMES:
                errors.append(f"{idx} unknown metric: {key!r}")
                continue
            if value is None:
                continue
            if not _is_real_number(value):
                errors.append(f"{idx} metric {key!r} must be None or a number")
                continue
            if key in _RATIO_METRICS and not (0.0 <= value <= 1.0):
                errors.append(f"{idx} metric {key!r} out of range [0,1]: {value}")
            if key in _DURATION_METRICS and value < 0:
                errors.append(f"{idx} metric {key!r} must be non-negative: {value}")

        exp_id = record.get("experiment_id")
        cid = record.get("case_id")
        grp = record.get("group")
        run_idx = record.get("run_index")
        if (
            _non_empty_str(exp_id)
            and _non_empty_str(cid)
            and grp in VALID_GROUPS
            and isinstance(run_idx, int)
            and not isinstance(run_idx, bool)
        ):
            pair_key = (exp_id, cid, run_idx)
            dup_key = (exp_id, cid, run_idx, grp)
            if dup_key in seen:
                errors.append(f"{idx} duplicate record for pair={pair_key} group={grp}")
            else:
                seen.add(dup_key)
                pairable.append((pair_key, grp, record))

    by_pair: dict[tuple, dict[str, dict]] = {}
    for pair_key, grp, record in pairable:
        by_pair.setdefault(pair_key, {})[grp] = record

    for pair_key, groups in by_pair.items():
        w = groups.get(GROUP_WITHOUT)
        wi = groups.get(GROUP_WITH)
        if w is None:
            errors.append(f"pair={pair_key} missing without_lujo record")
        if wi is None:
            errors.append(f"pair={pair_key} missing with_lujo record")
        if w is not None and wi is not None:
            for field in _CONTROL_FIELDS:
                if w.get(field) != wi.get(field):
                    errors.append(
                        f"pair={pair_key} control variable mismatch on {field}"
                    )

    return errors


def summarize_records(records: list[Any]) -> dict[str, Any]:
    """离线汇总记录；若校验失败则抛 ``ValueError``（不产出虚假汇总）。

    每个指标报告 measured / missing / coverage 与 mean / min / max（无样本时为
    null），并按 group 拆分。缺测值（None）计 missing，真实 0 计 measured。
    """
    errors = validate_records(records)
    if errors:
        raise ValueError("validation failed:\n" + "\n".join(errors))

    record_count = len(records)
    pair_keys = {
        (r["experiment_id"], r["case_id"], r["run_index"])
        for r in records
        if isinstance(r, dict)
    }

    metrics_summary: dict[str, Any] = {}
    for name in METRIC_NAMES:
        group_values = {g: [] for g in VALID_GROUPS}
        group_missing = dict.fromkeys(VALID_GROUPS, 0)
        values: list[float] = []
        missing = 0
        for r in records:
            m = r.get("metrics") or {}
            value = m.get(name)
            group = r["group"]
            if value is None:
                missing += 1
                group_missing[group] += 1
            else:
                values.append(value)
                group_values[group].append(value)

        measured = len(values)
        total = measured + missing
        coverage = (measured / total) if total else 0.0

        by_group: dict[str, Any] = {}
        for g in VALID_GROUPS:
            gv = group_values[g]
            gm = group_missing[g]
            gt = len(gv) + gm
            by_group[g] = {
                "measured": len(gv),
                "missing": gm,
                "coverage": (len(gv) / gt) if gt else 0.0,
                "mean": _mean(gv),
                "min": min(gv) if gv else None,
                "max": max(gv) if gv else None,
            }

        metrics_summary[name] = {
            "measured": measured,
            "missing": missing,
            "coverage": coverage,
            "mean": _mean(values),
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "by_group": by_group,
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "record_count": record_count,
        "pair_count": len(pair_keys),
        "incomplete_pairs": 0,
        "metrics": metrics_summary,
    }


__all__ = [
    "SCHEMA_VERSION",
    "GROUP_WITHOUT",
    "GROUP_WITH",
    "VALID_GROUPS",
    "METRIC_NAMES",
    "DEFAULT_TOOL_POLICY_WITHOUT",
    "DEFAULT_TOOL_POLICY_WITH",
    "stable_hash",
    "compute_input_hash",
    "build_manifest",
    "coerce_records",
    "validate_records",
    "summarize_records",
]