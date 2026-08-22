"""Pydantic Schema 层 —— 统一数据结构定义"""

from typing import Any, Optional
from pydantic import BaseModel, Field


# ── 流程追踪步骤 ──
class TraceStep(BaseModel):
    timestamp: float
    step: str
    data: Optional[Any] = None


# ── 调试上下文 ──
class DebugContext(BaseModel):
    """Runtime Debug Context — build_debug_context() 的正式数据模型。

    v0.5: 对齐 build_debug_context() 实际输出的 20 个字段。
    新增字段全部 Optional + default，向后兼容旧数据。
    extra="allow" 支持未来扩展字段无需改 schema。
    """
    # ── 基础字段（v0.4 已有，保持不变）──
    request_id: str
    flow: list[str] = Field(default_factory=list)
    input: Optional[Any] = None
    output: Optional[Any] = None
    errors: list[Any] = Field(default_factory=list)
    exception: Optional[dict] = None
    runtime: Optional[dict] = None

    # ── v0.5 新增：Trace 元数据 ──
    trace_id: Optional[str] = None
    trace_kind: Optional[str] = None
    source: Optional[str] = None
    extra: dict = Field(default_factory=dict)

    # ── v0.5 新增：源码 & 分析证据 ──
    code_snippets: Optional[list[dict]] = None
    static_analysis: Optional[dict] = None
    git_blame: Optional[list[dict]] = None
    recent_diffs: Optional[list[dict]] = None
    related_specs: Optional[list[dict]] = None

    # ── v0.5 新增：运行时证据链 ──
    network_trace: Optional[list[dict]] = None
    ui_events: Optional[list[dict]] = None
    spec_diffs: Optional[list[dict]] = None

    # ── v0.5 新增：故障定位 ──
    fault_localization: Optional[dict] = None

    # ── v0.5.1 新增：Source Map 还原帧 ──
    # 前端 minified 帧经 source map 还原后的原始源码帧（含 original 原位置与 resolved 标记）；
    # 未启用/未命中时为 None，exception.frames 保持原始 minified 帧
    resolved_frames: Optional[list[dict]] = None

    model_config = {"extra": "allow"}


# ── 请求模型 ──
class DebugRequest(BaseModel):
    payload: Any
    metadata: Optional[dict] = None


class DebugResponse(BaseModel):
    request_id: str
    result: Any
    trace: list[TraceStep]
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


# ── Verify 请求模型（P2-2: API Schema Validation）──
class VerifyRequest(BaseModel):
    """POST /api/debug/verify 请求体。

    actual 为必填字段；spec 与 spec_id 二选一（由 handler 内部逻辑处理）。
    extra="ignore" 保证旧客户端发送的多余字段不会触发 422。
    """
    actual: dict
    spec: dict | None = None
    spec_id: str | None = None
    trace_id: str | None = None

    model_config = {"extra": "ignore"}


class VerifyUiRequest(BaseModel):
    """POST /api/debug/verify/ui 请求体。

    spec 与 spec_id 二选一（由 handler 内部逻辑处理）。
    timeout_ms 默认 30000ms。
    extra="ignore" 保证旧客户端兼容。
    """
    spec: dict | None = None
    spec_id: str | None = None
    timeout_ms: int = 30000

    model_config = {"extra": "ignore"}


# ── Source Map 上传请求模型（v0.5.1）──
class SourcemapUploadRequest(BaseModel):
    """POST /api/debug/sourcemap 请求体。

    artifact 为 JS 产物标识（如 "app.9f3b2c.js"，也接受帧文件 basename 匹配）；
    map 为完整 source map JSON 对象（至少含 mappings/sources）。
    extra="ignore" 保证旧客户端兼容。
    """
    artifact: str
    map: dict
    release: str | None = None

    model_config = {"extra": "ignore"}


# ── 异常追踪（权威定义来自 app.schemas.trace）──
from app.schemas.trace import StackFrame as StackFrame, TraceEntry as TraceEntry, TraceSummary as TraceSummary  # noqa: E402
