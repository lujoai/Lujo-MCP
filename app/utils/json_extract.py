"""LLM 输出 JSON 提取 —— 公共实现（v0.7.0 Minor：合并两处重复实现）。

此前 ``app/llm/output_schema._extract_json`` 与 ``app/agent/utils.extract_json``
维护着逐字符等价的两份实现（修复一处漏一处的漂移温床，第 6 轮 Minor）。
现收敛到本模块：app.utils 无 app 内部依赖，llm / agent 双向 import 均无
循环导入风险（agent.utils 直接 import output_schema 会经 app.agent.__init__
→ base → llm.clients 成环，故不能以其中一方为正本）。
"""

from __future__ import annotations

import re
from typing import Optional


def extract_json(content: str) -> Optional[str]:
    """从 LLM 输出中提取 JSON 字符串，支持 markdown code block。"""
    stripped = content.strip()
    if stripped.startswith("```"):
        match = re.search(r"```(?:json)?\s*\n?(.*?)```", stripped, re.DOTALL)
        if match:
            return match.group(1).strip()
    # 尝试找最外层 {} 或 []（非贪婪匹配，取第一个）
    match = re.search(r"(\{.*?\}|\[.*?\])", stripped, re.DOTALL)
    if match:
        return match.group(1)
    return None
