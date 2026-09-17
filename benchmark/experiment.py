"""可复现的离线 Benchmark 对照框架（M2-B1）。

纯离线、可复现、可校验的「测量基础设施」，只建设对照能力，不执行真实 AI 实验、
不产生/伪造任何真实 Benchmark 结果。

能力：
- `build_manifest`   生成 without_lujo / with_lujo 成对实验清单模板
- `validate_records` 校验外部填写或导入的实验记录（含 input_hash canonical
  来源绑定：必须等于 case_id + user_description 的 SHA-256，M2-B1.2）
- `summarize_records` 离线汇总已有测量结果（区分 None 与 0，报告覆盖率）
- `stable_hash` / `compute_input_hash` 稳定输入哈希

设计约束（对照 Architecture Frozen / AGENTS.md）：
- 仅依赖 Python 标准库 + `benchmark.cases`，不 import `app/`（避免触发 .env 读取、
  LLM、Redis、数据库、网络等副作用）。
- 未测量一律用 None 表达；真实 0 保留为 0。
- 汇总只对已测量值计算统计，无样本时统计值为 null，绝不产出虚假均值。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from benchmark.cases import get_case
from benchmark.hashing import compute_input_hash, stable_hash, text_hash  # noqa: F401

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

# 指标方向：用于 improvement_delta = sign * raw_delta（raw_delta = with - without）。
# higher-is-better 指标 sign=+1；lower-is-better 指标 sign=-1。
# 因此 improvement_delta > 0 在全部 7 项指标上恒表示 Lujo 侧更优。
_HIGHER_IS_BETTER = frozenset(
    {
        "root_cause_accuracy",
        "evidence_completeness",
        "debug_success_rate",
        "verification_success_rate",
        "repeated_bug_recall",
    }
)
_LOWER_IS_BETTER = frozenset({"time_to_diagnosis", "unsupported_guess_rate"})

# 控制变量：同一配对（experiment_id + case_id + run_index）的 two 侧必须逐字一致。
# tool_policy 是实验变量（without/with 使用 Lujo 工具的策略不同），不在此列。
_CONTROL_FIELDS = ("model", "temperature", "repo_sha", "input_hash")

# manifest 顶层字段（与 records 内重名字段需一致，若顶层存在）。
_MANIFEST_FIELDS = ("schema_version", "experiment_id", "model", "temperature", "repo_sha")

_HEX_CHARS = frozenset("0123456789abcdefABCDEF")

DEFAULT_TOOL_POLICY_WITHOUT = "no_lujo_tools"
DEFAULT_TOOL_POLICY_WITH = "lujo_mcp_tools"

# ── M2-B2 真实 LLM 实验字段（均为可选：老记录/模板缺失时不报错）──
# run_mode：dry_run = 只生成计划（绝不产 metrics）；live_llm = 真实调用过模型。
RUN_MODE_DRY = "dry_run"
RUN_MODE_LIVE = "live_llm"
VALID_RUN_MODES = (RUN_MODE_DRY, RUN_MODE_LIVE)

# context_mode：without 组必须是 none；with 组的 fixture_replay 表示 context 来自
# 手写 fixture（不是真实 MCP 运行时），结论口径必须相应降级。
CONTEXT_MODE_NONE = "none"
CONTEXT_MODE_FIXTURE = "fixture_replay"
VALID_CONTEXT_MODES = (CONTEXT_MODE_NONE, CONTEXT_MODE_FIXTURE)


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


def _is_git_object_id(value: Any) -> bool:
    """40 位 SHA-1 或 64 位 SHA-256 的完整十六进制 Git object id。"""
    return (
        isinstance(value, str)
        and len(value) in (40, 64)
        and all(c in _HEX_CHARS for c in value)
    )


def _is_sha256_hex(value: Any) -> bool:
    """64 位十六进制 SHA-256 摘要。"""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in _HEX_CHARS for c in value)
    )


def _is_iso8601_with_tz(value: Any) -> bool:
    """合法 ISO 8601 时间且必须携带时区（Z 或显式 UTC 偏移）。"""
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return False
    return dt.tzinfo is not None and dt.utcoffset() is not None


def _control_equal(field: str, a: Any, b: Any) -> bool:
    """控制变量一致比较；input_hash 是 hex 摘要，按语义大小写不敏感。"""
    if field == "input_hash" and isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    return a == b


def _has_measurement(metrics: dict[Any, Any]) -> bool:
    """该记录是否含任何已测量指标（非 None）。"""
    return any(v is not None for k, v in metrics.items() if k in METRIC_NAMES)


def validate_records(records: list[Any]) -> list[str]:
    """校验记录，返回错误信息列表（空列表 = 全部合法）。

    校验维度：
    - 单条字段：schema_version / experiment_id / case_id 存在且合法、group 枚举、
      run_index 非负整数、model / tool_policy / repo_sha / input_hash 非空、
      temperature 为实数、metrics 键合法且值 None 或符合范围。
    - input_hash 来源（M2-B1.2）：case 已知且格式合法时，必须等于该 case 的
      canonical 输入哈希（case_id + user_description 的 SHA-256，大小写不敏感
      比较）。格式非法或 case_id 未知时不做来源校验（各自单独报错）。
    - 指标范围：比率 [0,1]，时间非负；未测量用 None，真实 0 合法。
    - 配对：配对键 (experiment_id, case_id, run_index) 下恰好 1 条 without + 1 条 with。
    - 重复：同一配对键 + 同 group 出现多次即重复。
    - 控制变量：同一配对的 model / temperature / repo_sha / input_hash 一致
      （input_hash 按摘要语义大小写不敏感，其余逐字比较）。
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
        case = None
        if not _non_empty_str(case_id):
            errors.append(f"{idx} missing case_id")
        else:
            case = get_case(case_id)
            if case is None:
                errors.append(f"{idx} unknown case_id: {case_id!r}")
        if record.get("group") not in VALID_GROUPS:
            errors.append(f"{idx} invalid group: {record.get('group')!r}")
        if (
            not isinstance(record.get("run_index"), int)
            or isinstance(record.get("run_index"), bool)
            or record.get("run_index") < 0
        ):
            errors.append(f"{idx} invalid run_index: {record.get('run_index')!r}")
        metrics = record.get("metrics")
        if metrics is None:
            metrics = {}
        elif not isinstance(metrics, dict):
            errors.append(f"{idx} metrics must be an object")
            metrics = {}
        has_measurement = _has_measurement(metrics)

        model = record.get("model")
        if not _non_empty_str(model):
            errors.append(f"{idx} missing model")
        elif model.strip() == "unspecified" and has_measurement:
            errors.append(f"{idx} model must not be 'unspecified' when metrics are measured")
        if not _is_real_number(record.get("temperature")):
            errors.append(f"{idx} invalid temperature: {record.get('temperature')!r}")
        if not _non_empty_str(record.get("tool_policy")):
            errors.append(f"{idx} missing tool_policy")
        if not _is_git_object_id(record.get("repo_sha")):
            errors.append(
                f"{idx} invalid repo_sha: {record.get('repo_sha')!r} (expected 40/64-char hex)"
            )
        input_hash = record.get("input_hash")
        if not _is_sha256_hex(input_hash):
            errors.append(
                f"{idx} invalid input_hash: {input_hash!r} (expected 64-char hex)"
            )
        elif case is not None:
            expected = compute_input_hash(case.case_id, case.user_description)
            if input_hash.lower() != expected:
                errors.append(
                    f"{idx} input_hash does not match canonical input of case "
                    f"{case_id!r} (expected sha256 of case_id + user_description)"
                )

        created_at = record.get("created_at")
        if created_at is None:
            if has_measurement:
                errors.append(f"{idx} missing created_at (required when metrics are measured)")
        elif not _is_iso8601_with_tz(created_at):
            errors.append(
                f"{idx} invalid created_at: {created_at!r} (expected ISO 8601 with timezone)"
            )

        # M2-B2：真实实验记录的组间契约（字段缺失时跳过，兼容老记录与模板）。
        run_mode = record.get("run_mode")
        if run_mode is not None and run_mode not in VALID_RUN_MODES:
            errors.append(f"{idx} invalid run_mode: {run_mode!r}")
        if run_mode == RUN_MODE_DRY and has_measurement:
            errors.append(f"{idx} dry_run records must not carry measured metrics")
        context_mode = record.get("context_mode")
        if context_mode is not None and context_mode not in VALID_CONTEXT_MODES:
            errors.append(f"{idx} invalid context_mode: {context_mode!r}")
        elif context_mode is not None and case is not None:
            if record.get("group") == GROUP_WITHOUT and context_mode != CONTEXT_MODE_NONE:
                errors.append(
                    f"{idx} without_lujo record must use context_mode='none', "
                    f"got {context_mode!r}"
                )
            if record.get("group") == GROUP_WITH and context_mode == CONTEXT_MODE_NONE:
                errors.append(
                    f"{idx} with_lujo record must not use context_mode='none'"
                )
        context_hash = record.get("context_hash")
        if context_hash is not None:
            if not _is_sha256_hex(context_hash):
                errors.append(
                    f"{idx} invalid context_hash: {context_hash!r} (expected 64-char hex)"
                )
            elif case is not None:
                if record.get("group") == GROUP_WITHOUT:
                    errors.append(
                        f"{idx} without_lujo record must not carry a context_hash"
                    )
                else:
                    expected_ctx = stable_hash(case.lujo_context)
                    if context_hash.lower() != expected_ctx:
                        errors.append(
                            f"{idx} context_hash does not match canonical lujo_context "
                            f"of case {case_id!r}"
                        )
        elif record.get("group") == GROUP_WITH and record.get("context_mode") == CONTEXT_MODE_FIXTURE:
            errors.append(f"{idx} with_lujo record with fixture context must carry context_hash")

        base_prompt_hash = record.get("base_prompt_hash")
        if base_prompt_hash is not None and not _is_sha256_hex(base_prompt_hash):
            errors.append(
                f"{idx} invalid base_prompt_hash: {base_prompt_hash!r} (expected 64-char hex)"
            )
        prompt_hash = record.get("prompt_hash")
        if prompt_hash is not None and not _is_sha256_hex(prompt_hash):
            errors.append(
                f"{idx} invalid prompt_hash: {prompt_hash!r} (expected 64-char hex)"
            )

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
                if not _control_equal(field, w.get(field), wi.get(field)):
                    errors.append(
                        f"pair={pair_key} control variable mismatch on {field}"
                    )
            # M2-B2：两组必须共用同一基础 prompt，但实际发送内容必须不同，
            # 否则说明对照失效（要么漏注入 context，要么给 with 组加了额外提示）。
            # 仅当**两侧都已执行**（均有 hash）时才比较，避免半执行配对（如
            # --group without 单侧运行）被误判为不一致。
            w_base = w.get("base_prompt_hash")
            wi_base = wi.get("base_prompt_hash")
            if w_base is not None and wi_base is not None:
                if not _control_equal("base_prompt_hash", w_base, wi_base):
                    errors.append(
                        f"pair={pair_key} base_prompt_hash mismatch between without and with"
                    )
            w_prompt = w.get("prompt_hash")
            wi_prompt = wi.get("prompt_hash")
            if w_prompt is not None and wi_prompt is not None:
                if w_prompt.lower() == wi_prompt.lower():
                    errors.append(
                        f"pair={pair_key} without/with prompt_hash are identical "
                        f"(context was not injected)"
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
    pairs: dict[tuple[Any, ...], dict[str, dict]] = {}
    for r in records:
        if isinstance(r, dict):
            key = (r["experiment_id"], r["case_id"], r["run_index"])
            pairs.setdefault(key, {})[r["group"]] = r
    pair_count = len(pairs)

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

        # paired 统计：只使用两侧该指标都非 None 的配对（对照同一现场）。
        raw_deltas: list[float] = []
        improvement_deltas: list[float] = []
        paired_missing = 0
        for groups in pairs.values():
            w = groups.get(GROUP_WITHOUT)
            wi = groups.get(GROUP_WITH)
            wv = (w.get("metrics") or {}).get(name) if w else None
            wiv = (wi.get("metrics") or {}).get(name) if wi else None
            if wv is not None and wiv is not None:
                raw = wiv - wv
                raw_deltas.append(raw)
                improvement_deltas.append(raw if name in _HIGHER_IS_BETTER else -raw)
            else:
                paired_missing += 1
        paired_measured = len(raw_deltas)
        paired_coverage = (paired_measured / pair_count) if pair_count else 0.0

        metrics_summary[name] = {
            "measured": measured,
            "missing": missing,
            "coverage": coverage,
            "mean": _mean(values),
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "by_group": by_group,
            "paired_measured": paired_measured,
            "paired_missing": paired_missing,
            "paired_coverage": paired_coverage,
            "raw_delta_mean": _mean(raw_deltas),
            "raw_delta_min": min(raw_deltas) if raw_deltas else None,
            "raw_delta_max": max(raw_deltas) if raw_deltas else None,
            "improvement_delta_mean": _mean(improvement_deltas),
            "improvement_delta_min": min(improvement_deltas) if improvement_deltas else None,
            "improvement_delta_max": max(improvement_deltas) if improvement_deltas else None,
        }

    measured_any = any(
        stat["measured"] > 0 for stat in metrics_summary.values()
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "record_count": record_count,
        "pair_count": pair_count,
        # 不变量（M2-B1.1/M2-B1.2）：summarize 前置 validate_records，任何不完整
        # 配对（缺侧/重复/控制变量不一致）都在校验阶段报错并抛 ValueError，能进入
        # 此处汇总的配对必然双侧完整，故 incomplete_pairs 恒为 0。
        "incomplete_pairs": 0,
        # 结果状态横幅（M2-B2 防误读）：所有指标全为未测量时，本输出**不是**实验
        # 结果，只是空汇总；防止把模板/采集阶段输出当成"已跑出结论"。
        "result_status": "measured" if measured_any else "no_measurements",
        "metrics": metrics_summary,
    }


def validate_payload(payload: Any) -> list[str]:
    """校验 payload（纯数组或 ``{"records": [...]}``），返回错误列表。

    对 ``{"records": [...]}`` 形态额外做顶层字段一致性校验：顶层存在
    schema_version / experiment_id / model / temperature / repo_sha 时，必须与
    每条 record 的对应字段一致；顶层字段缺失则兼容（不报错）。纯数组保持
    原行为，等价于 ``validate_records``。
    """
    if isinstance(payload, list):
        return validate_records(payload)
    if isinstance(payload, dict) and "records" in payload:
        records = payload["records"]
        if not isinstance(records, list):
            return ["manifest 'records' must be an array"]
        errors: list[str] = []
        for field in _MANIFEST_FIELDS:
            if field in payload:
                top_val = payload[field]
                for i, record in enumerate(records):
                    if isinstance(record, dict) and field in record and record[field] != top_val:
                        errors.append(
                            f"manifest.{field} conflicts with record[{i}].{field}: "
                            f"{top_val!r} != {record[field]!r}"
                        )
        errors.extend(validate_records(records))
        return errors
    return ["expected a JSON array of records or an object with a 'records' array"]


__all__ = [
    "SCHEMA_VERSION",
    "GROUP_WITHOUT",
    "GROUP_WITH",
    "VALID_GROUPS",
    "RUN_MODE_DRY",
    "RUN_MODE_LIVE",
    "VALID_RUN_MODES",
    "CONTEXT_MODE_NONE",
    "CONTEXT_MODE_FIXTURE",
    "VALID_CONTEXT_MODES",
    "METRIC_NAMES",
    "DEFAULT_TOOL_POLICY_WITHOUT",
    "DEFAULT_TOOL_POLICY_WITH",
    "stable_hash",
    "compute_input_hash",
    "build_manifest",
    "coerce_records",
    "validate_records",
    "validate_payload",
    "summarize_records",
]