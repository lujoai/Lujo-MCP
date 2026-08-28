"""SecurityAgent —— 修复方案安全审查（Phase 2 多 Agent DAG 节点）。

职责：对 RepairPlan 做安全审查，识别修复方案是否引入 LFI / SSRF / 注入 / 越权 /
敏感信息泄露等风险，输出风险清单 + 修复建议 + 整体风险等级。

设计要点：
- 依赖 RepairAgent 输出（repair_plan）；RepairPlan 缺失时返回 SKIPPED
- 复用 analyzer._get_async_client() 取 LLM 客户端（零侵入 analyzer.py）
- 重试/fallback 由 BaseAgent._call_llm 基类方法统一承担
- LLM 输出容错：_validate_security_review 缺字段补默认、超长截断、非法 severity 归 "unknown"
- 输出 AgentResult.output = {"security_review": {...}}
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.agent.base import AgentContext, AgentResult, AgentStatus, BaseAgent
from app.agent.utils import parse_llm_json, truncate_field
from app.config import settings
from app.llm.injection_guard import wrap_evidence, INJECTION_GUARD

logger = logging.getLogger("lujo-mcp.agent.security")

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
只输出 JSON，不要包含其他文字。""" + INJECTION_GUARD

VALID_SEVERITY = {"high", "medium", "low", "none"}
VALID_CATEGORIES = {
    "LFI", "SSRF", "SQLi", "CmdInjection", "PathTraversal",
    "AuthBypass", "InfoLeak", "Deserialization", "HardcodedSecret", "Other",
}
MAX_FIELD_CHARS = 4000
MAX_RAW_TRUNCATED = 800
MAX_RISKS = 20
MAX_RECOMMENDATIONS = 20


def _validate_security_review(raw_output: str) -> dict[str, Any]:
    """校验并净化 SecurityAgent 的 LLM 输出。"""
    parsed, parse_succeeded = parse_llm_json(raw_output)
    if parsed is None:
        parsed = {}

    raw_risks = parsed.get("risks", [])
    risks: list[dict[str, Any]] = []
    if isinstance(raw_risks, list):
        for risk in raw_risks[:MAX_RISKS]:
            if not isinstance(risk, dict):
                continue
            category = risk.get("category", "Other")
            if category not in VALID_CATEGORIES:
                category = "Other"
            severity = str(risk.get("severity", "")).strip().lower()
            # FIX: R7-S1 —— fail-safe 归一化。此前非法 severity 一律降为 "low"：
            # LLM 输出 prompt 声明外的 "critical" 或大小写变体 "High" 被静默降级，
            # verify_loop 安全门的 ("critical", "high") 分支对真实输出不可达
            # （安全姿态与 overall_severity 非法归 "unknown" 被拒绝不一致）。
            # 现按 fail-safe：critical→high，其余非法/缺失值保守按 high 处理，
            # 交由安全门钳制（宁可多拦，不可漏放）。
            if severity == "critical":
                severity = "high"
            if severity not in {"high", "medium", "low"}:
                severity = "high"
            risks.append(
                {
                    "category": category,
                    "severity": severity,
                    "description": truncate_field(
                        str(risk.get("description", "")), MAX_FIELD_CHARS
                    ),
                    "location": truncate_field(
                        str(risk.get("location", "")), MAX_FIELD_CHARS
                    ),
                }
            )

    raw_recommendations = parsed.get("recommendations", [])
    recommendations: list[str] = []
    if isinstance(raw_recommendations, list):
        recommendations = [
            truncate_field(str(item), MAX_FIELD_CHARS)
            for item in raw_recommendations[:MAX_RECOMMENDATIONS]
        ]

    overall = parsed.get("overall_severity", "none")
    if overall not in VALID_SEVERITY:
        overall = "unknown" if overall else "none"
        if overall == "":
            overall = "none"

    summary = parsed.get("summary", "")
    summary = truncate_field(summary, MAX_FIELD_CHARS) if summary else ""

    result: dict[str, Any] = {
        "risks": risks,
        "recommendations": recommendations,
        "overall_severity": overall,
        "summary": summary,
    }

    if not parse_succeeded:
        result["raw_truncated"] = truncate_field(raw_output, MAX_RAW_TRUNCATED)

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

            from app.llm.clients import _get_async_client

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
                    validate_fn=_validate_security_review,
                    fallback_model=settings.llm_fallback_model or "",
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
                usage=result.get("usage", {}),
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
            {"role": "user", "content": wrap_evidence(user_content)},
        ]
