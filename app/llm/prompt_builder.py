"""调试提示词生成（FR12）—— 供 GET /api/debug/prompt 端点使用。

把清洗后的结构化调试上下文（脱敏 + 截断，复用 analyzer.build_analysis_prompt）
套用模板，生成一份可一键复制的纯文本提示词，适用于非 MCP 场景
（把真实运行现场交给任意 AI 聊天助手分析）。
"""

import logging
import string
from functools import lru_cache
from pathlib import Path
from typing import Optional

from app.llm.context_prep import build_analysis_prompt

logger = logging.getLogger(__name__)

# 内置默认模板。占位符语法遵循 string.Template：
#   $context      → build_analysis_prompt 输出的脱敏调试上下文
#   $request_id   → 调试流程 ID
# 其余文本原样保留。$context 值不会二次解析（占位符仅作用于模板本身）。
DEFAULT_PROMPT_TEMPLATE = """你是一位资深排障专家。以下是程序运行时的调试上下文，请分析并定位问题根因：

== 调试上下文（request_id: $request_id）==

$context

请基于以上上下文给出分析结论，包括：
1. 问题根因（问题出在哪一步、为什么会发生）
2. 影响面（是否会导致数据不一致 / 服务中断 / 安全风险等）
3. 修复建议（具体的代码修改方案）
4. 置信度（high / medium / low）"""


@lru_cache(maxsize=1)
def load_prompt_template(template_path: Optional[str]) -> str:
    """读取自定义提示词模板文件；为空或读取失败时回退到内置默认模板。

    模板文件需为 UTF-8 文本，支持 $context / $request_id 占位符。
    """
    if template_path:
        try:
            return Path(template_path).read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("prompt_template_path 读取失败（%s），回退内置模板: %s", template_path, e)
    return DEFAULT_PROMPT_TEMPLATE


def build_debug_prompt(context: dict, template_path: Optional[str] = None) -> str:
    """把调试上下文套用模板生成纯文本提示词。

    context 会先经 analyzer.build_analysis_prompt 做脱敏 + 截断；
    使用 safe_substitute，模板中无法识别的 $xxx 原样保留，不抛错。
    template_path 未显式传入时读取 settings.prompt_template_path（为空用内置模板）。
    """
    # context_prep 与本模块无循环依赖（拆分后不再依赖 analyzer 完整链路）
    from app.config import settings

    if template_path is None:
        template_path = settings.prompt_template_path
    template_text = load_prompt_template(template_path)
    context_text = build_analysis_prompt(context)
    return string.Template(template_text).safe_substitute(
        context=context_text,
        request_id=context.get("request_id", "N/A"),
    )
