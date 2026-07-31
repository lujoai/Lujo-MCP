"""SecurityAgent —— 修复方案安全审查（Phase 2 多 Agent DAG 节点）。

职责：对 RepairPlan 做安全审查，识别修复方案是否引入 LFI / SSRF / 注入 / 越权 /
敏感信息泄露等风险，输出风险清单 + 修复建议 + 整体风险等级。

设计要点：
- 依赖 RepairAgent 输出（repair_plan）；RepairPlan 缺失时返回 SKIPPED
- 复用 analyzer._get_async_client() 取 LLM 客户端（零侵入 analyzer.py）
- 重试/fallback 模式参照 repair_agent.py（独立一份）
- LLM 输出容错：_validate_security_review 缺字段补默认、超长截断、非法 severity 归 "unknown"
- 输出 AgentResult.output = {"security_review": {...}}
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

logger = logging.getLogger("ai-debug-mcp.agent.security")

SYSTEM_PROMPT = """你是一位资深的安全工程师。审查以下修复方案是否存在安全风险，重点关注：
- LFI（任意文件读取）、SSRF（服务端请求伪造）
- SQL 注入、命令注入、路径穿越
- 越权访问、敏感信息泄露
- 不安全的反序列化、硬编码凭据

输出 JSON：

{
  "risks": [
    {"category": "LFI|SSRF|SQLi|CmdInjection|PathTraversal|AuthBypass|InfoLeak|Deserialization|HardcodedSecret|Other", "severity": "high|medium|low", "description": "风险描述", "location": "修复方案中的位置"}
  ],
  "recommendations": ["针对每个风险的修复建议"],
  "overall_severity": "high|medium|low|none",
  "summary": "整体安全评估摘要"
}

若修复方案无明显安全风险，risks 返回空数组，overall_severity 返回 "none"。
只输出 JSON，不要包含其他文字。"""

VALID_SEVERITY = {"high", "medium", "low", "none"}
VALID_CATEGORIES = {
    "LFI", "SSRF", "SQLi", "CmdInjection", "PathTraversal",
    "AuthBypass", "InfoLeak", "Deserialization", "HardcodedSecret", "Other",
}
MAX_FIELD_CHARS = 4000
MAX_RAW_TRUNCATED = 800
MAX_RISKS = 20
MAX_RECOMMENDATIONS = 20


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


def _validate_security_review(raw_output: str) -> dict[str, Any]:
    """校验并净化 SecurityAgent 的 LLM 输出（与 _validate_repair_plan 同模式）。"""
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

    # risks 数组：每项需校验 category / severity / description
    raw_risks = parsed.get("risks", [])
    risks: list[dict[str, Any]] = []
    if isinstance(raw_risks, list):
        for risk in raw_risks[:MAX_RISKS]:
            if not isinstance(risk, dict):
                continue
            category = risk.get("category", "Other")
            if category not in VALID_CATEGORIES:
                category = "Other"
            severity = risk.get("severity", "low")
            if severity not in {"high", "medium", "low"}:
                severity = "low"
            risks.append(
                {
                    "category": category,
                    "severity": severity,
                    "description": _truncate_field(
                        str(risk.get("description", "")), MAX_FIELD_CHARS
                    ),
                    "location": _truncate_field(
                        str(risk.get("location", "")), MAX_FIELD_CHARS
                    ),
                }
            )

    raw_recommendations = parsed.get("recommendations", [])
    recommendations: list[str] = []
    if isinstance(raw_recommendations, list):
        recommendations = [
            _truncate_field(str(item), MAX_FIELD_CHARS)
            for item in raw_recommendations[:MAX_RECOMMENDATIONS]
        ]

    overall = parsed.get("overall_severity", "none")
    if overall not in VALID_SEVERITY:
        overall = "unknown" if overall else "none"
        if overall == "":
            overall = "none"

    summary = parsed.get("summary", "")
    summary = _truncate_field(summary, MAX_FIELD_CHARS) if summary else ""

    result: dict[str, Any] = {
        "risks": risks,
        "recommendations": recommendations,
        "overall_severity": overall,
        "summary": summary,
    }

    if not parse_succeeded:
        result["raw_truncated"] = _truncate_field(raw_output, MAX_RAW_TRUNCATED)

    return result


class SecurityAgent(BaseAgent):
    """修复方案安全审查 Agent（Phase 2 DAG 节点）。

    依赖 RepairAgent 输出（repair_plan）；RepairPlan 缺失时返回 SKIPPED。
    LLM 不可用时返回 FAILED（含原因），由 Coordinator 静默降级。
    """

    name = "security"

    async def run(self, ctx: AgentContext) -> AgentResult:
        """执行安全审查。"""
        started_at = self._now()
        try:
            repair_plan = (ctx.repair_context or {}).get("repair_plan")
            if not repair_plan:
                return self._skipped(
                    started_at, "repair_plan unavailable, skip security review"
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

            review = result["analysis"]
            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.SUCCESS,
                output={"security_review": review},
                started_at=started_at,
                finished_at=self._now(),
                usage=result["usage"],
            )
        except Exception as e:
            logger.warning("SecurityAgent failed: %s", e, exc_info=True)
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
            "affected_files": repair_plan.get("affected_files", []),
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
                analysis = _validate_security_review(content)
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
                    "SecurityAgent rate limit, retrying in %ds (attempt %d/%d)",
                    wait, attempt + 1, max_retries,
                )
                if attempt < max_retries:
                    await asyncio.sleep(wait)
            except APITimeoutError as e:
                last_error = e
                logger.warning(
                    "SecurityAgent timeout, retrying (attempt %d/%d)",
                    attempt + 1, max_retries,
                )
                if attempt < max_retries:
                    await asyncio.sleep(1)
            except APIError as e:
                last_error = e
                logger.error(
                    "SecurityAgent API error on attempt %d: %s", attempt + 1, e
                )
                if attempt < max_retries:
                    await asyncio.sleep(1)

        if model != settings.llm_fallback_model and settings.llm_fallback_model:
            logger.warning(
                "SecurityAgent 主模型 %s 不可用，切换 fallback: %s",
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
            f"SecurityAgent LLM 调用失败（已重试 {max_retries} 次）: {last_error}"
        )

    @staticmethod
    def _skipped(started_at: float, reason: str) -> AgentResult:
        return AgentResult(
            agent_name="security",
            status=AgentStatus.SKIPPED,
            output={},
            error=reason,
            started_at=started_at,
            finished_at=BaseAgent._now(),
        )
