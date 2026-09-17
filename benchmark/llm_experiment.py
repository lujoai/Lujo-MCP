"""成对 LLM Debug 实验编排（M2-B2）。

职责（且仅此）：把已有 manifest 里的记录槽跑成真实模型调用结果——
- without_lujo：只有 user_description（+ 基础 prompt）
- with_lujo  ：user_description + Lujo Debug Context（+ 同一基础 prompt）

**不做**：评分（metrics 一律保持 None）、LLM Judge、Auto Patch、MCP 接入。
真实 MCP 运行时接入不在本轮范围；with 组的 context 来自 `benchmark.cases`
的 fixture，因此结果必须标记为 `context_mode="fixture_replay"`，
**不等于真实 MCP Agent 集成效果**。

metrics 只会在人工/外部评分阶段填写；本模块绝不写非 None 的 metric。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable

from benchmark.cases import get_case
from benchmark.experiment import (
    GROUP_WITH,
    GROUP_WITHOUT,
    compute_input_hash,
    validate_payload,
)
from benchmark.llm_provider import LLMProvider, LLMResult
from benchmark.prompting import (
    PROMPT_VERSION,
    build_messages,
    compute_base_prompt_hash,
    compute_context_hash,
    compute_prompt_hash,
)

RUN_MODE_DRY = "dry_run"
RUN_MODE_LIVE = "live_llm"

CONTEXT_MODE_NONE = "none"
CONTEXT_MODE_FIXTURE = "fixture_replay"
VALID_CONTEXT_MODES = (CONTEXT_MODE_NONE, CONTEXT_MODE_FIXTURE)

VALID_GROUPS_TUPLE = (GROUP_WITHOUT, GROUP_WITH)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def group_context(case: Any, group: str, *, allow_context: bool = True) -> dict[str, Any] | None:
    """按 group 返回要注入的 context；without 组**永远**返回 None。"""
    if group == GROUP_WITHOUT:
        return None
    if not allow_context:
        return None
    return case.lujo_context


def record_context_mode(group: str, context: dict[str, Any] | None, source_mode: str) -> str:
    """每条记录的 context_mode：without 恒为 none；with 记录 context 来源。"""
    if group == GROUP_WITHOUT or context is None:
        return CONTEXT_MODE_NONE
    return source_mode


def build_plan(
    manifest: dict[str, Any],
    *,
    case_ids: list[str] | None = None,
    run_indices: list[int] | None = None,
    groups: tuple[str, ...] = VALID_GROUPS_TUPLE,
    skip_measured: bool = True,
    retry_failed: bool = False,
) -> list[dict[str, Any]]:
    """列出待执行槽位（不联网、不写盘）。

    选择规则（互斥，从上到下）：
    - `skip_measured=False`（--force-rerun）：全部重跑；
    - 默认：已执行过的槽位（status=ok/error）跳过；
    - `retry_failed=True`（--retry-failed）：只重排上次带 error_class 的槽位。

    只挑选 manifest 中已存在的记录槽——绝不新增单侧记录，从而保持配对结构完整。
    """
    records = manifest.get("records") or []
    plan: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if case_ids and record.get("case_id") not in case_ids:
            continue
        if run_indices and record.get("run_index") not in run_indices:
            continue
        if record.get("group") not in groups:
            continue
        execution = record.get("execution") or {}
        status = execution.get("status")
        errored = bool(execution.get("error_class"))
        if not skip_measured:
            plan.append(record)
        elif errored:
            if retry_failed:
                plan.append(record)
        elif status == "ok":
            continue
        else:
            plan.append(record)
    return plan


def build_record_updates(
    record: dict[str, Any],
    *,
    result: LLMResult,
    provider_model: str,
    temperature: float,
    max_tokens: int,
    timeout_s: float,
    endpoint_host: str,
    context_mode: str,
    raw_response_ref: str | None = None,
    repo_sha: str | None = None,
) -> dict[str, Any]:
    """由一次调用结果构造该记录的**更新**（不写 metrics 以外的测量值）。

    失败也返回合法更新（metrics 保持 None + execution.error_class），保证配对两侧
    始终存在且可追踪；绝不把失败写成 0 分。
    """
    case_id = record["case_id"]
    group = record["group"]
    case = get_case(case_id)
    if case is None:
        raise ValueError(f"unknown case_id in manifest: {case_id!r}")

    context = group_context(case, group)
    messages = build_messages(case.user_description, context)

    updated = dict(record)
    updated["model"] = provider_model
    updated["temperature"] = temperature
    if repo_sha is not None:
        updated["repo_sha"] = repo_sha
    updated["input_hash"] = compute_input_hash(case.case_id, case.user_description)
    updated["prompt_version"] = PROMPT_VERSION
    updated["base_prompt_hash"] = compute_base_prompt_hash(
        case.case_id, case.user_description, context
    )
    updated["context_hash"] = compute_context_hash(context) if context is not None else None
    updated["context_mode"] = record_context_mode(group, context, context_mode)
    updated["prompt_hash"] = compute_prompt_hash(messages)
    updated["run_mode"] = RUN_MODE_LIVE
    updated["created_at"] = _now_iso()

    execution: dict[str, Any] = {
        "status": "ok" if result.ok else "error",
        "latency_ms": result.latency_ms,
        "attempts": result.attempts,
        "timeout_s": timeout_s,
        "max_tokens": max_tokens,
        "endpoint_host": endpoint_host,
        "finish_reason": result.finish_reason,
        "usage": result.usage or {},
        "response_sha256": result.response_sha256,
        "error_class": result.error_class,
        "error_message": result.error_message,
    }
    if result.http_status is not None:
        execution["http_status"] = result.http_status
    if result.retry_after_s is not None:
        execution["retry_after_s"] = result.retry_after_s
    if result.request_id:
        execution["request_id"] = result.request_id
    if raw_response_ref is not None:
        execution["raw_response_ref"] = raw_response_ref
    updated["execution"] = execution

    # metrics 一律写回 None——采集阶段绝不填分（防止把响应非空当成得分）。
    existing_metrics = record.get("metrics") or {}
    updated["metrics"] = dict.fromkeys(existing_metrics)
    return updated


def execute_plan(
    manifest: dict[str, Any],
    plan: list[dict[str, Any]],
    *,
    provider: LLMProvider,
    provider_model: str,
    temperature: float,
    max_tokens: int,
    timeout_s: float,
    endpoint_host: str,
    context_mode: str = CONTEXT_MODE_FIXTURE,
    raw_dir: str | None = None,
    repo_sha: str | None = None,
    on_record: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """执行计划中的槽位并原地更新 manifest（调用方负责落盘）。

    `on_record(record, updated)` 在每个槽位执行后回调（用于增量保存）。
    """
    plan_keys = {
        (r.get("experiment_id"), r.get("case_id"), r.get("run_index"), r.get("group"))
        for r in plan
    }
    records = manifest.get("records") or []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        key = (
            record.get("experiment_id"),
            record.get("case_id"),
            record.get("run_index"),
            record.get("group"),
        )
        if key not in plan_keys:
            continue
        case = get_case(record.get("case_id"))
        if case is None:
            continue
        context = group_context(case, record.get("group"))
        messages = build_messages(case.user_description, context)
        result = provider.call(messages)

        raw_ref = None
        if raw_dir is not None and result.text is not None:
            raw_ref = _write_raw_response(raw_dir, record, result.text)

        updated = build_record_updates(
            record,
            result=result,
            provider_model=provider_model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
            endpoint_host=endpoint_host,
            context_mode=context_mode,
            raw_response_ref=raw_ref,
            repo_sha=repo_sha,
        )
        records[index] = updated
        if on_record is not None:
            on_record(record, updated)
    manifest["records"] = records
    return manifest


def _write_raw_response(raw_dir: str, record: dict[str, Any], text: str) -> str:
    """把原始响应写到用户指定目录，返回**相对**标识（不泄漏绝对路径）。"""
    rel_name = (
        f"{record.get('experiment_id', 'exp')}"
        f"__{record.get('case_id', 'case')}"
        f"__r{record.get('run_index', 0)}"
        f"__{record.get('group', 'group')}.txt"
    )
    safe_name = "".join(c if (c.isalnum() or c in "._-") else "_" for c in rel_name)
    target_dir = os.path.abspath(raw_dir)
    os.makedirs(target_dir, exist_ok=True)
    with open(os.path.join(target_dir, safe_name), "w", encoding="utf-8") as f:
        f.write(text)
    return safe_name


def write_manifest_atomic(path: str, manifest: dict[str, Any]) -> None:
    """原子写 manifest（同目录 temp + fsync + os.replace），避免中断留下半文件。"""
    target = os.path.abspath(path)
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    blob = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(prefix=".benchmark-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_manifest(path: str) -> dict[str, Any]:
    """读取 manifest JSON；必须在执行前先通过 validate_payload。"""
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError("manifest must be a JSON object with a 'records' array")
    return payload


def check_manifest(manifest: dict[str, Any]) -> list[str]:
    """执行前校验（复用既有 validate_payload；含 canonical provenance）。"""
    return validate_payload(manifest)


def plan_summary(plan: list[dict[str, Any]]) -> dict[str, Any]:
    """计划的脱敏摘要（供 dry-run 与 stderr 输出）。"""
    by_group: dict[str, int] = dict.fromkeys(VALID_GROUPS_TUPLE, 0)
    for record in plan:
        group = record.get("group")
        if group in by_group:
            by_group[group] += 1
    return {
        "planned_records": len(plan),
        "by_group": by_group,
        "cases": sorted({str(r.get("case_id")) for r in plan if r.get("case_id")}),
    }


__all__ = [
    "RUN_MODE_DRY",
    "RUN_MODE_LIVE",
    "CONTEXT_MODE_NONE",
    "CONTEXT_MODE_FIXTURE",
    "VALID_CONTEXT_MODES",
    "group_context",
    "build_plan",
    "build_record_updates",
    "execute_plan",
    "write_manifest_atomic",
    "load_manifest",
    "check_manifest",
    "plan_summary",
]
