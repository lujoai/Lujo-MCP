"""存储工厂 —— 根据配置自动选择后端"""

import logging
import shutil
import threading
import time
from pathlib import Path

from app.config import settings
from app.runtime.core.storage.base import TraceStorage, SessionStorage, ErrorStorage, SpecStorage, KnowledgeBaseStorage

logger = logging.getLogger(__name__)

# 合法后端白名单（大小写敏感）。
# WP3（Step 3 Breaking #1）：postgresql 已从运行时移除，白名单收窄为仅 memory。
# 迁移专用白名单 _MIGRATION_SOURCE_BACKENDS 与本集合物理分离（§4.2 / §4.3-G3）：
# 收窄不削弱一次性 PG → SQLite 迁移能力，该能力保留到 WP5 与 PG 驱动同批到期。
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
    "STORAGE_BACKEND=postgresql 已被拒绝：PostgreSQL 运行时后端已在 Step 3 正式移除，"
    "不会静默回退到 memory。请改用默认 STORAGE_BACKEND=memory —— 运行现场存于进程内存，"
    "知识库经验由本地 SQLite 笔记本持久化（KB_PERSIST_ENABLED / KB_PERSIST_PATH）。"
    "已有 PostgreSQL kb_entries 数据请先执行一次性迁移脚本 "
    "scripts/migrate_pg_kb_to_sqlite.py（建议先 --dry-run 核对 report 再正式执行）。"
)

_REMOVED_BACKEND_HINT = (
    " 注意：PostgreSQL 后端已在 Step 3 正式移除，精确值 postgresql 会被直接拒绝；"
    "运行现场请使用默认 memory，知识库经验由本地 SQLite 笔记本持久化"
    "（一次性迁移脚本 scripts/migrate_pg_kb_to_sqlite.py）。"
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


class _AsyncMixError(RuntimeError):
    """pg_async_enabled=True 时同步 getter 的配置错误（async-mix）。"""


def _raise_async_mix(feature: str) -> None:
    """FIX: P1-4 pg_async_enabled=True 时同步 getter fail-fast。

    asyncpg store 的方法均为 async，同步调用（不带 await）不会执行函数体、
    只会返回 coroutine 对象导致数据静默丢失。开启 pg_async_enabled 后，
    同步存储用户必须走 async 版本，否则启动/首次调用即抛配置错误，
    杜绝"部分丢部分写"的混合行为。

    FIX: R7-V4 —— 专用异常类型：此前抛裸 RuntimeError 被 ``except Exception``
    fallback 分支吞掉，pg_async_enabled=True + storage_fallback_to_memory=True
    （默认）时静默降级 memory（重启即丢），与"启动即抛配置错误"的 docstring
    自相矛盾。_AsyncMixError 在各 getter 中先行 re-raise，不走降级。
    """
    raise _AsyncMixError(
        f"{feature}: pg_async_enabled=True 要求全链路 async 调用，"
        f"同步 getter 不可用（防止 coroutine 未 await 导致数据静默丢失）。"
        f"请把调用链迁移到 async 版本，或设置 PG_ASYNC_ENABLED=false 保持同步行为。"
    )


def get_trace_store() -> TraceStorage:
    global _trace_store
    if _trace_store is None:
        with _store_lock:
            if _trace_store is None:
                _validate_backend()
                if settings.storage_backend == "postgresql":
                    try:
                        # Phase 3.1：feature flag 开启时走 asyncpg 异步实现（与 psycopg2 同步并存）
                        if settings.pg_async_enabled:
                            # FIX: P1-4 同步 getter 遇到 async 后端 fail-fast（fallback=True 时由 except 分支降级）
                            _raise_async_mix("trace_store")
                        else:
                            from app.runtime.core.storage.pg_trace_store import PGTraceStore
                            _trace_store = PGTraceStore()
                            logger.info(
                                "trace_store initialized: backend=%s, async=disabled (psycopg2 sync)",
                                settings.storage_backend,
                            )
                    except _AsyncMixError:
                        # FIX: R7-V4 —— async-mix 配置错误必须 fail-fast，不允许静默降级 memory
                        raise
                    except Exception as e:
                        if settings.storage_fallback_to_memory:
                            logger.warning("PG trace_store 初始化失败，降级到 memory: %s", e)
                            from app.runtime.core.storage.memory_store import MemoryTraceStore
                            _trace_store = MemoryTraceStore(max_entries=settings.memory_store_max_entries)
                            logger.warning("trace_store 已降级到 memory (fallback enabled)")
                        else:
                            raise
                else:
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
                if settings.storage_backend == "postgresql":
                    try:
                        # Phase 3.1：feature flag 开启时走 asyncpg 异步实现（与 psycopg2 同步并存）
                        if settings.pg_async_enabled:
                            # FIX: P1-4 同步 getter 遇到 async 后端 fail-fast（fallback=True 时由 except 分支降级）
                            _raise_async_mix("session_store")
                        else:
                            from app.runtime.core.storage.pg_session_store import PGSessionStore
                            _session_store = PGSessionStore()
                            logger.info(
                                "session_store initialized: backend=%s, async=disabled (psycopg2 sync)",
                                settings.storage_backend,
                            )
                    except _AsyncMixError:
                        # FIX: R7-V4 —— async-mix 配置错误必须 fail-fast，不允许静默降级 memory
                        raise
                    except Exception as e:
                        if settings.storage_fallback_to_memory:
                            logger.warning("PG session_store 初始化失败，降级到 memory: %s", e)
                            from app.runtime.core.storage.memory_store import MemorySessionStore
                            _session_store = MemorySessionStore()
                            logger.warning("session_store 已降级到 memory (fallback enabled)")
                        else:
                            raise
                else:
                    from app.runtime.core.storage.memory_store import MemorySessionStore
                    _session_store = MemorySessionStore()
                    logger.info("session_store initialized: backend=%s", settings.storage_backend)
    return _session_store


def get_error_store() -> ErrorStorage:
    """返回错误存储实例（方案 C：按后端分发，PG 真实持久化，memory no-op）。"""
    global _error_store
    if _error_store is None:
        with _store_lock:
            if _error_store is None:
                _validate_backend()
                if settings.storage_backend == "postgresql":
                    try:
                        if settings.pg_async_enabled:
                            # FIX: P1-4 同步 getter 遇到 async 后端 fail-fast（fallback=True 时由 except 分支降级）
                            _raise_async_mix("error_store")
                        else:
                            from app.runtime.core.storage.pg_error_store import PGErrorStore
                            _error_store = PGErrorStore()
                            logger.info(
                                "error_store initialized: backend=%s, async=disabled (psycopg2 sync)",
                                settings.storage_backend,
                            )
                    except _AsyncMixError:
                        # FIX: R7-V4 —— async-mix 配置错误必须 fail-fast，不允许静默降级 memory
                        raise
                    except Exception as e:
                        if settings.storage_fallback_to_memory:
                            logger.warning("PG error_store 初始化失败，降级到 no-op: %s", e)
                            from app.runtime.core.storage.noop_store import NoOpErrorStore
                            _error_store = NoOpErrorStore()
                            logger.warning("error_store 已降级到 no-op (fallback enabled)")
                        else:
                            raise
                else:
                    from app.runtime.core.storage.noop_store import NoOpErrorStore
                    _error_store = NoOpErrorStore()
                    logger.info("error_store initialized: backend=%s (no-op)", settings.storage_backend)
    return _error_store


def get_spec_store() -> SpecStorage:
    """返回规范存储实例（方案 C：按后端分发，PG 真实持久化，memory no-op）。"""
    global _spec_store
    if _spec_store is None:
        with _store_lock:
            if _spec_store is None:
                _validate_backend()
                if settings.storage_backend == "postgresql":
                    try:
                        if settings.pg_async_enabled:
                            # FIX: P1-4 同步 getter 遇到 async 后端 fail-fast（fallback=True 时由 except 分支降级）
                            _raise_async_mix("spec_store")
                        else:
                            from app.runtime.core.storage.pg_spec_store import PGSpecStore
                            _spec_store = PGSpecStore()
                            logger.info(
                                "spec_store initialized: backend=%s, async=disabled (psycopg2 sync)",
                                settings.storage_backend,
                            )
                    except _AsyncMixError:
                        # FIX: R7-V4 —— async-mix 配置错误必须 fail-fast，不允许静默降级 memory
                        raise
                    except Exception as e:
                        if settings.storage_fallback_to_memory:
                            logger.warning("PG spec_store 初始化失败，降级到 no-op: %s", e)
                            from app.runtime.core.storage.noop_store import NoOpSpecStore
                            _spec_store = NoOpSpecStore()
                            logger.warning("spec_store 已降级到 no-op (fallback enabled)")
                        else:
                            raise
                else:
                    from app.runtime.core.storage.noop_store import NoOpSpecStore
                    _spec_store = NoOpSpecStore()
                    logger.info("spec_store initialized: backend=%s (no-op)", settings.storage_backend)
    return _spec_store


def get_knowledge_store() -> KnowledgeBaseStorage:
    """返回知识库持久化实例。

    - `STORAGE_BACKEND=postgresql`：PG 真实持久化（v0.5.3 行为，一行不变）；
    - 其余（默认 memory）：v0.8.0「笔记本」——`KB_PERSIST_ENABLED=true`
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
                if settings.storage_backend == "postgresql":
                    try:
                        if settings.pg_async_enabled:
                            _raise_async_mix("knowledge_store")
                        else:
                            from app.runtime.core.storage.pg_kb_store import PGKnowledgeBaseStore
                            _knowledge_store = PGKnowledgeBaseStore()
                            logger.info(
                                "knowledge_store initialized: backend=%s, async=disabled (psycopg2 sync)",
                                settings.storage_backend,
                            )
                    except _AsyncMixError:
                        # FIX: R7-V4 —— async-mix 配置错误必须 fail-fast，不允许静默降级 memory
                        raise
                    except Exception as e:
                        if settings.storage_fallback_to_memory:
                            logger.warning("PG knowledge_store 初始化失败，降级到 no-op: %s", e)
                            from app.runtime.core.storage.noop_store import NoOpKnowledgeBaseStore
                            _knowledge_store = NoOpKnowledgeBaseStore()
                            logger.warning("knowledge_store 已降级到 no-op (fallback enabled)")
                        else:
                            raise
                else:
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


# ── 一次性迁移入口（Step 3 WP2 交付；WP5 与 PG 驱动同批删除，不留到 WP6/WP7） ──

# 迁移源白名单：与运行时 _VALID_BACKENDS 物理分离（设计文档 §4.2 / §4.3-G3）。
# 只被 migrate_knowledge_entries() 消费，不影响任何运行时 getter；
# WP3 收窄 _VALID_BACKENDS 不改变本集合。
_MIGRATION_SOURCE_BACKENDS = {"postgresql"}

_MIGRATION_CONFLICT_POLICIES = {"skip", "upsert"}
_MIGRATION_DEFAULT_LIMIT = 1_000_000


def _migration_existing_fingerprints(store) -> set:
    """经存储边界读取目标库既有 fingerprint 集合（on_conflict=skip 判定用，不新增 SQL 面）。"""
    return {
        e["fingerprint"]
        for e in store.list_recent_kb_entries(limit=_MIGRATION_DEFAULT_LIMIT)
        if e.get("fingerprint")
    }


def _migration_backup_path(target_path: str) -> str:
    """生成带时间戳、不与既有文件冲突的备份路径。"""
    base = f"{target_path}.backup-{time.strftime('%Y%m%dT%H%M%S')}"
    candidate = base
    n = 1
    while Path(candidate).exists():
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def migrate_knowledge_entries(
    *,
    source_backend: str,
    target_path: str,
    limit: int = _MIGRATION_DEFAULT_LIMIT,
    on_conflict: str = "skip",
    dry_run: bool = False,
    fail_fast: bool = False,
    backup: bool = True,
) -> dict:
    """一次性迁移：PostgreSQL kb_entries → 本地 SQLite「笔记本」（仅 KB，别无他表）。

    Step 3 WP2 交付（对应 DEV_PLAN S2-3，兑现 docs/public/TROUBLESHOOTING.md 的公开承诺）。
    **硬到期 WP5**：本函数与 `_MIGRATION_SOURCE_BACKENDS` 将随 PG 驱动 / PG store /
    PG 测试同批删除（设计文档 §4.3-G1），不得成为长期运行时后端；
    `app/` 下不允许出现任何调用方（运行时业务代码必须走 `get_knowledge_store()`）。

    与运行时链路的关系：
    - `source_backend` 只接受 `_MIGRATION_SOURCE_BACKENDS`（独立白名单，G3），
      与 `STORAGE_BACKEND` / `_VALID_BACKENDS` / `_validate_backend()` 完全无关；
    - 不读 `settings.storage_backend`、不复用运行时 store 单例（临时构造、即用即弃），
      `get_*_store()` 返回的实例与迁移互不可见。

    边界（设计文档 §4.4，逐条对应）：
    - B1 源读取只经 `PGKnowledgeBaseStore.list_recent_kb_entries(limit)`，limit 为全量大值，
      不复用启动回灌的 max_entries 截断；
    - B2 目标写入只经 `SQLiteKnowledgeBaseStore.upsert_kb_entry(entry)`，
      不触发 seed 加载 / LRU 驱逐 / 向量同步；
    - B3 JSONB→dict 已由 PG 读边界完成、dict→TEXT 由 SQLite 写边界完成，
      本函数只搬运 dict，不复制任何序列化逻辑；
    - B5 重复执行幂等（upsert 语义 / skip 命中既有指纹即跳过）；
    - B6 `on_conflict="skip"`（默认）保留目标既有经验，禁止时间戳规则覆盖；
      `"upsert"` 须调用方显式选择；
    - B7 空表 → `read=0, status="ok"`，不视为失败；
    - B8 `fail_fast=True` 首错即停并保留已完成写入；`False` 逐行隔离错误计入
      `report["errors"]`，行级失败经 report 暴露而非抛异常；
    - B9 `backup=True`（默认）且目标文件已存在时先做带时间戳备份；
      **目标已存在 + backup=False 直接拒绝**（不允许无备份写既有目标库）；
    - B10 迁移全程不写源库；失败不破坏目标库；report 不含任何凭据。

    返回 report dict（G2：绝不返回 store 实例）。关键键：
    `status`（"ok" | "failed"）、`read` / `migrated` / `skipped_existing` / `errors`、
    `dry_run` / `backup_path` / `limit` / `on_conflict`。
    dry_run 时 `migrated` / `skipped_existing` 表示「将要发生」的计数，且
    不创建目标文件、不做备份、不 checkpoint；目标已存在时仅经存储边界读取
    既有指纹用于冲突统计，不改任何行。
    """
    if source_backend not in _MIGRATION_SOURCE_BACKENDS:
        raise ValueError(
            f"Invalid migration source_backend={source_backend!r}. "
            f"Valid values: {sorted(_MIGRATION_SOURCE_BACKENDS)} (case-sensitive). "
            f"注意：这是迁移专用白名单，与运行时 STORAGE_BACKEND 白名单无关。"
        )
    if on_conflict not in _MIGRATION_CONFLICT_POLICIES:
        raise ValueError(
            f"Invalid on_conflict={on_conflict!r}. "
            f"Valid values: {sorted(_MIGRATION_CONFLICT_POLICIES)}."
        )
    if not isinstance(target_path, str) or not target_path.strip():
        raise ValueError(
            f"Invalid target_path={target_path!r}: 迁移目标必须是 SQLite 文件路径。"
        )
    if target_path.strip() == ":memory:":
        raise ValueError(
            "target_path 不支持 ':memory:'：SQLiteKnowledgeBaseStore 使用短连接，"
            "每连接独立的内存库无法承载持久化。请改用文件路径。"
        )
    if limit <= 0:
        raise ValueError(f"Invalid limit={limit!r}: 必须为正整数。")

    target = str(Path(target_path).expanduser().resolve())
    target_exists = Path(target).exists()
    if target_exists and not dry_run and not backup:
        raise ValueError(
            f"目标文件已存在且 backup=False，拒绝执行：{target}。"
            f"已有目标库不允许无备份写入；请保持 backup=True（默认）或先手动备份。"
        )

    report: dict = {
        "status": "ok",
        "source_backend": source_backend,
        "target_path": target,
        "dry_run": dry_run,
        "limit": limit,
        "on_conflict": on_conflict,
        "fail_fast": fail_fast,
        "backup": backup,
        "backup_path": None,
        "read": 0,
        "migrated": 0,
        "skipped_existing": 0,
        "errors": [],
    }

    # 源侧：经 factory 内部构造临时 PG KB store（不触碰 _knowledge_store 单例）。
    # 连接生命周期由 pg_executor 接管；读边界已完成 JSONB→dict 与 NULL 兜底。
    from app.runtime.core.storage.pg_kb_store import PGKnowledgeBaseStore

    source = PGKnowledgeBaseStore()
    entries = source.list_recent_kb_entries(limit=limit)
    report["read"] = len(entries)
    if not entries:
        # B7：空表直接返回成功，不创建目标文件、不做备份。
        return report

    # 目标侧：dry_run 且目标不存在时完全不构造（避免 dry_run 建库落盘）；
    # dry_run 且目标已存在时仅构造用于读取既有指纹（无写入、无备份、无 checkpoint）。
    from app.runtime.core.storage.sqlite_kb_store import SQLiteKnowledgeBaseStore

    existing: set = set()
    target_store = None
    if target_exists or not dry_run:
        target_store = SQLiteKnowledgeBaseStore(db_path=target)
        existing = _migration_existing_fingerprints(target_store)
        if not dry_run and target_exists and backup:
            # B9：写入前先收口 WAL 再复制，保证备份文件内容完整（含迁移前全部数据）。
            target_store.checkpoint()
            backup_path = _migration_backup_path(target)
            shutil.copy2(target, backup_path)
            report["backup_path"] = backup_path

    for index, entry in enumerate(entries):
        fingerprint = entry.get("fingerprint") if isinstance(entry, dict) else None
        if not fingerprint:
            report["errors"].append(
                {"index": index, "fingerprint": None, "error": "缺少 fingerprint，无法迁移"}
            )
            if fail_fast:
                break
            continue
        if on_conflict == "skip" and fingerprint in existing:
            report["skipped_existing"] += 1
            continue
        if dry_run:
            report["migrated"] += 1
            continue
        try:
            target_store.upsert_kb_entry(entry)
            report["migrated"] += 1
            existing.add(fingerprint)
        except Exception as exc:
            report["errors"].append(
                {
                    "index": index,
                    "fingerprint": fingerprint,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            if fail_fast:
                break

    if report["errors"]:
        report["status"] = "failed"
    if not dry_run:
        target_store.checkpoint()
    return report
