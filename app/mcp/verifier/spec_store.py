"""
规范存储 —— 期望规范的 CRUD。

复用 TraceStorage 抽象（add_log / get_logs / delete_logs，step="spec"）做持久化备份，
同时用模块级 dict 做主存以支持 list / update / delete。

C4 对标：重启后通过 _restore_from_storage() 从 trace_store 恢复 spec 到内存缓存，
确保 list_specs() / Dashboard 在进程重启后仍可正常工作。

Spec = { id, kind: "api"|"ui"|"rule", target, expect: {status?, body_rules?, state_change?} }
"""
import json
import time
import uuid
import threading
import logging

from app.mcp.core.logs import add_log, get_logs, delete_logs

logger = logging.getLogger("ai-debug-mcp.spec_store")
_STEP_SPEC = "spec"

# 主存：spec_id → spec dict
_specs: dict[str, dict] = {}
_lock = threading.Lock()
_restored = False


def _pg_available() -> bool:
    """检查 PG 后端是否可用（spec_store 仅在 PG 后端时双写 specs 表）。"""
    try:
        from app.config import settings
        return settings.storage_backend == "postgresql"
    except Exception:
        return False


def _new_id() -> str:
    return "spec-" + uuid.uuid4().hex[:12]


def _spec_version_ts(data: dict, entry: dict) -> float:
    """取 spec 版本时间戳：优先 data.updated_at，回退 entry 顶层 timestamp。

    用于多版本共存时比较新旧：add_log 写入的 entry 结构为
    {"timestamp":..., "step":..., "data": <spec dict>}，spec dict 内含 updated_at。
    """
    ts = data.get("updated_at")
    if ts is None:
        ts = entry.get("timestamp")
    try:
        return float(ts) if ts is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _do_restore() -> list[dict]:
    """从存储层恢复 spec 列表（纯 IO，不持有锁）。

    Phase 2.4：PG 后端优先从 specs 表直接查询（消除 N+1 扫描）。
    内存后端回退到 trace_store 扫描（向后兼容）。

    返回从存储层读取到的 spec 列表，供调用方在持锁状态下合并。

    SEC-13：update 走 append-only，同一 spec_id 可能存在多个历史版本，
    trace_store 回退路径取 updated_at 最大者。
    """
    # Phase 2.4：PG 后端直接查 specs 表（消除 N+1）
    if _pg_available():
        try:
            from app.mcp.core.storage.pg_store import list_specs_pg
            return list_specs_pg()
        except Exception:
            logger.debug("specs 表查询失败，回退 trace_store 扫描", exc_info=True)

    # 内存后端回退：扫描 trace_store（legacy N+1 路径）
    latest: dict[str, tuple[dict, float]] = {}
    try:
        from app.mcp.core.logs import list_request_ids as _list_rids
        request_ids = _list_rids(limit=500)
        for rid in request_ids:
            logs = get_logs(rid)
            for log in logs:
                if log.get("step") != _STEP_SPEC:
                    continue
                data = log.get("data")
                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except (json.JSONDecodeError, TypeError):
                        continue
                if not isinstance(data, dict):
                    continue
                spec_id = data.get("id")
                if not spec_id:
                    continue
                ts = _spec_version_ts(data, log)
                prev = latest.get(spec_id)
                if prev is None or ts > prev[1]:
                    latest[spec_id] = (data, ts)
    except Exception:
        pass
    return [v[0] for v in latest.values()]


def _restore_if_needed() -> None:
    """从 trace_store 恢复 spec 到内存缓存（C4 对标模式）。

    IO 操作在锁外执行，合并结果时才加锁，避免持锁做 IO 阻塞其他 spec 操作。
    恢复失败静默降级，不影响正常功能。
    """
    global _restored
    with _lock:
        if _restored:
            return

    # IO 在锁外执行，不阻塞并发 spec 操作
    restored_specs = _do_restore()

    # 合并结果时加锁
    with _lock:
        if not _restored:
            for spec_data in restored_specs:
                sid = spec_data.get("id")
                if sid and sid not in _specs:
                    _specs[sid] = spec_data
            _restored = True


def create(spec: dict) -> str:
    """创建一条规范，返回 spec_id。

    spec 可选传 id（不传则自动生成）。

    Phase 2.4：双写 —— specs 表（PG 后端）+ trace_store（step="spec"）。
    """
    spec_id = spec.get("id") or _new_id()
    now = time.time()
    record = {
        "id": spec_id,
        "kind": spec.get("kind", "api"),
        "target": spec.get("target", ""),
        "expect": spec.get("expect") or {},
        "created_at": spec.get("created_at") or now,
        "updated_at": now,
    }
    with _lock:
        _specs[spec_id] = record
    # Phase 2.4：双写 —— specs 表（PG 后端优先）
    if _pg_available():
        try:
            from app.mcp.core.storage.pg_store import save_spec
            save_spec(record)
        except Exception:
            logger.debug("specs 表写入失败 (spec_id=%s)", spec_id, exc_info=True)
    # 保留 trace_store 写入（迁移期双写）
    add_log(spec_id, _STEP_SPEC, record)
    return spec_id


def get(spec_id: str) -> dict | None:
    """取一条规范。优先从内存取，fallback 从存储层恢复。

    Phase 2.4：PG 后端优先从 specs 表读取（消除 N+1）。
    """
    with _lock:
        if spec_id in _specs:
            return dict(_specs[spec_id])

    # Phase 2.4：PG 后端优先从 specs 表读取
    if _pg_available():
        try:
            from app.mcp.core.storage.pg_store import get_spec
            data = get_spec(spec_id)
            if data is not None:
                with _lock:
                    _specs[spec_id] = data
                return dict(data)
        except Exception:
            logger.debug("specs 表读取失败 (spec_id=%s)", spec_id, exc_info=True)

    # fallback：从 trace_store 恢复到内存
    _restore_if_needed()
    with _lock:
        if spec_id in _specs:
            return dict(_specs[spec_id])

    # 仍然未找到，尝试直接从存储读取（SEC-13：取最新版本）
    best_data = None
    best_ts = -1.0
    for entry in get_logs(spec_id):
        if entry.get("step") != _STEP_SPEC:
            continue
        data = entry.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                continue
        if not isinstance(data, dict):
            continue
        ts = _spec_version_ts(data, entry)
        if ts > best_ts:
            best_ts = ts
            best_data = data
    if best_data is not None:
        with _lock:
            _specs[spec_id] = best_data
        return dict(best_data)
    return None


def update(spec_id: str, patch: dict) -> dict | None:
    """部分更新规范，返回更新后的规范。不存在则返回 None。

    id 不可修改。

    SEC-13：改为 crash-safe append —— 仅追加新版本到 trace_store 作为提交点，
    不再 delete_logs 旧条目（删除仅由显式 delete() 负责）。多版本共存时读取
    路径取 updated_at 最大者。中途崩溃最多丢失“本次更新未提交”，不会丢历史版本。

    Phase 2.4：双写 —— specs 表（PG 后端）+ trace_store（step="spec"）。
    """
    with _lock:
        existing = _specs.get(spec_id)
        if not existing:
            return None
        existing.update(patch)
        existing["id"] = spec_id  # id 不可改
        existing["updated_at"] = time.time()
        updated = dict(existing)

    # Phase 2.4：双写 —— specs 表（PG 后端优先）
    if _pg_available():
        try:
            from app.mcp.core.storage.pg_store import save_spec
            save_spec(updated)
        except Exception:
            logger.debug("specs 表更新失败 (spec_id=%s)", spec_id, exc_info=True)
    # crash-safe append：写入新版本作为提交点，失败仅记录日志不影响内存层
    try:
        add_log(spec_id, _STEP_SPEC, updated)
    except Exception:
        logger.exception("写入 spec 失败 (spec_id=%s)", spec_id)
    return updated


def delete(spec_id: str) -> bool:
    """删除一条规范，返回是否删除成功。

    Phase 2.4：双删 —— specs 表（PG 后端）+ trace_store。
    """
    with _lock:
        existed = spec_id in _specs
        _specs.pop(spec_id, None)

    if existed:
        # Phase 2.4：双删 —— specs 表（PG 后端优先）
        if _pg_available():
            try:
                from app.mcp.core.storage.pg_store import delete_spec as _pg_delete
                _pg_delete(spec_id)
            except Exception:
                logger.debug("specs 表删除失败 (spec_id=%s)", spec_id, exc_info=True)
        try:
            delete_logs(spec_id)
        except Exception:
            pass
    return existed


def list_specs(kind: str | None = None, target: str | None = None) -> list[dict]:
    """列出所有规范，可按 kind / target 过滤。按 updated_at 倒序。

    C4 对标：首次调用时从存储层恢复 spec 到内存缓存，
    确保进程重启后 list_specs() 仍可正常工作。
    Phase 2.4：PG 后端优先从 specs 表恢复（消除 N+1）。
    """
    _restore_if_needed()

    with _lock:
        items = [dict(s) for s in _specs.values()]

    if kind:
        items = [s for s in items if s.get("kind") == kind]
    if target:
        items = [s for s in items if target in (s.get("target") or "")]
    items.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
    return items


def clear() -> None:
    """清空所有规范（测试用）。"""
    global _restored
    with _lock:
        _specs.clear()
        _restored = False
