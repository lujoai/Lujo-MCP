"""
ai-debug-mcp — 基于 MCP 协议的 AI 智能调试服务
"""
import logging

import uvicorn
from fastapi import FastAPI
from contextlib import asynccontextmanager

from app.config import settings
from app.utils.logging import setup_logging
from app.middleware import setup_middleware
from app.error_handlers import setup_error_handlers
from app.observability import setup_observability
from app.mcp.core.logs import create_request_id, add_log, get_logs
from app.mcp.builders.context import build_context
from app.api.debug import router as debug_router
from app.api.mcp_routes import router as mcp_router
from app.api.ingest import router as ingest_router
from app.api.dashboard import router as dashboard_router
from app.api.spec import router as spec_router
from fastapi.responses import HTMLResponse
import pathlib

# ── 注册 MCP 工具 ──
from app.mcp.tools import register_all_tools

register_all_tools()

logger = logging.getLogger("ai-debug-mcp")


def validate_startup_configuration(host: str | None = None, api_key: str | None = None) -> None:
    """拒绝外网监听 + 无鉴权的危险启动方式。"""
    bind_host = host if host is not None else settings.host
    bind_api_key = api_key if api_key is not None else settings.api_key
    if "0.0.0.0" in str(bind_host) and not bind_api_key:
        raise RuntimeError(
            "Refusing to start: host contains 0.0.0.0 but API_KEY is empty. "
            "Set API_KEY before exposing the service."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    setup_logging()
    logger.info(
        f"服务启动 | {settings.service_name} v0.3.0 | "
        f"storage={settings.storage_backend} | "
        f"llm={settings.llm_model} | "
        f"auth={'on' if settings.api_key else 'off'} | "
        f"rate_limit={settings.rate_limit_per_minute}/min"
    )

    if not settings.api_key:
        logger.warning(
            "未配置 API_KEY，服务以【免鉴权】模式运行。"
            "生产环境请通过环境变量 API_KEY 设置访问令牌，避免未授权访问。"
        )

    # 启动定时清理任务
    import asyncio
    from app.mcp.core.storage.factory import get_trace_store, get_session_store

    # 启动期 fail-fast：主动触发 factory 校验，非法 STORAGE_BACKEND 立即崩，
    # 避免拼写错误（如 "postgrsql"）静默回退到 memory 导致生产环境数据丢失。
    # 仅 HTTP 入口覆盖；stdio 入口在首次 add_log 时触发校验。
    get_trace_store()
    get_session_store()

    async def periodic_cleanup():
        while True:
            await asyncio.sleep(300)
            try:
                get_trace_store().cleanup_expired(settings.trace_ttl_seconds)
                get_session_store().cleanup_expired(settings.session_ttl_seconds)
                from app.mcp.transports.session import registry as mcp_registry
                mcp_registry.cleanup(ttl_seconds=1800)
            except Exception:
                logger.exception("定时清理失败")

    task = asyncio.create_task(periodic_cleanup())
    yield
    task.cancel()

    # 优雅关闭：关闭 PG 连接池
    if settings.storage_backend == "postgresql":
        try:
            from app.mcp.core.storage.pg_store import close_pool
            close_pool()
        except Exception as e:
            logger.warning(f"关闭 PG 连接池失败: {e}")

    logger.info("服务已停止")


app = FastAPI(
    title=settings.service_name,
    description="基于 MCP 协议的 AI 智能调试服务",
    version="0.3.0",
    lifespan=lifespan,
)

# 中间件
# NetworkCapture 先于安全栈添加 → 位于最内层，仅记录已通过鉴权/限流/体积限制的请求
from app.middleware_network import NetworkCaptureMiddleware  # noqa: E402
app.add_middleware(NetworkCaptureMiddleware)
setup_middleware(app)

# 全局异常兜底
setup_error_handlers(app)

# 可观测性（/metrics）
setup_observability(app)

# 路由
app.include_router(debug_router)
app.include_router(mcp_router)
app.include_router(ingest_router)
app.include_router(dashboard_router)
app.include_router(spec_router)


@app.get("/")
@app.get("/health")
def health():
    """增强健康检查 —— 包含存储层可用性验证"""
    llm_ok = bool(settings.openai_api_key)

    storage_ok = True
    storage_detail = settings.storage_backend
    if settings.storage_backend == "postgresql":
        try:
            from app.mcp.core.storage.pg_store import _get_pool
            pool = _get_pool()
            conn = pool.getconn()
            cur = conn.cursor()
            cur.execute("SELECT 1")
            pool.putconn(conn)
            storage_detail = "postgresql (connected)"
        except Exception:
            storage_ok = False
            storage_detail = "postgresql (disconnected)"

    if llm_ok and storage_ok:
        status = "ok"
    elif not llm_ok and not storage_ok:
        status = "unhealthy"
    else:
        status = "degraded"

    return {
        "status": status,
        "service": settings.service_name,
        "version": "0.3.0",
        "storage": storage_detail,
        "llm_configured": llm_ok,
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """Web 控制台 —— Trace / Verify 可视化"""
    dashboard_path = pathlib.Path(__file__).parent / "web" / "dashboard.html"
    if dashboard_path.exists():
        return HTMLResponse(dashboard_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)


@app.get("/demo", response_class=HTMLResponse)
def demo():
    """网络捕获演示页面"""
    demo_path = pathlib.Path(__file__).parent / "web" / "network_capture_demo.html"
    if demo_path.exists():
        return HTMLResponse(demo_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Demo page not found</h1>", status_code=404)


@app.get("/ai-debug.js")
def ai_debug_js():
    """SDK 脚本文件"""
    sdk_path = pathlib.Path(__file__).parent.parent / "browser-sdk" / "ai-debug.js"
    if sdk_path.exists():
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(sdk_path.read_text(encoding="utf-8"), media_type="application/javascript")
    return HTMLResponse("<h1>SDK not found</h1>", status_code=404)


@app.post("/debug")
def debug(req: dict):
    """便捷调试入口"""
    request_id = create_request_id()

    try:
        add_log(request_id, "request_start", req)
        add_log(request_id, "processing")
        result = {"echo": req}
        add_log(request_id, "response_ready", result)
    except Exception as e:
        logger.error(str(e), exc_info=True)
        return {
            "request_id": request_id,
            "result": {"status": "error", "message": "Internal server error"},
            "trace": [],
            "context": {"request_id": request_id, "flow": [], "input": None, "output": None, "errors": ["Internal server error"]},
        }

    try:
        trace = get_logs(request_id)
        context = build_context(request_id, trace)
    except Exception as e:
        logger.error(str(e), exc_info=True)
        return {
            "request_id": request_id,
            "result": result,
            "trace": [],
            "context": {"request_id": request_id, "flow": [], "input": None, "output": None, "errors": ["Internal server error"]},
        }

    return {
        "request_id": request_id,
        "result": result,
        "trace": trace,
        "context": context,
    }


if __name__ == "__main__":
    validate_startup_configuration()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
