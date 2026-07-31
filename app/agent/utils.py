"""Agent 公共工具函数 —— 消除 repair_agent / test_agent / security_agent 的重复代码。"""

from __future__ import annotations

import json
import re
from typing import Any, Optional


def extract_json(content: str) -> Optional[str]:
    """从 LLM 输出中提取 JSON 字符串，支持 markdown code block。"""
    stripped = content.strip()
    if stripped.startswith("```"):
        match = re.search(r"```(?:json)?\s*\n?(.*?)```", stripped, re.DOTALL)
        if match:
            return match.group(1).strip()
    match = re.search(r"(\{.*?\}|\[.*?\])", stripped, re.DOTALL)
    if match:
        return match.group(1)
    return None


def truncate_field(value: str, max_chars: int) -> str:
    """截断字符串到指定长度。"""
    if not isinstance(value, str):
        value = str(value)
    return value[:max_chars] if len(value) > max_chars else value


def parse_llm_json(raw_output: str) -> tuple[Optional[dict[str, Any]], bool]:
    """通用 LLM JSON 解析前导：直接 parse → extract_json 回退。

    返回 (parsed_dict, parse_succeeded)。parsed_dict 保证为 dict 或 None。
    """
    parsed: Optional[dict[str, Any]] = None
    parse_succeeded = False
    try:
        parsed = json.loads(raw_output)
        parse_succeeded = True
    except (json.JSONDecodeError, TypeError):
        extracted = extract_json(raw_output)
        if extracted:
            try:
                parsed = json.loads(extracted)
                parse_succeeded = True
            except (json.JSONDecodeError, TypeError):
                pass

    if not isinstance(parsed, dict):
        return None, False
    return parsed, parse_succeeded
