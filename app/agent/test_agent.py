"""TestAgent —— 验证策略生成（Phase 2 多 Agent DAG 节点）。

职责：基于 RepairPlan（若 RepairAgent 已成功）+ 调试上下文，生成可执行的验证策略，
包括：推荐测试用例、受影响测试文件、回归风险点。供 Coordinator 聚合到最终修复方案。

设计要点：
- 依赖 RepairAgent 的输出（repair_plan）；RepairAgent 失败时返回 SKIPPED（非 FAILED）
- 复用 analyzer._get_async_client() 取 LLM 客户端（零侵入 analyzer.py）
- 重试/fallback 由 BaseAgent._call_llm 基类方法统一承担
- LLM 输出容错：_validate_test_plan 缺字段补默认、超长截断
- 输出 AgentResult.output = {"test_plan": {...}}
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from app.agent.base import AgentContext, AgentResult, AgentStatus, BaseAgent
from app.agent.utils import parse_llm_json, truncate_field
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


def _validate_test_plan(raw_output: str) -> dict[str, Any]:
    """校验并净化 TestAgent 的 LLM 输出。"""
    parsed, parse_succeeded = parse_llm_json(raw_output)
    if parsed is None:
        parsed = {}

    result: dict[str, Any] = {}
    for field_name in REQUIRED_FIELDS:
        val = parsed.get(field_name, [])
        if isinstance(val, list):
            result[field_name] = [str(item) for item in val][:MAX_LIST_ITEMS]
        else:
            result[field_name] = []

    coverage_note = parsed.get("coverage_note", "")
    result["coverage_note"] = (
        truncate_field(coverage_note, MAX_FIELD_CHARS) if coverage_note else ""
    )

    if not parse_succeeded:
        result["raw_truncated"] = truncate_field(raw_output, MAX_RAW_TRUNCATED)

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
                self._call_llm(
                    client=client,
                    model=model,
                    messages=messages,
                    temperature=settings.llm_temperature,
                    max_retries=settings.agent_max_retries,
                    validate_fn=_validate_test_plan,
                    fallback_model=settings.llm_fallback_model or "",
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
                usage=result.get("usage", {}),
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
