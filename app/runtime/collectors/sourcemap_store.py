"""Source Map 上报存储与自动选路 —— 进程内 TTL + 容量上限（单机，v0.5.1 SM2）。

两条获取通道（均受 sourcemap_enabled 总开关控制，默认关闭）：
1. 上传通道：POST /api/debug/sourcemap 上传 .map JSON，按 artifact 键存储；
   TTL 过期 + LRU 容量驱逐（模式对齐 errors.py 的有限容量语义）。
2. 磁盘约定：帧文件 foo.js → <sourcemap_path_prefix>/foo.js.map，
   路径必须落在 code_locator 白名单允许根内（防 LFI，复用 SEC-01 语义）。

自动选路 resolve_frames_auto：显式 artifact > 上传按帧文件名匹配 > 磁盘约定。
任何失败静默降级（返回原始帧 + 空 snippets），绝不阻断 Debug Context 构建。

架构约束：归属 runtime 层，不依赖 app.mcp / app.agent / app.llm / app.rag。
单机限制：进程内存储，多 Worker 不共享（记入 DESIGN「当前限制」）。
"""

from __future__ import annotations

import itertools
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Optional

from app.config import settings
from app.runtime.collectors.sourcemap_resolver import (
    SourceMapError,
    SourceMapParser,
    is_frontend_frame,
    load_parser_from_dict,
    load_parser_from_file,
    resolve_frame,
)

logger = logging.getLogger("ai-debug-mcp.runtime.collectors.sourcemap_store")

# artifact -> {"map": dict, "expires_at": float, "token": int}
_uploads: OrderedDict[str, dict] = OrderedDict()
_lock = threading.Lock()
_token_seq = itertools.count(1)


def _validate_map(map_obj: object) -> dict:
    """结构校验：必须是含 mappings(str) 与 sources(list) 的 JSON 对象。非法抛 ValueError。"""
    if not isinstance(map_obj, dict):
        raise ValueError("source map 必须是 JSON 对象")
    if not isinstance(map_obj.get("mappings"), str):
        raise ValueError("source map 缺少 mappings 字符串字段")
    sources = map_obj.get("sources")
    if not isinstance(sources, list):
        raise ValueError("source map 缺少 sources 数组字段")
    return map_obj


def upload_sourcemap(artifact: str, map_obj: object, ttl_seconds: int | None = None) -> dict:
    """存储一份上传的 source map（覆盖同名 artifact），返回存储回执。"""
    artifact = (artifact or "").strip()
    if not artifact or len(artifact) > 256:
        raise ValueError("artifact 不能为空且长度需 <= 256")
    _validate_map(map_obj)

    ttl = ttl_seconds if ttl_seconds is not None else settings.sourcemap_upload_ttl_seconds
    ttl = max(1, int(ttl))
    max_uploads = max(1, int(settings.sourcemap_max_uploads))

    with _lock:
        _uploads.pop(artifact, None)  # 覆盖时先移除，保证 LRU 位置正确
        while len(_uploads) >= max_uploads:
            evicted, _ = _uploads.popitem(last=False)
            logger.info("source map 上传容量已满，驱逐最旧 artifact=%s", evicted)
        _uploads[artifact] = {
            "map": map_obj,
            "expires_at": time.time() + ttl,
            "token": next(_token_seq),
        }
        return {
            "artifact": artifact,
            "stored": True,
            "expires_at": _uploads[artifact]["expires_at"],
            "total_uploads": len(_uploads),
        }


def get_uploaded_map(artifact: str) -> Optional[dict]:
    """取未过期的上传 map；过期则驱逐并返回 None。"""
    artifact = (artifact or "").strip()
    if not artifact:
        return None
    with _lock:
        entry = _uploads.get(artifact)
        if entry is None:
            return None
        if entry["expires_at"] <= time.time():
            _uploads.pop(artifact, None)
            return None
        _uploads.move_to_end(artifact)
        return entry["map"]


def _uploaded_token(artifact: str) -> Optional[int]:
    """取上传条目的指纹 token（覆盖上传后变化，用于解析缓存失效）。"""
    with _lock:
        entry = _uploads.get((artifact or "").strip())
        if entry is None or entry["expires_at"] <= time.time():
            return None
        return entry["token"]


def get_uploaded_parser(artifact: str) -> Optional[SourceMapParser]:
    """构造/复用上传 map 的解析器；无上传或结构非法返回 None（非法条目被驱逐）。"""
    token = _uploaded_token(artifact)
    if token is None:
        return None
    map_obj = get_uploaded_map(artifact)
    if map_obj is None:
        return None
    try:
        return load_parser_from_dict(map_obj, token, f"upload:{artifact.strip()}")
    except SourceMapError:
        logger.warning("上传的 source map 解析失败，已驱逐 artifact=%s", artifact)
        with _lock:
            _uploads.pop((artifact or "").strip(), None)
        return None


def _frame_basename(file: str) -> Optional[str]:
    """从帧文件（可能是 URL）提取安全 basename；含路径穿越成分返回 None。"""
    name = (file or "").split("?")[0].split("#")[0].rstrip("/")
    name = name.rsplit("/", 1)[-1]
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return None
    return name


def _disk_parser(file: str) -> Optional[SourceMapParser]:
    """磁盘约定通道：<prefix>/<basename>.map；路径必须落在白名单允许根内。"""
    from app.runtime.collectors.code_locator import _is_allowed

    prefix = (settings.sourcemap_path_prefix or "").strip()
    if not prefix:
        return None
    name = _frame_basename(file)
    if name is None:
        return None
    candidate = os.path.join(prefix, name + ".map")
    abs_candidate = os.path.abspath(candidate)
    if not _is_allowed(abs_candidate):
        logger.warning("source map 磁盘路径不在白名单内，拒绝读取: %s", abs_candidate)
        return None
    if not os.path.isfile(abs_candidate):
        return None
    try:
        return load_parser_from_file(abs_candidate)
    except Exception:
        logger.warning("source map 磁盘文件解析失败: %s", abs_candidate, exc_info=True)
        return None


def _parser_for_frame(frame: dict, artifact: Optional[str]) -> Optional[SourceMapParser]:
    """单帧选路：显式 artifact > 上传按 basename 匹配 > 磁盘约定。"""
    file = str(frame.get("file") or "")
    if artifact:
        parser = get_uploaded_parser(artifact)
        if parser is not None:
            return parser
    name = _frame_basename(file)
    if name:
        parser = get_uploaded_parser(name)
        if parser is not None:
            return parser
    return _disk_parser(file)


def resolve_frames_auto(
    frames: list[dict],
    artifact: Optional[str] = None,
    context_lines: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """自动选路解析帧（受 sourcemap_enabled 总开关控制）。

    返回 (resolved_frames, code_snippets)；关闭或无可用 map 时返回
    (原帧列表拷贝, [])，绝不抛异常（与 builder 各 collector 降级纪律一致）。
    """
    if not settings.sourcemap_enabled:
        return [dict(f) for f in frames or []], []

    ctx = context_lines if context_lines is not None else settings.code_context_lines
    resolved_frames: list[dict] = []
    snippets: list[dict] = []

    # 同一 basename 只选路一次（避免每帧重复查上传表/磁盘）
    parser_by_key: dict[str, Optional[SourceMapParser]] = {}

    for frame in frames or []:
        try:
            if not is_frontend_frame(str(frame.get("file") or "")):
                resolved_frames.append(dict(frame))
                continue

            name = _frame_basename(str(frame.get("file") or ""))
            key = artifact or name or ""
            if key not in parser_by_key:
                parser_by_key[key] = _parser_for_frame(frame, artifact)
            parser = parser_by_key[key]
            if parser is None:
                resolved_frames.append(dict(frame))
                continue

            new_frame, snippet = resolve_frame(frame, parser, ctx)
            resolved_frames.append(new_frame)
            if snippet is not None:
                snippets.append(snippet)
        except Exception:
            logger.warning("source map 自动解析失败，保留原始帧: %r", frame, exc_info=True)
            resolved_frames.append(dict(frame))

    return resolved_frames, snippets
