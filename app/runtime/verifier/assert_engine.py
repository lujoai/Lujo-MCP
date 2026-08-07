"""
断言引擎核心 —— 纯函数，无副作用。

输入：一条请求/交互的实际结果 + 一份期望规范
输出：{matched, diffs, silent_failure, spec_id?, trace_id?}

支持三种 spec.kind：
- "api"：比对 status_code、body 字段
- "ui"：比对期望状态变化是否发生
- "rule"：自定义键值规则

静默失败判定：无异常、无 4xx/5xx，但比对不匹配 → silent_failure=true
"""

from typing import Any


def assert_behavior(actual: dict, spec: dict) -> dict:
    """
    比对 actual vs spec.expect。

    Args:
        actual: 实际结果，至少含 status_code、body、error 等字段。
        spec: 期望规范 {id, kind, target, expect: {...}}。

    Returns:
        {matched, diffs: [{field, expected, actual}], silent_failure, spec_id?, trace_id?}
    """
    kind = spec.get("kind", "api")
    expect = spec.get("expect") or {}
    diffs: list[dict] = []

    if kind == "api":
        _check_api(actual, expect, diffs)
    elif kind == "ui":
        _check_ui(actual, expect, diffs)
    elif kind == "rule":
        _check_rule(actual, expect, diffs)

    matched = len(diffs) == 0
    silent_failure = _judge_silent_failure(matched, actual)

    result: dict[str, Any] = {
        "matched": matched,
        "diffs": diffs,
        "silent_failure": silent_failure,
    }
    if "id" in spec:
        result["spec_id"] = spec["id"]
    if spec.get("trace_id"):
        result["trace_id"] = spec["trace_id"]

    return result


# ── kind 分发 ──

def _check_api(actual: dict, expect: dict, diffs: list) -> None:
    # status_code
    if "status" in expect:
        exp_status = expect["status"]
        act_status = actual.get("status_code")
        if act_status != exp_status:
            diffs.append({"field": "status_code", "expected": exp_status, "actual": act_status})

    # body_rules: { "field.path": expected_value }
    body_rules = expect.get("body_rules") or {}
    actual_body = actual.get("body") or {}
    for field_path, expected_val in body_rules.items():
        actual_val = _get_nested_value(actual_body, field_path)
        if actual_val != expected_val:
            diffs.append({"field": f"body.{field_path}", "expected": expected_val, "actual": actual_val})


def _check_ui(actual: dict, expect: dict, diffs: list) -> None:
    # state_change: { "route_change": "/target", "dom_change": ".button[disabled]" }
    state_change = expect.get("state_change") or {}
    actual_changes = actual.get("state_changes") or {}
    for key, expected_val in state_change.items():
        actual_val = actual_changes.get(key)
        if actual_val != expected_val:
            diffs.append({"field": f"state_change.{key}", "expected": expected_val, "actual": actual_val})


def _check_rule(actual: dict, expect: dict, diffs: list) -> None:
    # rules: [{field, expected}]
    rules = expect.get("rules") or []
    for rule in rules:
        field = rule.get("field", "")
        expected_val = rule.get("expected")
        actual_val = _get_nested_value(actual, field)
        if actual_val != expected_val:
            diffs.append({"field": field, "expected": expected_val, "actual": actual_val})


# ── 静默失败判定 ──

def _judge_silent_failure(matched: bool, actual: dict) -> bool:
    """无异常、无 4xx/5xx，但比对不匹配 → 静默失败。"""
    if matched:
        return False

    has_error = (
        actual.get("error") is not None
        or actual.get("exception") is not None
        or actual.get("traceback") is not None
    )
    if has_error:
        return False

    status = actual.get("status_code", 200)
    if status is None:
        return True  # 无状态码 → 归为静默
    if not isinstance(status, int):
        return True
    if 400 <= status < 600:
        return False

    return True


# ── 工具函数 ──

def _get_nested_value(obj: Any, path: str) -> Any:
    """
    按点号分隔路径从嵌套 dict 中取值。
    _get_nested_value({"a":{"b":1}}, "a.b") → 1
    _get_nested_value({"a":{"b":1}}, "a.c") → None
    """
    if not path or not isinstance(obj, dict):
        return None

    parts = path.split(".")
    current: Any = obj
    for key in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current