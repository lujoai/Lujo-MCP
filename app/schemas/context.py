"""
运行时快照 & 调试上下文（打包给 AI 的最终结构）。
"""
from typing import Optional
from pydantic import BaseModel, Field

from app.schemas.trace import TraceEntry


class RuntimeInfo(BaseModel):
    pid: int
    cpu_percent: float
    memory_mb: float
    thread_count: int
    open_files: int
    python_version: str
    env_hint: dict = Field(default_factory=dict)  # 只挑几个非敏感的关键环境变量


class CodeSnippet(BaseModel):
    file: str
    error_line: int
    snippet: str
    found: bool = True
    # 可点击打开到对应行的 IDE 链接（如 vscode://file/...:行号），受白名单限制
    link: Optional[str] = None


class DebugContextPayload(BaseModel):
    """一次性打包给宿主 AI 的完整调试上下文"""
    trace: TraceEntry
    runtime: Optional[RuntimeInfo] = None
    code_snippets: list[CodeSnippet] = Field(default_factory=list)
    note: str = (
        "以上为原始堆栈、运行时状态与相关代码片段，未做 AI 分析。"
        "请结合项目代码库上下文自行判断根因与修复方案。"
    )
