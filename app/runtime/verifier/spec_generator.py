"""
规范生成器 —— 纯函数，零依赖，极简开箱即用。

支持两种零门槛规范推导方式：
1. infer_spec_from_sample: 根据已有请求/响应样本（如 status_code, body），自动推导规范基线
2. parse_openapi_to_specs: 解析标准 OpenAPI 3.0 / Swagger 2.0 文档，自动提取 API 期望规范
"""
import logging
from typing import Any

logger = logging.getLogger("lujo-mcp.verifier.spec_generator")


def infer_spec_from_sample(target: str, sample: dict, kind: str = "api") -> dict:
    """
    根据已有的实际成功样本，自动推导出校验规范草稿。
    
    Args:
        target: 目标端点或动作，如 "/api/v1/users" 或 "submit_form"
        sample: 实际样本字典，通常含 status_code、body 等
        kind: "api" 或 "rule"
        
    Returns:
        期望规范字典 {id, kind, target, expect: {...}}
    """
    if not isinstance(sample, dict):
        return {"kind": kind, "target": target, "expect": {}}

    expect: dict[str, Any] = {}
    
    # 1. status_code 约束
    if "status_code" in sample:
        expect["status"] = sample["status_code"]
    elif "status" in sample:
        expect["status"] = sample["status"]

    # 2. 从响应体结构自动推导基础非空/类型规则
    body = sample.get("body")
    if isinstance(body, dict):
        body_rules = {}
        _extract_shallow_rules(body, body_rules, prefix="", max_depth=3)
        if body_rules:
            expect["body_rules"] = body_rules

    return {
        "kind": kind,
        "target": target,
        "expect": expect,
    }


def parse_openapi_to_specs(openapi_data: dict) -> list[dict]:
    """
    解析 OpenAPI 3.0 或 Swagger 2.0 字典，批量生成各路由的断言规范。
    
    Args:
        openapi_data: 解析后的 OpenAPI JSON/Dict
        
    Returns:
        规范列表 list[dict]
    """
    specs: list[dict] = []
    if not isinstance(openapi_data, dict):
        return specs

    paths = openapi_data.get("paths")
    if not isinstance(paths, dict):
        return specs

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in {"get", "post", "put", "delete", "patch"}:
                continue
            if not isinstance(operation, dict):
                continue
            
            # 提取 200/201 等成功期望
            responses = operation.get("responses") or {}
            success_status = None
            for code in (200, "200", 201, "201", "default"):
                if code in responses:
                    success_status = 200 if str(code) in {"200", "default"} else int(code)
                    break

            expect: dict[str, Any] = {}
            if success_status:
                expect["status"] = success_status

            target_name = f"{method.upper()} {path}"
            specs.append({
                "kind": "api",
                "target": target_name,
                "expect": expect,
                "description": operation.get("summary") or operation.get("description") or target_name,
            })

    return specs


def _extract_shallow_rules(d: dict, rules: dict, prefix: str = "", max_depth: int = 3) -> None:
    """提取高确定性的一级/二级结构键（避免过度拟合导致易碎）。"""
    if max_depth <= 0:
        return
    for k, v in d.items():
        field_path = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            _extract_shallow_rules(v, rules, field_path, max_depth - 1)
        elif v is not None:
            # 仅记录基础非空/确定值规则
            rules[field_path] = v
