"""存储工厂 —— 根据配置自动选择后端"""

import logging
import threading

from app.config import settings
from app.runtime.core.storage.base import TraceStorage, SessionStorage, ErrorStorage, SpecStorage, KnowledgeBaseStorage

logger = logging.getLogger(__name__)

# 合法后端白名单（大小写敏感）。
# WP3（Step 3 Breaking #1）：postgresql 已从运行时移除，白名单收窄为仅 memory。
# WP5（Step 3 Breaking #3）：一次性迁移入口与全部 PG 实现模块已同批删除；
# 此后 factory 只分发 memory（KB 经验由 SQLite 笔记本持久化）。
_VALID_BACKENDS = {"memory"}

_store_lock = threading.Lock()
_trace_store: TraceStorage = None   # type: ignore
_session_store: SessionStorage = None  # type: ignore
_error_store: ErrorStorage = None  # type: ignore
_spec_store: SpecStorage = None  # type: ignore
_knowledge_store: KnowledgeBaseStorage = None  # type: ignore


class StorageBackendRemovedError(RuntimeError):
    """STORAGE_BACKEND 指向一个已被正式移除的后端。

    必须是 ``RuntimeError`` 而非 ``ValueError``（设计文档 §5.2 / 决策 4）：
    ``app/api/ingest.py`` 与 ``app/api/debug.py`` 都有 ``except ValueError`` 分支，
    用 ``ValueError`` 表达「后端已移除」会让**服务端** misconfiguration 被这两个
    分支抢先截走、上报成调用方的 422 "Invalid request payload"，把责任推给客户端。
    ``RuntimeError`` 落到 ``except Exception`` / 全局 500 兜底，责任归属正确。
    同一判例见 ``app/api/ingest.py`` 的 ``_DecompressedSizeExceeded``（R7-A2）。
    """


# 精确匹配 "postgresql" 才走移除语义；大小写变体属「非法配置」，仍走通用 ValueError
# （设计文档 §5.3 匹配纪律，保证既有 case-sensitive 断言逐字存活）。
_REMOVED_BACKEND_GUIDANCE = (
    "STORAGE_BACKEND=postgresql 已被拒绝：PostgreSQL 运行时后端已正式移除（Step 3），"
    "不会静默回退到 memory。请改用默认 STORAGE_BACKEND=memory —— 运行现场存于进程内存，"
    "知识库经验由本地 SQLite 笔记本持久化（KB_PERSIST_ENABLED / KB_PERSIST_PATH）。"
    "旧版 v0.8.x 曾提供 PG kb_entries 一次性迁移脚本"
    "（scripts/migrate_pg_kb_to_sqlite.py，支持 --dry-run 核对）；当前版本不附带该脚本，"
    "尚未迁移的旧 PG 用户请先在 v0.8.x 完成迁移再升级"
    "（traces/errors/sessions/specs 无迁移路径）。"
    "遗留的 PG_* 环境变量不会重新启用 PostgreSQL，只会被忽略。"
)

_REMOVED_BACKEND_HINT = (
    " 注意：PostgreSQL 后端已正式移除，精确值 postgresql 会被直接拒绝；"
    "运行现场请使用默认 memory，知识库经验由本地 SQLite 笔记本持久化"
    "（旧版 v0.8.x 附带一次性迁移脚本，当前版本不附带）。"
)


def _validate_backend() -> None:
    """校验 storage_backend 配置值，非法值 fail-fast。

    防止拼写错误（如 "postgrsql"）静默回退到 memory，导致生产环境
    误以为用了 PG 实际用了内存，重启即丢数据。

    WP3 起分两档拒绝：
    - 精确等于 ``"postgresql"`` → :class:`StorageBackendRemovedError`，附迁移指引；
    - 其余非法值（大小写变体、拼写错误、空串）→ 通用 ``ValueError``，保持既有
      消息结构（非法值 ``!r`` / 有效值列表 / case-sensitive 提示）并追加移除提示。

    拒绝点刻意留在本函数（factory 使用点）而不是 ``Settings`` 构造阶段：构造阶段
    抛出会让 ``.env`` 仍写着 postgresql 的既有部署在**导入期**崩溃，pytest 连
    collection 都过不去，宿主也拿不到可读的启动失败原因。
    """
    backend = settings.storage_backend
    if backend == "postgresql":
        raise StorageBackendRemovedError(_REMOVED_BACKEND_GUIDANCE)
    if backend not in _VALID_BACKENDS:
        raise ValueError(
            f"Invalid STORAGE_BACKEND={backend!r}. "
            f"Valid values: {sorted(_VALID_BACKENDS)}. "
            f"Check .env or environment variable spelling (case-sensitive)."
            f"{_REMOVED_BACKEND_HINT}"
        )


def get_trace_store() -> TraceStorage:
    global _trace_store
    if _trace_store is None:
        with _store_lock:
            if _trace_store is None:
                _validate_backend()
                from app.runtime.core.storage.memory_store import MemoryTraceStore
                _trace_store = MemoryTraceStore(max_entries=settings.memory_store_max_entries)
                logger.info("trace_store initialized: backend=%s", settings.storage_backend)
    return _trace_store


def get_session_store() -> SessionStorage:
    global _session_store
    if _session_store is None:
        with _store_lock:
            if _session_store is None:
                _validate_backend()
                from app.runtime.core.storage.memory_store import MemorySessionStore
                _session_store = MemorySessionStore()
                logger.info("session_store initialized: backend=%s", settings.storage_backend)
    return _session_store


def get_error_store() -> ErrorStorage:
    """返回错误存储实例（memory 后端 no-op：运行现场重启即清）。"""
    global _error_store
    if _error_store is None:
        with _store_lock:
            if _error_store is None:
                _validate_backend()
                from app.runtime.core.storage.noop_store import NoOpErrorStore
                _error_store = NoOpErrorStore()
                logger.info("error_store initialized: backend=%s (no-op)", settings.storage_backend)
    return _error_store


def get_spec_store() -> SpecStorage:
    """返回规范存储实例（memory 后端 no-op：运行现场重启即清）。"""
    global _spec_store
    if _spec_store is None:
        with _store_lock:
            if _spec_store is None:
                _validate_backend()
                from app.runtime.core.storage.noop_store import NoOpSpecStore
                _spec_store = NoOpSpecStore()
                logger.info("spec_store initialized: backend=%s (no-op)", settings.storage_backend)
    return _spec_store


def get_knowledge_store() -> KnowledgeBaseStorage:
    """返回知识库持久化实例。

    - 默认 memory：v0.8.0「笔记本」——`KB_PERSIST_ENABLED=true`
      （默认）时写穿到本地 SQLite 单文件，跨重启保留自有经验；
      显式关闭时退回历史 no-op 行为。

    KB 主存仍是进程内 KnowledgeBaseStore；本实例承担写穿持久化
    （upsert/record_verification/驱逐/clear 同步落库）与启动回灌
    （list_recent_kb_entries）。初始化失败时降级 no-op（KB 退回纯内存行为，
    不阻断启动）。
    """
    global _knowledge_store
    if _knowledge_store is None:
        with _store_lock:
            if _knowledge_store is None:
                _validate_backend()
                # v0.8.0「笔记本」：memory 后端下 KB 经验写穿到本地 SQLite 单文件
                # （零安装、单文件、跨重启保留）；显式关闭时退回历史 no-op 行为。
                if settings.kb_persist_enabled:
                    try:
                        from app.runtime.core.storage.sqlite_kb_store import (
                            SQLiteKnowledgeBaseStore,
                        )

                        _knowledge_store = SQLiteKnowledgeBaseStore()
                        logger.info(
                            "knowledge_store initialized: backend=sqlite "
                            "(local notebook, path=%s)",
                            settings.kb_persist_path,
                        )
                    except Exception as e:
                        logger.warning("SQLite KB store 初始化失败，降级 no-op: %s", e)
                        from app.runtime.core.storage.noop_store import (
                            NoOpKnowledgeBaseStore,
                        )

                        _knowledge_store = NoOpKnowledgeBaseStore()
                else:
                    from app.runtime.core.storage.noop_store import NoOpKnowledgeBaseStore

                    _knowledge_store = NoOpKnowledgeBaseStore()
                    logger.info(
                        "knowledge_store initialized: backend=%s (no-op)",
                        settings.storage_backend,
                    )
    return _knowledge_store
