"""GitAgent —— 代码归因分析（Phase 2 多 Agent DAG 节点）。

职责：对堆栈帧涉及文件做 git blame / recent diff 归因，判断错误是否由近期改动引入，
输出嫌疑 commit 列表 + 最近改动摘要，供 Coordinator 聚合参考。

设计要点（与 RepairContextAssembler._safe_get_git_context 的 fail-safe 模式一致）：
- 优先复用 repair_context.git_context（RepairContextAssembler 已并发装配），避免重复 git 调用
- 缺失时回退到 get_recent_diff 独立拉取（自带白名单 + 超时保护）
- 各堆栈帧独立 try/except，失败静默降级为空贡献
- 不依赖 LLM（纯 git 数据归因，零外部服务依赖，与 RepairAgent 解耦）
- 输出 AgentResult.output = {"suspect_commits": [...], "recent_changes": [...], "attribution": str}
- 无堆栈帧时返回 SKIPPED（非 FAILED），避免误导聚合器的失败计数
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.agent.base import AgentContext, AgentResult, AgentStatus, BaseAgent

logger = logging.getLogger("ai-debug-mcp.agent.git")

# 单次归因最多分析前 N 帧，避免串行 git 调用拖慢
_MAX_FRAMES_TO_BLAME = 3
# 每帧最多保留的嫌疑 commit 数
_MAX_SUSPECT_COMMITS_PER_FRAME = 3
# 最近改动摘要最大字符数
_MAX_RECENT_CHANGE_CHARS = 800


class GitAgent(BaseAgent):
    """代码归因 Agent：git blame/diff 判断错误是否由近期改动引入。

    纯数据驱动（不调 LLM），与 RepairAgent 解耦；作为 DAG 中与 RepairAgent 并行
    的独立节点。无堆栈帧时返回 SKIPPED，git 全部失败时返回 FAILED。
    """

    name = "git"

    async def run(self, ctx: AgentContext) -> AgentResult:
        """执行 git 归因分析。"""
        started_at = self._now()
        try:
            debug_ctx = ctx.debug_context or {}
            frames = (debug_ctx.get("exception") or {}).get("frames") or []

            if not frames:
                return self._skipped(started_at, "no stack frames to attribute")

            # 优先复用 RepairContextAssembler 已装配的 git_context，避免重复 git 调用
            preassembled = (ctx.repair_context or {}).get("git_context", [])

            suspect_commits = await self._safe_blame_frames(
                frames[:_MAX_FRAMES_TO_BLAME], preassembled
            )
            recent_changes = list(preassembled)[:_MAX_SUSPECT_COMMITS_PER_FRAME]
            attribution = self._build_attribution_summary(
                suspect_commits, recent_changes
            )

            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.SUCCESS,
                output={
                    "suspect_commits": suspect_commits,
                    "recent_changes": recent_changes,
                    "attribution": attribution,
                },
                started_at=started_at,
                finished_at=self._now(),
            )
        except Exception as e:
            logger.warning("GitAgent failed: %s", e, exc_info=True)
            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.FAILED,
                output={},
                error=str(e),
                started_at=started_at,
                finished_at=self._now(),
            )

    async def _safe_blame_frames(
        self,
        frames: list[dict[str, Any]],
        preassembled: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """对每帧做归因。优先复用 preassembled，缺失时回退 get_recent_diff。"""
        # 优先复用：若装配器已提供足够数据，直接映射到帧维度
        if preassembled:
            return self._map_preassembled_to_frames(frames, preassembled)

        # 回退：独立拉取（装配器失败或未装配时）
        from app.mcp.core.git import get_recent_diff

        results: list[dict[str, Any]] = []
        for f in frames:
            file_path = f.get("file", "")
            line_no = f.get("line") or f.get("lineno") or 0
            if not file_path:
                continue
            try:
                diff = await asyncio.to_thread(
                    get_recent_diff, file_path, _MAX_SUSPECT_COMMITS_PER_FRAME
                )
                if diff:
                    results.append(
                        {"file": file_path, "line": line_no, "diff": diff}
                    )
            except Exception:
                logger.debug(
                    "git blame failed for %s, skipping", file_path, exc_info=True
                )
        return results

    @staticmethod
    def _map_preassembled_to_frames(
        frames: list[dict[str, Any]],
        preassembled: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """将装配器返回的 git_context 映射到帧维度（按文件名匹配）。"""
        frame_files = {f.get("file", "") for f in frames if f.get("file")}
        results: list[dict[str, Any]] = []
        for change in preassembled:
            # get_recent_diff 返回 dict 含 file 字段；按文件名关联帧
            change_file = change.get("file", "") or change.get("path", "")
            if change_file and change_file in frame_files:
                line_no = next(
                    (
                        f.get("line") or f.get("lineno") or 0
                        for f in frames
                        if f.get("file") == change_file
                    ),
                    0,
                )
                results.append(
                    {"file": change_file, "line": line_no, "diff": change}
                )
            if len(results) >= _MAX_SUSPECT_COMMITS_PER_FRAME:
                break
        return results

    def _build_attribution_summary(
        self,
        suspect_commits: list[dict[str, Any]],
        recent_changes: list[dict[str, Any]],
    ) -> str:
        """构建人类可读的归因摘要。"""
        if not suspect_commits and not recent_changes:
            return "无近期 git 改动命中堆栈帧，错误可能与历史代码相关。"
        parts: list[str] = []
        if suspect_commits:
            files = {s.get("file", "?") for s in suspect_commits}
            parts.append(f"嫌疑文件 {len(files)} 个：" + ", ".join(sorted(files)))
        if recent_changes:
            parts.append(f"近期改动 {len(recent_changes)} 条记录")
        summary = "；".join(parts) + "。"
        return summary[:_MAX_RECENT_CHANGE_CHARS]

    @staticmethod
    def _skipped(started_at: float, reason: str) -> AgentResult:
        return AgentResult(
            agent_name="git",
            status=AgentStatus.SKIPPED,
            output={},
            error=reason,
            started_at=started_at,
            finished_at=BaseAgent._now(),
        )
