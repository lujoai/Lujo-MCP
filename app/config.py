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

    # ── Phase 5：数据层长期优化 ──
    # P3-1：traces 表按月分区（PostgreSQL 声明式 RANGE 分区）
    # 默认关闭，启用后自动创建当月及下月分区，历史数据需手动迁移
    pg_partition_enabled: bool = False
    # 自动预创建未来 N 个月的分区（默认 2，保证跨月不中断）
    pg_partition_precreate_months: int = 2

    # P3-2：归档策略（>N 天数据自动归档到 traces_archive 表）
    # 默认关闭，启用后 cleanup_expired 先归档再删除
    pg_archive_enabled: bool = False
    # 归档阈值天数，超过该天数的 traces 数据自动归档（默认 30 天）
    pg_archive_days: int = 30
    # 归档后是否从主表删除（默认 True，False=仅复制不删除，用于验证）
    pg_archive_delete_after: bool = True

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

    # ── OpenTelemetry（P3-4）──
    # 是否启用 OTel 指标导出（默认关闭）
    # 开启后使用 OTel SDK 记录指标，同时保留 /metrics Prometheus 文本端点向后兼容
    otel_enabled: bool = False
    # OTel 服务名（用于指标标签）
    otel_service_name: str = "ai-debug-mcp"
    # OTLP gRPC 导出端点（如 http://localhost:4317），为空则使用 OTEL_EXPORTER_OTLP_ENDPOINT 环境变量
    otel_exporter_endpoint: str = ""
    # OTel 采样率（0.0-1.0）
    otel_metrics_interval_ms: int = 60000

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

    # ── 熔断器（P3-8）──
    # 全局开关：开启后 LLM 和 PG 调用都受熔断器保护
    circuit_breaker_enabled: bool = False

    # LLM 熔断器配置（cb_llm_*）
    # 滑动窗口内的最大失败次数，超过则熔断
    cb_llm_max_failures: int = 5
    # 熔断后半开状态等待时间（秒），期间允许一次试探调用
    cb_llm_reset_timeout: int = 30
    # 滑动窗口大小（秒），超过窗口的失败不计入统计
    cb_llm_window_size: int = 60

    # PG 熔断器配置（cb_pg_*）
    cb_pg_max_failures: int = 3
    cb_pg_reset_timeout: int = 15
    cb_pg_window_size: int = 60

    # ── P3-6 异步分析队列（消息队列削峰）──
    # 全局开关：开启后 /api/debug/analyze/async 走有界队列 + K 常驻消费协程，对齐 LLM RPM/TPM
    # 裸 BackgroundTasks 无削峰语义；真正削峰靠有界 asyncio.Queue(maxsize=N) + Semaphore(K)
    llm_async_analysis_enabled: bool = False
    # N：队列峰容量，满则背压（返回 429 + 排队号），不得无限堆积
    llm_queue_maxsize: int = 100
    # K：常驻消费协程数，对齐 LLM RPM（并发上限旋钮）
    llm_queue_workers: int = 4
    # 优雅停机排空超时（秒），超时后记录未完成 job 并退出
    llm_queue_drain_timeout: int = 30

    # ── P3-7 L3 缓存预热 ──
    # 全局开关：开启后服务启动时从 L2 Redis 扫描热门 fingerprint 回填 L1，避免冷启动 miss 洪峰
    # 只写 L1 不写 L2（保护 L2 TTL 淘汰语义）；analyzer.py 缓存区零改动，仅通过 _set_l1_only 写入
    llm_cache_prewarm_enabled: bool = False
    # 预热条数上限（受 L1 _MAX_CACHE_SIZE=100 约束，超出会 cap + warning）
    llm_cache_prewarm_top_n: int = 20
    # 定时预热间隔（秒）；0=仅启动时预热一次，不创建定时任务
    llm_cache_prewarm_interval_seconds: int = 3600

    # ── 向量检索 RAG（Phase 7）──
    # 全局开关：开启后 LLM 分析前先做向量召回（精确指纹 miss 后的二级 fallback）
    # 抽象落在检索语义 add(docs)/search(query, top_k)，禁止 Qdrant collection/point 概念 leak 进接口
    vector_store_enabled: bool = False
    # 向量库后端：in_process（默认，零外部依赖）| qdrant（接入 OpenAI/智谱 Embeddings，语义召回）
    vector_store_backend: str = "in_process"
    # 召回 top_k 数量
    vector_store_top_k: int = 3
    # 召回相似度阈值，低于该分数不返回
    vector_store_min_score: float = 0.3
    # Qdrant 配置（backend=qdrant 时生效；依赖未装或连接失败时静默降级为 add=no-op / search=空）
    qdrant_url: str = ""
    qdrant_collection: str = "ai-debug-kb"
    qdrant_api_key: str = ""
    # Embedding 模型：OpenAI 用 text-embedding-3-small（1536维）；智谱用 embedding-3（1024维）
    # 与 llm_provider 解耦而非自动推导——用户可能 LLM 与 embedding 用不同 provider
    qdrant_embedding_model: str = "text-embedding-3-small"
    # 向量维度：必须与 qdrant_embedding_model 对齐，且与已建 collection 维度一致
    # 维度不匹配时适配器不自动重建 collection（避免丢数据），改为 warning + 降级
    qdrant_embedding_dim: int = 1536
    # Qdrant 建连超时（秒），参照 Redis socket_timeout=2 的快速失败风格
    qdrant_connect_timeout: int = 5
    # Qdrant upsert/query 单次请求超时（秒）；embedding 调用走 llm_timeout
    qdrant_request_timeout: int = 10

    # ── RBAC + API_KEY 轮换（AUDIT-2-13/14）──
    # 多 key 轮换：逗号分隔的有效 key 列表（新/旧 key 重叠期共存）；空时回退单 api_key
    api_keys: str = ""
    api_key_rotation_enabled: bool = False
    # RBAC 角色分级开关
    rbac_enabled: bool = False
    # key→role 映射，逗号分隔，如 "key1:admin,key2:viewer"；未配置时默认 admin（向后兼容）
    rbac_role_mapping: str = ""

    # ── AI Debug Agent（Phase 1：自动修复 + 多 Agent 协同）──
    # 全局开关：开启后 POST /api/debug/repair/async 走有界队列 + K 常驻消费协程
    # 静默降级：Agent 失败不影响主链路（与 Qdrant 适配器一致）
    agent_enabled: bool = False
    # N：队列峰容量，满则背压（返回 429）
    agent_queue_maxsize: int = 50
    # K：常驻消费协程数（与 llm_queue_workers 解耦，避免抢 LLM RPM 配额）
    agent_queue_workers: int = 2
    # 优雅停机排空超时（秒）
    agent_queue_drain_timeout: int = 60
    # 是否启用上下文装配的 prior_analysis（复用 analyzer.analyze_async）
    # 关闭时 RepairAgent 仅基于原始 debug_context 生成方案，节省 LLM 调用
    agent_prior_analysis_enabled: bool = True
    # RepairAgent 用的 LLM 模型（与 llm_model 解耦，可指定更强模型）；空串=回退 llm_model
    agent_model: str = ""
    # RepairAgent 重试次数（独立于 llm_max_retries）
    agent_max_retries: int = 2
    # RepairAgent 单次执行总超时（秒，含 LLM 调用 + 重试）
    agent_timeout: int = 90
    # Phase 2 预留：多 Agent 协同模式开关（Phase 1 不生效，仅占位）
    # 启用后 Coordinator 按 plan 调度 GitAgent / TestAgent / SecurityAgent
    agent_multi_agent_enabled: bool = False

    # ── 服务 ──
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    service_name: str = "ai-debug-mcp"

    def model_post_init(self, __context: object) -> None:
        from dotenv import dotenv_values

        dotenv_values_map = dotenv_values(str(_PROJECT_ROOT / ".env"))
        # 字段名统一小写后做差集，避免 .env 大写键名与 model_fields 小写键名不匹配
        known_lower = {k.lower() for k in type(self).model_fields.keys()}
        extra_keys = {k for k in dotenv_values_map.keys() if k.lower() not in known_lower}
        if extra_keys:
            logger.warning("Ignored extra .env keys: %s", sorted(extra_keys))

        # M7: 空串/纯空白 API_KEY 视为未配置，归一化为 None，避免"开而无锁"
        if self.api_key is not None and not self.api_key.strip():
            logger.warning("API_KEY 为空，已视为未配置，鉴权关闭")
            self.api_key = None


# 全局单例
settings = Settings()
