"""LLM 输出校验与净化 —— JSON 提取、Schema 校验、字段截断。

从 analyzer.py 拆出（god object 重构）：确保 LLM 输出符合
{root_cause, impact, fix, confidence, reasoning_chain, evidence_items} 契约。
"""

import json

# FIX(v0.7.0 Minor): extract_json 两处重复实现合一（正本在 app/utils/json_extract，
# 中性模块无循环导入风险）；_extract_json 保留为旧名别名，既有调用方与测试不变。
from app.utils.json_extract import extract_json as _extract_json

VALID_CONFIDENCE = {"high", "medium", "low"}
REQUIRED_FIELDS = ("root_cause", "impact", "fix")
MAX_FIELD_CHARS = 2000
MAX_RAW_TRUNCATED = 500


def _truncate_field(value: str, max_chars: int) -> str:
    """截断字符串到指定长度。"""
    if not isinstance(value, str):
        value = str(value)
    if len(value) > max_chars:
        return value[:max_chars]
    return value


def _validate_and_normalize(raw_output: str) -> dict:
    """
    校验并净化 LLM 输出，确保符合 {root_cause, impact, fix, confidence, reasoning_chain, evidence_items} 契约。

    步骤：
      1. 容错 JSON 提取（支持 markdown code block、嵌套文本）
      2. Schema 校验（字段齐全 + confidence 合法）
      3. 字段长度截断
      4. v0.4.0 新增 reasoning_chain / evidence_items（缺失时默认空列表，向后兼容）
      5. 仍失败返回结构化 fallback
    """
    # Step 1: 尝试解析 JSON
    parsed = None
    parse_succeeded = False
    try:
        parsed = json.loads(raw_output)
        parse_succeeded = True
    except (json.JSONDecodeError, TypeError):
        extracted = _extract_json(raw_output)
        if extracted:
            try:
                parsed = json.loads(extracted)
                parse_succeeded = True
            except (json.JSONDecodeError, TypeError):
                pass

    # null / 数组 / 基本类型 → 视为无法解析为对象
    if not isinstance(parsed, dict):
        parsed = {}
        parse_succeeded = False

    # Step 2: 字段校验与默认值
    result = {}
    for field in REQUIRED_FIELDS:
        val = parsed.get(field, "")
        result[field] = _truncate_field(val, MAX_FIELD_CHARS) if val else ""

    # confidence: 缺失或无效 → "low"
    confidence = parsed.get("confidence")
    if not confidence or confidence not in VALID_CONFIDENCE:
        confidence = "low"
    result["confidence"] = confidence

    # v0.4.0: reasoning_chain —— 推理步骤链（缺失时默认空列表，向后兼容旧输出）
    reasoning_chain = parsed.get("reasoning_chain")
    if isinstance(reasoning_chain, list):
        result["reasoning_chain"] = [
            _truncate_field(str(s), MAX_FIELD_CHARS) for s in reasoning_chain
        ]
    else:
        result["reasoning_chain"] = []

    # v0.4.0: evidence_items —— LLM 提取的证据条目（缺失时默认空列表，向后兼容）
    evidence_items = parsed.get("evidence_items")
    if isinstance(evidence_items, list):
        valid_items = []
        for item in evidence_items:
            if isinstance(item, dict):
                item_type = item.get("type", "")
                desc = _truncate_field(str(item.get("description", "")), MAX_FIELD_CHARS)
                relevance = item.get("relevance", "medium")
                if relevance not in ("high", "medium", "low"):
                    relevance = "medium"
                valid_items.append({
                    "type": _truncate_field(str(item_type), 100),
                    "description": desc,
                    "relevance": relevance,
                })
        result["evidence_items"] = valid_items[:10]  # 最多 10 条
    else:
        result["evidence_items"] = []

    # Step 3: 解析失败时添加 raw_truncated
    if not parse_succeeded:
        result["raw_truncated"] = _truncate_field(raw_output, MAX_RAW_TRUNCATED)

    return result
