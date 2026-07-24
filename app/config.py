"""统一配置管理 —— 全局单例，替代散落的 os.getenv()"""

import logging
from pathlib import Path
from typing import Optional

from pydantic import ConfigDict
from pydantic_settings import BaseSettings

# 项目根目录（app/ 的上一级）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = ConfigDict(
        extra="ignore",
        env_file=str(_PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
    )

    # ── LLM ──
    # provider: "openai" | "zhipu" | "custom"
    # openai → 默认 https://api.openai.com/v1
    # zhipu  → 自动设置 base_url = https://open.bigmodel.cn/api/paas/v4/，model 推荐 glm-4-flash
    # custom → 需自行填写 llm_base_url
    llm_provider: str = "openai"
    openai_api_key: str = ""
    llm_model: str = "gpt-4o"
    llm_timeout: int = 30
    llm_max_retries: int = 3
    llm_temperature: float = 0.3
    # 备用模型（主模型不可用时 fallback）
    llm_fallback_model: str = "gpt-4o-mini"
    # 自定义 base_url（留空则按 llm_provider 自动选；填写后覆盖 provider 默认值）
    llm_base_url: str = ""

    # ── 上下文 ──
    max_context_tokens: int = 8000
    # 堆栈截断：只保留最近 N 帧
    max_stack_frames: int = 20
    # 局部变量截断：每个 frame 最多展示 N 个变量
    max_locals_per_frame: int = 8

    # ── 代码定位（FR11）──
    # 报错行上下各读取多少行源码片段
    code_context_lines: int = 5
    # 远程/容器路径 → 本地路径 映射，逗号分隔，如 "/app:/Users/me/project"
    source_path_map: str = ""
    # 生成的可点击链接协议：vscode | file
    ide_scheme: str = "vscode"
    # 允许生成 file:// 链接的路径白名单前缀（逗号分隔），为空=不限制
    whitelist_path_prefix: str = ""

    # ── 存储 ──
    storage_backend: str = "memory"  # "memory" | "postgresql"
    # 内存存储容量上限（按 request_id 条数计），超限时按最旧条目 FIFO 淘汰，防 OOM
    memory_store_max_entries: int = 10000
    # PG 不可达时是否自动降级到 memory 存储（生产建议 True，保证服务可用性）
    # False = PG 不可达时启动直接失败（fail-fast，适用于强一致性场景）
    storage_fallback_to_memory: bool = True

    # ── 状态后端（限流/指标计数）──
    state_backend: str = "memory"  # "memory" | "redis"
    redis_url: str = "redis://localhost:6379/0"

    # ── PostgreSQL ──
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_database: str = "ai_debug_mcp"
    pg_user: str = "postgres"
    pg_password: str = ""
    pg_min_connections: int = 2
    pg_max_connections: int = 20

    # ── PG 异步化（Phase 3.1）──
    # feature flag：开启后使用 asyncpg 异步存储实现（与 psycopg2 同步实现并存）
    # 默认关闭，保持 psycopg2 同步行为；开启前需安装 asyncpg（见 requirements.txt）
    pg_async_enabled: bool = False
    # asyncpg 连接池容量（独立于 psycopg2 的 pg_min/max_connections）
    pg_async_min: int = 2
    pg_async_max: int = 20

    # ── 过期清理 ──
    trace_ttl_seconds: int = 3600
    session_ttl_seconds: int = 3600

    # ── 安全 ──
    api_key: Optional[str] = None  # 不设置 = 不鉴权
    cors_origins: str = ""  # 空串=不下发 CORS 头（默认收紧）；"*"=显式开放所有来源（opt-in）
    rate_limit_per_minute: int = 60
    # 请求体最大字节数（防御超大请求体 OOM / DoS）
    max_body_size: int = 1_048_576
    # 诊断端点开关（/api/debug/echo, /api/debug/token），生产环境保持关闭
    debug_endpoints_enabled: bool = False
    # SEC-08: /metrics 端点独立鉴权开关
    # False（默认）= 不额外鉴权，依赖全局 AuthMiddleware
    # True = 在 /metrics 端点层独立校验 API Key（Bearer/X-API-Key），与全局中间件解耦
    metrics_auth_enabled: bool = False

    # ── 脱敏（redaction）──
    # 默认开启：数据入库前统一掩码敏感字段（fail-safe，宁可多掩不泄露）
    # ⚠️ 生产环境必须保持 True！设为 False 将关闭所有脱敏，敏感数据（密码、token 等）会明文存储和传输
    redaction_enabled: bool = True
    # 额外脱敏正则，每行一条；无效正则静默跳过，不阻断主流程
    redaction_extra_patterns: str = ""
    # 脱敏白名单字段名（逗号分隔），命中白名单的键名即使含敏感子串也不脱敏。
    # 用于避免 password_hash / public_key / key_count 等正常字段被子串匹配误伤。
    # 内置默认白名单已覆盖常见安全字段，此处可追加自定义白名单。
    redaction_key_allowlist: str = ""

    # ── Git 归因（M5）──
    # git 命令超时秒数，超时返回 None，不阻断主流程
    git_timeout: int = 10
    # 允许执行 git 操作的路径白名单前缀（逗号分隔绝对路径）；为空=不限制（生产建议限定项目根）
    git_path_whitelist: str = ""

    # ── inbound 网络采集（M6）──
    # 默认关闭：开启后记录每个进入服务的请求为 network 记录（跳过 /health /metrics）
    network_capture_enabled: bool = False

    # ── MCP 工具调用超时（SEC-05）──
    # 单次工具 handler 执行上限（秒），超时返回 isError+_timed_out，防单工具卡死阻塞会话。
    # 注意：Playwright 类工具（auto_test/verify_ui）耗时较长，需要时调大。
    tool_timeout_seconds: int = 60

    # ── 前端验证 URL 安全（SEC-02，防 SSRF）──
    # 默认拒绝回环/私网/链路本地(云元数据 169.254.x)/保留地址，仅允许公网 http(s)。
    # 本地联调需显式放开。
    ui_url_allow_private: bool = False
    # 额外允许的主机白名单（逗号分隔，如 "localhost,127.0.0.1,test.internal"）；命中即放行。
    ui_url_allowlist: str = ""

    # ── 日志 ──
    log_level: str = "INFO"
    log_format: str = "json"  # "json" | "text"

    # ── 服务 ──
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    service_name: str = "ai-debug-mcp"

    def model_post_init(self, __context: object) -> None:
        from dotenv import dotenv_values

        dotenv_values_map = dotenv_values(str(_PROJECT_ROOT / ".env"))
        # 字段名统一小写后做差集，避免 .env 大写键名与 model_fields 小写键名不匹配
        known_lower = {k.lower() for k in self.model_fields.keys()}
        extra_keys = {k for k in dotenv_values_map.keys() if k.lower() not in known_lower}
        if extra_keys:
            logger.warning("Ignored extra .env keys: %s", sorted(extra_keys))

        # M7: 空串/纯空白 API_KEY 视为未配置，归一化为 None，避免"开而无锁"
        if self.api_key is not None and not self.api_key.strip():
            logger.warning("API_KEY 为空，已视为未配置，鉴权关闭")
            self.api_key = None


# 全局单例
settings = Settings()
