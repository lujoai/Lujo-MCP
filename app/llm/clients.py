"""OpenAI 客户端工厂 —— 同步/异步客户端惰性初始化 + provider base_url 分派。

从 analyzer.py 拆出（god object 重构）：客户端是跨模块共享的基础设施
（3 个 agent 直接使用异步客户端），独立成模块消除对 analyzer 的反向依赖。
"""

import logging
import threading
from typing import Optional

from openai import OpenAI, AsyncOpenAI

from app.config import settings

logger = logging.getLogger("lujo-mcp.llm")

_PROVIDER_BASE_URLS = {
    "openai": "",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4/",
    "deepseek": "https://api.deepseek.com",
    "custom": "",
}

_client: Optional[OpenAI] = None
_client_lock = threading.Lock()

# ── 异步 OpenAI 客户端（Phase 3.2）──
_async_client: Optional[AsyncOpenAI] = None
# _async_client_lock：threading.Lock 保护 _async_client 的双重检查锁。
# 线程安全性：threading.Lock 在模块级创建是安全的——Python GIL 确保
# Lock 对象本身的创建是原子的，import 完成后锁已就绪，后续多线程
# 共享同一把锁实例，双重检查模式保证只初始化一次 AsyncOpenAI。
_async_client_lock = threading.Lock()


def _resolve_base_url() -> str:
    """确定 base_url：显式配置优先 → provider 默认 → 空（OpenAI 默认）"""
    return settings.llm_base_url or _PROVIDER_BASE_URLS.get(settings.llm_provider, "")


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                api_key = settings.openai_api_key
                if not api_key:
                    raise RuntimeError("请在 .env 中配置有效的 OPENAI_API_KEY")

                base_url = _resolve_base_url()
                kwargs = {
                    "api_key": api_key,
                    "timeout": settings.llm_timeout,
                    "max_retries": 0,
                }
                if base_url:
                    kwargs["base_url"] = base_url

                _client = OpenAI(**kwargs)
    return _client


def _get_async_client() -> AsyncOpenAI:
    """获取 AsyncOpenAI 客户端（惰性初始化，线程安全）。

    复用 _resolve_base_url 的 provider 分派逻辑。
    """
    global _async_client
    if _async_client is None:
        with _async_client_lock:
            if _async_client is None:  # 双重检查
                api_key = settings.openai_api_key
                if not api_key:
                    raise RuntimeError("请在 .env 中配置有效的 OPENAI_API_KEY")

                base_url = _resolve_base_url()
                kwargs = {
                    "api_key": api_key,
                    "timeout": settings.llm_timeout,
                    "max_retries": 0,  # 我们自己控制重试
                }
                if base_url:
                    kwargs["base_url"] = base_url

                _async_client = AsyncOpenAI(**kwargs)
    return _async_client
