"""统一 Prompt 构造（M2-B2）。

两组实验（without_lujo / with_lujo）使用**逐字相同**的基础 prompt 与输出契约，
唯一差别是 with 组额外注入 Lujo Debug Context。任何 case 元数据（title /
category / expected_root_cause / expected_evidence）都不得进入 prompt——否则会
把标准答案泄漏给模型，使对照失效。

约束：仅 Py 标准库 + `benchmark.*`；不 import `app/`；无 I/O。
"""

from __future__ import annotations

import json
from typing import Any

from benchmark.hashing import stable_hash, text_hash

PROMPT_VERSION = "benchmark-debug-v1"

# 输出契约：五个必需字段。两组完全一致。
REQUIRED_RESPONSE_FIELDS: tuple[str, ...] = (
    "root_cause",
    "evidence",
    "unsupported_guesses",
    "suggested_fix",
    "verification_plan",
)

SYSTEM_PROMPT = (
    "You are a senior software engineer performing root-cause analysis on a bug report.\n"
    "You may only use the information given in this conversation.\n"
    "Do not invent facts that are not supported by the provided information; if you are\n"
    "unsure, say so explicitly and list it under unsupported_guesses.\n"
    "\n"
    "Reply with a single JSON object (no markdown fences) using exactly these keys:\n"
    '  "root_cause": string - the most likely root cause\n'
    '  "evidence": array of strings - each item must quote or point at a concrete fact\n'
    "      from the provided information that supports the root cause\n"
    '  "unsupported_guesses": array of strings - claims you cannot support with the\n'
    "      provided information\n"
    '  "suggested_fix": string - the minimal change that would fix the issue\n'
    '  "verification_plan": array of strings - how a human would verify the fix\n'
)

_BASE_INSTRUCTION = (
    "A bug report from a user:\n"
    "-----\n"
    "{user_description}\n"
    "-----\n"
)

_CONTEXT_HEADER = (
    "\nAdditional runtime context collected from the running system:\n"
    "-----\n"
)


def _context_json(context: dict[str, Any]) -> str:
    """上下文段落的规范序列化（与 stable_hash 同一规范）。"""
    return json.dumps(context, sort_keys=True, ensure_ascii=False, indent=2)


def render_user_message(user_description: str, context: dict[str, Any] | None) -> str:
    """渲染 user message；`context` 为 None 即 without 组。

    组间唯一差异：是否追加 context 段落。基础指令段逐字相同。
    """
    message = _BASE_INSTRUCTION.format(user_description=user_description)
    if context is not None:
        message += _CONTEXT_HEADER + _context_json(context) + "\n-----\n"
    return message


def build_messages(
    user_description: str, context: dict[str, Any] | None
) -> list[dict[str, str]]:
    """构造 chat messages（system + user）。"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": render_user_message(user_description, context)},
    ]


def compute_base_prompt_hash(
    case_id: str, user_description: str, context: dict[str, Any] | None
) -> str:
    """基础 prompt 哈希：**与 context 无关**，故两组必须逐字相同。

    组成 = prompt 版本 + system prompt + 基础指令模板 + case_id + user_description。
    不含 context、不含任何 case 元数据（title / expected_*）。
    """
    return stable_hash(
        {
            "prompt_version": PROMPT_VERSION,
            "system_prompt": SYSTEM_PROMPT,
            "base_instruction": _BASE_INSTRUCTION,
            "case_id": case_id,
            "user_description": user_description,
        }
    )


def compute_context_hash(context: dict[str, Any]) -> str:
    """Lujo Context 的 canonical 哈希（with 组专用）。"""
    return stable_hash(context)


def compute_prompt_hash(messages: list[dict[str, str]]) -> str:
    """实际发送消息的哈希（两组必须不同，相同说明对照失效）。"""
    return text_hash(
        json.dumps(messages, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    )


__all__ = [
    "PROMPT_VERSION",
    "REQUIRED_RESPONSE_FIELDS",
    "SYSTEM_PROMPT",
    "render_user_message",
    "build_messages",
    "compute_base_prompt_hash",
    "compute_context_hash",
    "compute_prompt_hash",
]
