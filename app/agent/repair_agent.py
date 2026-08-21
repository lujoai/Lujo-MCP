"""RepairAgent —— 生成结构化可执行修复方案。

复用 analyzer._get_async_client() 获取 AsyncOpenAI 客户端（零侵入 analyzer.py）。
重试/fallback 由 BaseAgent._call_llm 基类方法统一承担。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agent.base import AgentContext, AgentResult, AgentStatus, BaseAgent
from app.agent.utils import parse_llm_json, truncate_field
from app.config import settings
from app.llm.injection_guard import wrap_evidence, INJECTION_GUARD

logger = logging.getLogger("lujo-mcp.agent.repair")


SYSTEM_PROMPT = """你是一位资深的代码修复工程师。基于以下调试上下文、历史排障经验库（debug_experience）、历史相似修复（vector_recall）、git 近期改动，
生成可执行的修复方案。若存在高匹配的历史排障经验或知识库命中文档，请优先参考其根因与修复建议。输出 JSON：

{
  "patch": "具体的代码修改方案 —— 包含文件路径、修改位置、修改前/后代码片段、修改动作",
  "affected_files": ["受影响的文件列表（绝对/相对路径）"],
  "validation_strategy": "验证策略 —— 如何验证修复有效（单测/集成测/手动验证步骤）",
  "risk_assessment": "风险评估 —— 可能引入的副作用、回归风险、影响范围",
  "confidence": "high/medium/low",
  "rationale": "修复思路的推理过程"
}

只输出 JSON，不要包含其他文字。""" + INJECTION_GUARD

REQUIRED_FIELDS = (
    "patch",
    "affected_files",
    "validation_strategy",
    "risk_assessment",
)
VALID_CONFIDENCE = {"high", "medium", "low"}
MAX_FIELD_CHARS = 4000
MAX_RAW_TRUNCATED = 800


def _validate_repair_plan(raw_output: str) -> dict[str, Any]:
    """校验并净化 RepairAgent 的 LLM 输出。"""
    parsed, parse_succeeded = parse_llm_json(raw_output)
    if parsed is None:
        parsed = {}

    result: dict[str, Any] = {}
    for field_name in REQUIRED_FIELDS:
        val = parsed.get(field_name, "")
        if field_name == "affected_files":
            if isinstance(val, list):
                result[field_name] = [str(item) for item in val][:50]
            else:
                result[field_name] = []
        else:
            result[field_name] = truncate_field(val, MAX_FIELD_CHARS) if val else ""

    confidence = parsed.get("confidence")
    if not confidence or confidence not in VALID_CONFIDENCE:
        confidence = "low"
    result["confidence"] = confidence

    rationale = parsed.get("rationale", "")
    result["rationale"] = truncate_field(rationale, MAX_FIELD_CHARS) if rationale else ""

    if not parse_succeeded:
        result["raw_truncated"] = truncate_field(raw_output, MAX_RAW_TRUNCATED)

    return result


class RepairAgent(BaseAgent):
    """生成结构化修复方案的 Agent（Phase 1 实现）。

    复用 analyzer._get_async_client() 获取 AsyncOpenAI 客户端，
    熔断器自动覆盖（analyzer._get_llm_circuit_breaker）。
    """

    name = "repair"

    async def run(self, ctx: AgentContext) -> AgentResult:
        """执行修复方案生成。失败返回 FAILED 状态，由 Coordinator 静默降级。"""
        started_at = self._now()
        try:
            from app.llm.clients import _get_async_client

            client = _get_async_client()
            model = ctx.model or settings.agent_model or settings.llm_model
            messages = self._build_messages(ctx)

            import asyncio

            result = await asyncio.wait_for(
                self._call_llm(
                    client=client,
                    model=model,
                    messages=messages,
                    temperature=settings.llm_temperature,
                    max_retries=settings.agent_max_retries,
                    validate_fn=_validate_repair_plan,
                    fallback_model=settings.llm_fallback_model or "",
                ),
                timeout=settings.agent_timeout,
            )

            repair_plan = result["analysis"]
            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.SUCCESS,
                output={"repair_plan": repair_plan},
                started_at=started_at,
                finished_at=self._now(),
                usage=result.get("usage", {}),
            )
        except Exception as e:
            logger.warning("RepairAgent failed: %s", e, exc_info=True)
            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.FAILED,
                output={},
                error=str(e),
                started_at=started_at,
                finished_at=self._now(),
            )

    def _build_messages(self, ctx: AgentContext) -> list[dict[str, str]]:
        """构建 LLM messages：SYSTEM_PROMPT + 装配后的修复上下文 JSON。"""
        repair_ctx = ctx.repair_context
        user_payload = {
            "debug_context": repair_ctx.get("debug_context", {}),
            "prior_analysis": repair_ctx.get("prior_analysis"),
            "vector_recall": repair_ctx.get("vector_recall", []),
            "debug_experience": repair_ctx.get("debug_experience"),
            "git_context": repair_ctx.get("git_context", []),
            "quality_report": repair_ctx.get("quality_report"),
            # FIX: P1-9f 上一轮 verify_loop 注入的 repair_plan，供迭代轮复用收敛；
            # 无上一轮（首轮）时为 None，保持与原行为一致
            "prior_repair_plan": repair_ctx.get("repair_plan"),
        }
        user_content = json.dumps(user_payload, ensure_ascii=False, default=str)
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": wrap_evidence(user_content)},
        ]
