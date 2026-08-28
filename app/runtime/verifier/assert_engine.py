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
        # FIX: P2 值类型归一 —— JSON 反序列化后 status 可能是 str("200")
        # 或 int(200)，归一化后比较
        if not _values_equal(act_status, exp_status):
            diffs.append({"field": "status_code", "expected": exp_status, "actual": act_status})

    # body_rules: { "field.path": expected_value }
    body_rules = expect.get("body_rules") or {}
    actual_body = actual.get("body") or {}
    for field_path, expected_val in body_rules.items():
        actual_val = _get_nested_value(actual_body, field_path)
        if not _values_equal(actual_val, expected_val):
            diffs.append({"field": f"body.{field_path}", "expected": expected_val, "actual": actual_val})


def _check_ui(actual: dict, expect: dict, diffs: list) -> None:
    # state_change: { "route_change": "/target", "dom_change": ".button[disabled]" }
    state_change = expect.get("state_change") or {}
    actual_changes = actual.get("state_changes") or {}
    for key, expected_val in state_change.items():
        actual_val = actual_changes.get(key)
        if not _values_equal(actual_val, expected_val):
            diffs.append({"field": f"state_change.{key}", "expected": expected_val, "actual": actual_val})


def _check_rule(actual: dict, expect: dict, diffs: list) -> None:
    # rules: [{field, expected}]
    rules = expect.get("rules") or []
    for rule in rules:
        field = rule.get("field", "")
        # FIX: P2 expected=None 语义 —— 无 expected 键表示"不检查"（跳过），
        # 显式 expected=None 表示期望实际值为 None/缺失
        if "expected" not in rule:
            continue
        expected_val = rule.get("expected")
        actual_val = _get_nested_value(actual, field)
        if not _values_equal(actual_val, expected_val):
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
    # FIX: R7-V2 —— JSON 反序列化后 status 可能是 str("500")：此前非 int
    # 一律返回 True，5xx 被误判为静默失败（误导 AI 归因）。先归一转数值
    # 再判 4xx/5xx。
    if isinstance(status, bool):
        return True
    if isinstance(status, (str, float)):
        try:
            status = float(status)
        except (TypeError, ValueError):
            return True
    if not isinstance(status, (int, float)):
        return True
    if 400 <= status < 600:
        return False

    return True


# ── 工具函数 ──

def _values_equal(a: Any, b: Any) -> bool:
    """值相等比较，含类型归一。

    FIX: P2 值类型归一 —— JSON 反序列化/不同来源可能产生 "200"(str) vs
    200(int)、"1.5"(str) vs 1.5(float) 的错配，比较前把数值字符串归一到数值。
    bool 是 int 子类，显式排除避免 True == 1 误判。

    FIX: R7-V1 —— bool 排除必须发生在 ``a == b`` 短路之前：``True == 1``
    首行即返回 True，期望 True 实际 1 被判相等 → 断言引擎漏报（恰是它要
    检测的 silent failure 被放过）。
    """
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if a == b:
        return True
    if a is None or b is None:
        return False

    def _to_number(v: Any):
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float, str)):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        return None

    num_a = _to_number(a)
    num_b = _to_number(b)
    if num_a is not None and num_b is not None:
        return num_a == num_b
    return False


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
