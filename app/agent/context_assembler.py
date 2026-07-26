"""修复上下文装配器 —— 并发聚合 debug context + 历史修复 + git diff + 先验分析。

设计要点（与 build_debug_context 各 collector 的 fail-safe 模式一致）：
- 三个子装配并发执行（asyncio.gather + asyncio.to_thread），缩短延迟
- 各子装配独立 try/except，失败静默降级，不阻断整体
- 复用 analyzer.analyze_async / knowledge_base.retrieve_similar / git.get_recent_diff
  零侵入主链路
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.config import settings

logger = logging.getLogger("ai-debug-mcp.agent.assembler")


class RepairContextAssembler:
    """装配修复上下文：debug_context + 向量召回 + git diff + 基础 LLM 分析。

    所有子装配失败静默降级，RepairAgent 仍可基于原始 debug_context 生成方案
    （虽质量略降，但保证可用性）。
    """

    async def assemble(self, debug_context: dict[str, Any]) -> dict[str, Any]:
        """并发执行三个独立子装配，返回聚合后的修复上下文。"""
        # 并发执行：prior_analysis / vector_recall / git_context
        # 各 _safe_* 方法内部吞异常，永不抛出（return_exceptions=False 安全）
        analysis, vector_recall, git_context = await asyncio.gather(
            self._safe_get_analysis(debug_context),
            self._safe_vector_recall(debug_context),
            self._safe_get_git_context(debug_context),
        )

        return {
            "debug_context": debug_context,
            "prior_analysis": analysis,
            "vector_recall": vector_recall,
            "git_context": git_context,
            "sources": {
                "vector_recall": vector_recall,
                "git_context": git_context,
                "knowledge_base_hit": bool(
                    analysis and analysis.get("knowledge_base_hit")
                ),
            },
        }

    async def _safe_get_analysis(
        self, ctx: dict[str, Any]
    ) -> dict[str, Any] | None:
        """复用 analyzer.analyze_async() 取先验分析。

        可通过 settings.agent_prior_analysis_enabled 关闭（节省 LLM 调用）。
        analyzer 内部的 KB 命中 / L1/L2 缓存 / 向量召回对本方法透明。
        """
        if not settings.agent_prior_analysis_enabled:
            return None
        try:
            from app.llm.analyzer import analyze_async

            return await analyze_async(ctx)
        except Exception:
            logger.warning("prior analysis failed, continuing without", exc_info=True)
            return None

    async def _safe_vector_recall(
        self, ctx: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """向量召回历史相似修复。复用 knowledge_base.retrieve_similar。

        vector_store 关闭时（NullVectorStore）返回 []，自动降级。
        """
        try:
            from app.rag.knowledge_base import retrieve_similar

            query = json.dumps(ctx, ensure_ascii=False, default=str)
            # retrieve_similar 是同步函数，用 to_thread 避免阻塞事件循环
            return await asyncio.to_thread(retrieve_similar, query)
        except Exception:
            logger.warning("vector recall failed", exc_info=True)
            return []

    async def _safe_get_git_context(
        self, ctx: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """拉取堆栈前 3 帧的 git recent diff。复用 git.get_recent_diff。

        git 模块自带白名单 + 超时保护，无需重复实现安全控制。
        """
        try:
            from app.mcp.core.git import get_recent_diff

            frames = (ctx.get("exception") or {}).get("frames") or []
            results: list[dict[str, Any]] = []
            # 仅前 3 帧，避免串行 git 调用拖慢
            for f in frames[:3]:
                file_path = f.get("file", "")
                if not file_path:
                    continue
                d = await asyncio.to_thread(get_recent_diff, file_path, 3)
                if d:
                    results.append(d)
            return results
        except Exception:
            logger.warning("git context assembly failed", exc_info=True)
            return []
