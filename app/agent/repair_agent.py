"""RepairAgent —— 生成结构化可执行修复方案。

复用 analyzer._get_async_client() 获取 AsyncOpenAI 客户端（零侵入 analyzer.py）。
重试/fallback 模式参照 analyzer._retry_call_async，独立一份以避免改 analyzer 公共签名。
_validate_repair_plan 参照 analyzer._validate_and_normalize 的容错 JSON 提取模式。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional

from openai import APIError, APITimeoutError, AsyncOpenAI, RateLimitError

from app.agent.base import AgentContext, AgentResult, AgentStatus, BaseAgent
from app.config import settings

logger = logging.getLogger("ai-debug-mcp.agent.repair")


SYSTEM_PROMPT = """你是一位资深的代码修复工程师。基于以下调试上下文、历史相似修复、git 近期改动，
生成可执行的修复方案。输出 JSON：

{
  "patch": "具体的代码修改方案 —— 包含文件路径、修改位置、修改前/后代码片段、修改动作",
  "affected_files": ["受影响的文件列表（绝对/相对路径）"],
  "validation_strategy": "验证策略 —— 如何验证修复有效（单测/集成测/手动验证步骤）",
  "risk_assessment": "风险评估 —— 可能引入的副作用、回归风险、影响范围",
  "confidence": "high/medium/low",
  "rationale": "修复思路的推理过程"
}

只输出 JSON，不要包含其他文字。"""

REQUIRED_FIELDS = (
    "patch",
    "affected_files",
    "validation_strategy",
    "risk_assessment",
)
VALID_CONFIDENCE = {"high", "medium", "low"}
MAX_FIELD_CHARS = 4000
MAX_RAW_TRUNCATED = 800


def _extract_json(content: str) -> Optional[str]:
    """从 LLM 输出中提取 JSON 字符串，支持 markdown code block。

    与 analyzer._extract_json 同模式，独立一份避免跨模块私有函数耦合。
    """
    stripped = content.strip()
    if stripped.startswith("```"):
        match = re.search(r"```(?:json)?\s*\n?(.*?)```", stripped, re.DOTALL)
        if match:
            return match.group(1).strip()
    match = re.search(r"(\{.*\}|\[.*\])", stripped, re.DOTALL)
    if match:
        return match.group(1)
    return None


def _truncate_field(value: str, max_chars: int) -> str:
    """截断字符串到指定长度。"""
    if not isinstance(value, str):
        value = str(value)
    return value[:max_chars] if len(value) > max_chars else value


def _validate_repair_plan(raw_output: str) -> dict[str, Any]:
    """校验并净化 RepairAgent 的 LLM 输出。

    步骤（与 analyzer._validate_and_normalize 对称）：
      1. 容错 JSON 提取（支持 markdown code block、嵌套文本）
      2. Schema 校验（必填字段齐全 + confidence 合法）
      3. 字段长度截断
      4. 仍失败返回结构化 fallback（patch 标记无法解析）
    """
    parsed: Optional[dict[str, Any]] = None
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

    if not isinstance(parsed, dict):
        parsed = {}
        parse_succeeded = False

    result: dict[str, Any] = {}
    for field_name in REQUIRED_FIELDS:
        val = parsed.get(field_name, "")
        if field_name == "affected_files":
            # affected_files 必须是 list[str]
            if isinstance(val, list):
                result[field_name] = [str(item) for item in val][:50]
            else:
                result[field_name] = []
        else:
            result[field_name] = _truncate_field(val, MAX_FIELD_CHARS) if val else ""

    confidence = parsed.get("confidence")
    if not confidence or confidence not in VALID_CONFIDENCE:
        confidence = "low"
    result["confidence"] = confidence

    rationale = parsed.get("rationale", "")
    result["rationale"] = _truncate_field(rationale, MAX_FIELD_CHARS) if rationale else ""

    if not parse_succeeded:
        result["raw_truncated"] = _truncate_field(raw_output, MAX_RAW_TRUNCATED)

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
            # 延迟导入避免循环依赖
            from app.llm.analyzer import _get_async_client

            client = _get_async_client()
            model = ctx.model or settings.agent_model or settings.llm_model
            messages = self._build_messages(ctx)

            result = await asyncio.wait_for(
                self._call_llm_with_retry(
                    client,
                    model,
                    messages,
                    settings.llm_temperature,
                    settings.agent_max_retries,
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
                usage=result["usage"],
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
        # 组装用户消息：debug_context + prior_analysis + vector_recall + git_context
        user_payload = {
            "debug_context": repair_ctx.get("debug_context", {}),
            "prior_analysis": repair_ctx.get("prior_analysis"),
            "vector_recall": repair_ctx.get("vector_recall", []),
            "git_context": repair_ctx.get("git_context", []),
        }
        user_content = json.dumps(user_payload, ensure_ascii=False, default=str)
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    async def _call_llm_with_retry(
        self,
        client: AsyncOpenAI,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_retries: int,
    ) -> dict[str, Any]:
        """带重试的异步 LLM 调用。参照 analyzer._retry_call_async 模式。

        重试 RateLimitError / APITimeoutError / APIError；耗尽抛 RuntimeError。
        fallback 模型逻辑与 analyzer 一致。
        """
        last_error: Optional[Exception] = None

        for attempt in range(max_retries + 1):
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    response_format={"type": "json_object"},
                )
                choice = response.choices[0]
                content = choice.message.content or "{}"
                analysis = _validate_repair_plan(content)
                return {
                    "analysis": analysis,
                    "model": response.model,
                    "usage": {
                        "prompt_tokens": response.usage.prompt_tokens
                        if response.usage
                        else 0,
                        "completion_tokens": response.usage.completion_tokens
                        if response.usage
                        else 0,
                        "total_tokens": response.usage.total_tokens
                        if response.usage
                        else 0,
                    },
                    "attempts": attempt + 1,
                }
            except RateLimitError as e:
                last_error = e
                wait = min(2 ** attempt, 30)
                logger.warning(
                    "RepairAgent rate limit, retrying in %ds (attempt %d/%d)",
                    wait, attempt + 1, max_retries,
                )
                if attempt < max_retries:
                    await asyncio.sleep(wait)
            except APITimeoutError as e:
                last_error = e
                logger.warning(
                    "RepairAgent timeout, retrying (attempt %d/%d)",
                    attempt + 1, max_retries,
                )
                if attempt < max_retries:
                    await asyncio.sleep(1)
            except APIError as e:
                last_error = e
                logger.error(
                    "RepairAgent API error on attempt %d: %s",
                    attempt + 1, e,
                )
                if attempt < max_retries:
                    await asyncio.sleep(1)

        # 所有重试失败，尝试 fallback 模型
        if (
            model != settings.llm_fallback_model
            and settings.llm_fallback_model
        ):
            logger.warning(
                "主模型 %s 不可用，切换到 fallback: %s",
                model, settings.llm_fallback_model,
            )
            return await self._call_llm_with_retry(
                client,
                settings.llm_fallback_model,
                messages[:3],  # fallback 时缩短 prompt
                temperature,
                1,  # 只重试 1 次
            )

        raise RuntimeError(
            f"RepairAgent LLM 调用失败（已重试 {max_retries} 次）: {last_error}"
        )
