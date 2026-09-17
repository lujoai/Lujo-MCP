"""稳定哈希工具（M2-B2 抽取）。

`stable_hash` / `compute_input_hash` 原本位于 `benchmark.experiment`；M2-B2 的
provider / prompting / llm_experiment 也需要它们，若继续放在 experiment 会造成
循环 import。此处独立成模块，`benchmark.experiment` 重新导出以保持既有 API。

约束：仅 Py 标准库；哈希为完整 SHA-256（不截断）；内容只来自显式传入的参数，
不含随机值、当前时间、绝对路径或机器信息。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


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


def text_hash(text: str) -> str:
    """文本（prompt / 原始响应）的完整 sha256 摘要。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = ["stable_hash", "compute_input_hash", "text_hash"]
