"""TestAgent —— 验证策略生成（Phase 2 多 Agent DAG 节点）。

职责：基于 RepairPlan（若 RepairAgent 已成功）+ 调试上下文，生成可执行的验证策略，
包括：推荐测试用例、受影响测试文件、回归风险点。供 Coordinator 聚合到最终修复方案。

设计要点：
- 依赖 RepairAgent 的输出（repair_plan）；RepairAgent 失败时返回 SKIPPED（非 FAILED）
- 复用 analyzer._get_async_client() 取 LLM 客户端（零侵入 analyzer.py）
- 重试/fallback 模式参照 repair_agent.py（独立一份避免跨模块私有函数耦合）
- LLM 输出容错：_validate_test_plan 缺字段补默认、超长截断
- 输出 AgentResult.output = {"test_plan": {...}}
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

logger = logging.getLogger("ai-debug-mcp.agent.test")

SYSTEM_PROMPT = """你是一位资深的测试工程师。基于以下修复方案与调试上下文，生成可执行的验证策略。输出 JSON：

{
  "test_files": ["受影响的测试文件列表（绝对/相对路径）"],
  "test_cases": ["推荐新增或修改的测试用例描述"],
  "regression_risks": ["回归风险点列表"],
  "validation_steps": ["手动验证步骤（若自动化测试不足）"],
  "coverage_note": "覆盖度说明：当前测试是否充分覆盖修复点"
}

只输出 JSON，不要包含其他文字。"""

REQUIRED_FIELDS = (
    "test_files",
    "test_cases",
    "regression_risks",
    "validation_steps",
)
MAX_FIELD_CHARS = 4000
MAX_RAW_TRUNCATED = 800
MAX_LIST_ITEMS = 30


def _extract_json(content: str) -> Optional[str]:
    """从 LLM 输出中提取 JSON 字符串（与 repair_agent._extract_json 同模式）。"""
    stripped = content.strip()
    if stripped.startswith("```"):
        match = re.search(r"```(?:json)?\s*\n?(.*?)```", stripped, re.DOTALL)
        if match:
            return match.group(1).strip()
    match = re.search(r"(\{.*?\}|\[.*?\])", stripped, re.DOTALL)
    if match:
        return match.group(1)
    return None


def _truncate_field(value: str, max_chars: int) -> str:
    if not isinstance(value, str):
        value = str(value)
    return value[:max_chars] if len(value) > max_chars else value


def _validate_test_plan(raw_output: str) -> dict[str, Any]:
    """校验并净化 TestAgent 的 LLM 输出（与 _validate_repair_plan 同模式）。"""
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
        val = parsed.get(field_name, [])
        if isinstance(val, list):
            result[field_name] = [str(item) for item in val][:MAX_LIST_ITEMS]
        else:
            result[field_name] = []

    coverage_note = parsed.get("coverage_note", "")
    result["coverage_note"] = (
        _truncate_field(coverage_note, MAX_FIELD_CHARS) if coverage_note else ""
    )

    if not parse_succeeded:
        result["raw_truncated"] = _truncate_field(raw_output, MAX_RAW_TRUNCATED)

    return result


class TestAgent(BaseAgent):
    """验证策略生成 Agent（Phase 2 DAG 节点）。

    依赖 RepairAgent 输出（repair_plan）；RepairPlan 缺失时返回 SKIPPED。
    LLM 不可用时返回 FAILED（含原因），由 Coordinator 静默降级。
    """

    name = "test"

    async def run(self, ctx: AgentContext) -> AgentResult:
        """执行验证策略生成。"""
        started_at = self._now()
        try:
            # 依赖 RepairAgent 的输出（由 Coordinator 注入 ctx.repair_context.repair_plan）
            repair_plan = (ctx.repair_context or {}).get("repair_plan")
            if not repair_plan:
                return self._skipped(
                    started_at, "repair_plan unavailable, skip test plan generation"
                )

            from app.llm.analyzer import _get_async_client

            client = _get_async_client()
            model = ctx.model or settings.agent_model or settings.llm_model
            messages = self._build_messages(ctx, repair_plan)

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

            test_plan = result["analysis"]
            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.SUCCESS,
                output={"test_plan": test_plan},
                started_at=started_at,
                finished_at=self._now(),
                usage=result["usage"],
            )
        except Exception as e:
            logger.warning("TestAgent failed: %s", e, exc_info=True)
            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.FAILED,
                output={},
                error=str(e),
                started_at=started_at,
                finished_at=self._now(),
            )

    def _build_messages(
        self, ctx: AgentContext, repair_plan: dict[str, Any]
    ) -> list[dict[str, str]]:
        """构建 LLM messages：SYSTEM_PROMPT + repair_plan + 调试上下文摘要。"""
        debug_ctx = ctx.debug_context or {}
        exception = debug_ctx.get("exception") or {}
        user_payload = {
            "repair_plan": repair_plan,
            "error_type": exception.get("type", ""),
            "error_message": exception.get("message", ""),
            "stack_files": [
                f.get("file", "")
                for f in (exception.get("frames") or [])[:5]
                if f.get("file")
            ],
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
        """带重试的异步 LLM 调用（与 RepairAgent._call_llm_with_retry 同构）。"""
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
                analysis = _validate_test_plan(content)
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
                    "TestAgent rate limit, retrying in %ds (attempt %d/%d)",
                    wait, attempt + 1, max_retries,
                )
                if attempt < max_retries:
                    await asyncio.sleep(wait)
            except APITimeoutError as e:
                last_error = e
                logger.warning(
                    "TestAgent timeout, retrying (attempt %d/%d)",
                    attempt + 1, max_retries,
                )
                if attempt < max_retries:
                    await asyncio.sleep(1)
            except APIError as e:
                last_error = e
                logger.error(
                    "TestAgent API error on attempt %d: %s", attempt + 1, e
                )
                if attempt < max_retries:
                    await asyncio.sleep(1)

        # fallback 模型
        if model != settings.llm_fallback_model and settings.llm_fallback_model:
            logger.warning(
                "TestAgent 主模型 %s 不可用，切换 fallback: %s",
                model, settings.llm_fallback_model,
            )
            return await self._call_llm_with_retry(
                client,
                settings.llm_fallback_model,
                messages[:3],
                temperature,
                1,
            )

        raise RuntimeError(
            f"TestAgent LLM 调用失败（已重试 {max_retries} 次）: {last_error}"
        )

    @staticmethod
    def _skipped(started_at: float, reason: str) -> AgentResult:
        return AgentResult(
            agent_name="test",
            status=AgentStatus.SKIPPED,
            output={},
            error=reason,
            started_at=started_at,
            finished_at=BaseAgent._now(),
        )
