"""
ai-debug-mcp — 基于 MCP 协议的 AI 智能调试服务
"""
import logging

import uvicorn
from fastapi import FastAPI
from contextlib import asynccontextmanager

from app import __version__
from app.config import settings
from app.utils.logging import setup_logging
from app.middleware import setup_middleware
from app.error_handlers import setup_error_handlers
from app.observability import setup_observability
from app.runtime.core.logs import create_request_id, add_log, get_logs
from app.runtime.context.builder import build_context
from app.api.debug import router as debug_router
from app.api.mcp_routes import router as mcp_router
from app.api.ingest import router as ingest_router
from app.api.dashboard import router as dashboard_router
from app.api.spec import router as spec_router
from app.api.auth import router as auth_router
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
        f"服务启动 | {settings.service_name} v{__version__} | "
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

    # SEC-03：启动期强制安全校验，覆盖所有启动方式
    # （python -m app.main / uvicorn app.main:app / gunicorn）。
    # 修复前该校验仅在 __main__ 分支调用，uvicorn 直启会绕过防护。
    validate_startup_configuration()

    # 启动定时清理任务
    import asyncio
    import uuid
    from app.runtime.core.storage.factory import get_trace_store, get_session_store
    from app.state.store import get_state_store, RedisStateStore
    
    # 启动期 fail-fast：主动触发 factory 校验，非法 STORAGE_BACKEND 立即崩，
    # 避免拼解错误（如 "postgrsql"）静默回退到 memory 导致生产环境数据丢失。
    # 仅 HTTP 入口覆盖；stdio 入口在首次 add_log 时触发校验。
    get_trace_store()
    get_session_store()
    
    # ── 分布式锁常量 ──
    _CLEANUP_INTERVAL = 300          # 清理周期（秒）
    _LOCK_KEY = "ai-debug:cleanup:lock"
    _LOCK_TTL = 310                  # 略大于清理周期，worker 崩溃后下个周期锁已过期
    
    # ── Lua 脚本：原子性“比较并删除”锁 ──
    # 防止误删其他 worker 持有的锁：先 GET 比较值，匹配才 DEL。
    # 场景：worker A 清理耗时超过 TTL → 锁过期 → worker B 获取新锁 →
    # worker A 完成后若直接 DELETE 会删掉 B 的锁。Lua 脚本在 Redis 中原子执行，
    # 确保只删除自己设置的锁。
    _UNLOCK_SCRIPT = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"

    async def periodic_cleanup():
        """定时清理过期 trace / session / MCP 会话。

        多 worker 部署时通过 Redis ``SET NX EX`` 分布式锁确保仅一个 worker
        执行清理，避免重复清理与惊群效应。单机模式（无 Redis）用
        ``asyncio.Lock`` 防同进程重复。
        """
        _local_lock = asyncio.Lock()

        while True:
            await asyncio.sleep(_CLEANUP_INTERVAL)

            # ── 抢占分布式/本地锁 ──
            store = get_state_store()
            use_redis = isinstance(store, RedisStateStore)
            acquired = False

            if use_redis:
                # 每次清理周期生成唯一 worker_id，用于锁归属判定。
                # 防止场景：锁 TTL 过期 → 另一个 worker 获取锁 → 当前 worker
                # 完成后误删他人锁。用 UUID 确保每次尝试的锁值唯一。
                _worker_id = str(uuid.uuid4())
                try:
                    acquired = bool(
                        store._r.set(_LOCK_KEY, _worker_id, nx=True, ex=_LOCK_TTL)
                    )
                except Exception:
                    logger.warning(
                        "获取清理分布式锁异常，跳过本次清理", exc_info=True
                    )
                    acquired = False
            else:
                # 单机模式：asyncio.Lock 防同进程重复
                try:
                    _local_lock.acquire_nowait()
                    acquired = True
                except RuntimeError:
                    acquired = False  # 上次清理尚未完成

            if not acquired:
                continue

            try:
                # Phase 2 过渡桥：同步 PG cleanup 用 to_thread 包装，
                # 避免阻塞事件循环。Phase 3 全异步化后移除。
                # 仅包装 PG 调用本身，不涉及锁逻辑。
                await asyncio.to_thread(
                    get_trace_store().cleanup_expired,
                    settings.trace_ttl_seconds,
                )
                await asyncio.to_thread(
                    get_session_store().cleanup_expired,
                    settings.session_ttl_seconds,
                )
                from app.mcp.transports.session import registry as mcp_registry
                mcp_registry.cleanup(ttl_seconds=1800)
            except Exception:
                logger.exception("定时清理失败")
            finally:
                # 释放锁：仅删除自己持有的锁，防止误删其他 worker 的锁。
                # 使用 Lua 脚本原子执行 GET+COMPARE+DEL，避免 GET 和 DEL 之间
                # 的竞态窗口（GET 返回自己的值 → 锁过期 → 他人获取 → DEL 删他人锁）。
                if use_redis:
                    try:
                        store._r.eval(_UNLOCK_SCRIPT, 1, _LOCK_KEY, _worker_id)
                    except Exception:
                        pass
                else:
                    if _local_lock.locked():
                        _local_lock.release()

    task = asyncio.create_task(periodic_cleanup())

    # ── P3-6 异步分析队列：启动 K 常驻消费协程 ──
    if settings.llm_async_analysis_enabled:
        from app.llm.analysis_queue import start_analysis_queue
        await start_analysis_queue()

    # ── P3-7 L3 缓存预热：启动期一次性回填 L1 + 启动定时任务 ──
    if settings.llm_cache_prewarm_enabled:
        from app.llm.cache_prewarm import (
            prewarm_once_with_timeout,
            start_prewarm_task,
        )
        prewarm_stats = await prewarm_once_with_timeout(
            settings.llm_cache_prewarm_top_n
        )
        logger.info("L3 cache prewarm (startup): %s", prewarm_stats)
        start_prewarm_task()

    # ── AI Debug Agent：启动 K 常驻消费协程（Phase 1）──
    if settings.agent_enabled:
        from app.agent.repair_queue import start_repair_queue
        await start_repair_queue()

    # ── v0.4.0 M2：加载种子知识到知识库（失败静默降级，不阻断启动）──
    try:
        from app.rag.seed_data import load_seed_data

        seed_count = load_seed_data()
        logger.info("知识库种子加载完成: %d 条", seed_count)
    except Exception:
        logger.warning("知识库种子加载失败，跳过（不影响启动）", exc_info=True)

    yield
    task.cancel()

    # ── AI Debug Agent 优雅停机：排空修复队列（限时 agent_queue_drain_timeout 秒）──
    if settings.agent_enabled:
        from app.agent.repair_queue import drain_repair_queue
        repair_drain_stats = await drain_repair_queue(settings.agent_queue_drain_timeout)
        logger.info("repair queue drain stats: %s", repair_drain_stats)

    # ── P3-7 优雅停机：取消定时预热任务（cancel + await，抑制 CancelledError）──
    if settings.llm_cache_prewarm_enabled:
        from app.llm.cache_prewarm import stop_prewarm_task
        await stop_prewarm_task()

    # ── P3-6 优雅停机：排空分析队列（限时 llm_queue_drain_timeout 秒）──
    if settings.llm_async_analysis_enabled:
        from app.llm.analysis_queue import drain_analysis_queue
        drain_stats = await drain_analysis_queue(settings.llm_queue_drain_timeout)
        logger.info("analysis queue drain stats: %s", drain_stats)

    # 优雅关闭：关闭 PG 连接池（同步 psycopg2）
    if settings.storage_backend == "postgresql":
        try:
            from app.runtime.core.storage.pg_store import close_pool
            close_pool()
        except Exception as e:
            logger.warning(f"关闭 PG 连接池失败: {e}")

    # 优雅关闭：关闭 asyncpg 连接池（Phase 3.1）
    if settings.pg_async_enabled:
        try:
            from app.runtime.core.storage.async_pg_store import close_pool as close_async_pool
            await close_async_pool()
        except Exception as e:
            logger.warning(f"关闭 asyncpg 连接池失败: {e}")

    # 优雅关闭：关闭 OTel 指标导出器
    try:
        from app.observability import shutdown_observability
        shutdown_observability()
    except Exception as e:
        logger.warning(f"关闭 OTel 失败: {e}")

    logger.info("服务已停止")


app = FastAPI(
    title=settings.service_name,
    description="基于 MCP 协议的 AI 智能调试服务",
    version=__version__,
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
app.include_router(auth_router)


@app.get("/")
@app.get("/health")
def health():
    """健康检查 —— 仅返回状态，不暴露内部配置"""
    llm_ok = bool(settings.openai_api_key)

    # 经存储抽象层探活（A1），不直接操作后端连接池
    storage_ok = True
    if settings.storage_backend == "postgresql":
        try:
            from app.runtime.core.storage.factory import get_trace_store
            storage_ok = get_trace_store().ping()
        except Exception:
            storage_ok = False

    if llm_ok and storage_ok:
        status = "ok"
    elif not llm_ok and not storage_ok:
        status = "unhealthy"
    else:
        status = "degraded"

    return {"status": status}


@app.get("/internal/health")
def internal_health():
    """详细健康检查 —— 仅集群内访问，暴露完整配置信息"""
    llm_ok = bool(settings.openai_api_key)

    storage_ok = True
    storage_detail = settings.storage_backend
    if settings.storage_backend == "postgresql":
        try:
            from app.runtime.core.storage.factory import get_trace_store
            if get_trace_store().ping():
                storage_detail = "postgresql (connected)"
            else:
                storage_ok = False
                storage_detail = "postgresql (disconnected)"
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
        "version": __version__,
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


@app.get("/demo/silent-failure", response_class=HTMLResponse)
def demo_silent_failure():
    """静默失败检测演示页面"""
    demo_path = pathlib.Path(__file__).parent / "web" / "silent_failure_demo.html"
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
