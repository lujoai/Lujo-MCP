"""Pydantic Schema 层 —— 统一数据结构定义"""

from typing import Any, Optional
from pydantic import BaseModel, Field


# ── 追踪 ──
class TraceEntry(BaseModel):
    timestamp: float
    step: str
    data: Optional[Any] = None


# ── 调试上下文 ──
class DebugContext(BaseModel):
    request_id: str
    flow: list[str] = Field(default_factory=list)
    input: Optional[Any] = None
    output: Optional[Any] = None
    errors: list[Any] = Field(default_factory=list)
    exception: Optional[dict] = None
    runtime: Optional[dict] = None


# ── 请求模型 ──
class DebugRequest(BaseModel):
    payload: Any
    metadata: Optional[dict] = None


class DebugResponse(BaseModel):
    request_id: str
    result: Any
    trace: list[TraceEntry]
    context: DebugContext


class AnalyzeRequest(BaseModel):
    request_id: str


class AnalyzeResponse(BaseModel):
    request_id: str
    context: DebugContext
    analysis: dict


# ── MCP 协议 ──
class MCPToolSchema(BaseModel):
    """MCP 工具定义（对齐 MCP 规范）"""
    name: str
    description: str
    inputSchema: dict


class MCPToolCallRequest(BaseModel):
    """MCP 工具调用请求（JSON-RPC 2.0 格式）"""
    jsonrpc: str = "2.0"
    id: Optional[int] = None
    method: str
    params: Optional[dict] = None


class MCPToolCallResult(BaseModel):
    """MCP 工具调用返回"""
    content: list[dict]
    isError: bool = False


class MCPInitializeResult(BaseModel):
    protocolVersion: str = "2024-11-05"
    capabilities: dict
    serverInfo: dict


# ── 会话 ──
class SessionInfo(BaseModel):
    session_id: str
    created_at: float
    last_active: float
    idle_seconds: float
    metadata: dict = Field(default_factory=dict)


class SessionListResponse(BaseModel):
    count: int
    sessions: list[SessionInfo]


# ── 运行时 ──
class RuntimeSnapshot(BaseModel):
    timestamp: float
    python: dict
    system: dict
    process: dict


# ── 健康检查 ──
class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    storage_backend: str
    llm_configured: bool


# ── 规范（Spec / Verify）─
class SpecExpect(BaseModel):
    """期望行为定义"""
    status: Optional[int] = None
    body_rules: Optional[dict] = None
    state_change: Optional[dict] = None
    interactions: Optional[list[dict]] = None
    no_response: Optional[bool] = None


class Spec(BaseModel):
    """期望规范（FR15）"""
    id: Optional[str] = None
    kind: str = "api"  # api | ui | rule
    target: str = ""
    expect: SpecExpect = Field(default_factory=SpecExpect)
    created_at: Optional[float] = None
    updated_at: Optional[float] = None


class SpecListResponse(BaseModel):
    """规范列表响应"""
    count: int
    specs: list[Spec]


class DiffItem(BaseModel):
    """单条差异"""
    field: str
    expected: Any = None
    actual: Any = None


class VerifyResult(BaseModel):
    """验证结果（FR13）"""
    matched: bool
    diffs: list[DiffItem] = Field(default_factory=list)
    silent_failure: bool = False
    error: Optional[str] = None
    trace_id: Optional[str] = None
    spec_diffs: Optional[list[DiffItem]] = None
    interactions: Optional[list[dict]] = None
