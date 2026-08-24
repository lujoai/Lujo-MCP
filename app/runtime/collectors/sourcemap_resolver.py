"""Source Map 解析器 —— 把 minified JS 堆栈帧还原为原始源码位置。

设计定位（v0.5.1 SM1，纯计算模块）：
- 生产环境前端堆栈是压缩后的 file:line:column（如 app.9f3b2c.js:1:48213），
  直接喂给 code_locator / static_analyzer / fault_localizer 三条证据链全部失效。
- 本模块解析标准 Source Map（version 3）的 mappings（base64-VLQ 编码），
  将生成位置 (line, column) 映射回 (sources[i], original_line, original_column, names[j])。
- 零外部依赖：VLQ 解码手写（与 static_analyzer 用 ast 的零依赖哲学一致）。

依赖约束：
- 仅允许 app.runtime.collectors（code_locator 兜底读本地源码）+ Python 标准库。

架构约束（Architecture Frozen）：
- 归属 runtime 层；不依赖 app.mcp / app.agent / app.llm / app.rag。

降级纪律（与 Qdrant / static_analyzer fail-safe 模式一致）：
- 解析失败 / 无命中 / 索引越界 → 返回原始帧 + warning，绝不 raise 穿透主链路。
"""

from __future__ import annotations

import bisect
import json
import logging
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger("lujo-mcp.runtime.collectors.sourcemap")

# ── base64-VLQ 解码表 ──
_B64_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_B64_LOOKUP = {c: i for i, c in enumerate(_B64_CHARS)}

# 前端帧识别：文件后缀或 URL 形式
_FRONTEND_EXTS = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")


class SourceMapError(ValueError):
    """Source Map 结构非法（内部异常，调用方按降级处理，不外抛）。"""


@dataclass(slots=True)
class _Segment:
    """单个 mapping 段：生成列 → 原始位置。source_idx 为 None 表示无源信息段。"""

    gen_col: int
    source_idx: Optional[int] = None
    source_line: Optional[int] = None
    source_col: Optional[int] = None
    name_idx: Optional[int] = None


@dataclass(slots=True)
class OriginalPosition:
    """查询结果：原始源码位置。"""

    source: str
    line: int          # 1-based（与 mappings 内 0-based 的差已消除）
    column: int        # 0-based（保持 source map 惯例）
    name: Optional[str] = None


def decode_vlq(segment: str) -> list[int]:
    """解码一个 base64-VLQ 段（逗号分隔的字段组）为整数列表。

    每个整数由若干 6-bit base64 数字组成：最低位是符号位（1=负），
    其余位右移一位后为绝对值；数字间以 continuation bit 续接。
    """
    values: list[int] = []
    value = 0
    shift = 0
    for ch in segment:
        digit = _B64_LOOKUP.get(ch)
        if digit is None:
            raise SourceMapError(f"mappings 含非法 base64 字符: {ch!r}")
        # 低 5 位是数据，第 6 位（32）是 continuation bit
        value |= (digit & 0x1F) << shift
        if digit & 0x20:
            shift += 5
            if shift > 30:
                raise SourceMapError("VLQ 整数溢出（shift > 30）")
            continue
        # 符号位解码
        negative = value & 1
        value >>= 1
        values.append(-value if negative else value)
        value = 0
        shift = 0
    if shift != 0:
        raise SourceMapError("VLQ 段以 continuation bit 结尾，编码不完整")
    return values


class SourceMapParser:
    """解析并索引一份 Source Map（version 3），支持 (line, column) 查询。"""

    def __init__(self, map_obj: dict):
        if not isinstance(map_obj, dict):
            raise SourceMapError("source map 根节点必须是 JSON 对象")
        version = map_obj.get("version")
        if version != 3:
            # 宽容处理：仅 warning，不拒绝（部分工具产出 "3" 字符串 / 缺省）
            logger.warning("非标准 source map version=%r，按 v3 尝试解析", version)
        self.sources: list[str] = list(map_obj.get("sources") or [])
        self.names: list[str] = list(map_obj.get("names") or [])
        self.sources_content: list[Optional[str]] = list(
            map_obj.get("sourcesContent") or [None] * len(self.sources)
        )
        mappings = map_obj.get("mappings")
        if not isinstance(mappings, str):
            raise SourceMapError("source map 缺少 mappings 字符串")
        # line -> 已排序的 gen_col 列表（与 _lines[line] 平行，供 bisect）
        self._lines: dict[int, list[_Segment]] = {}
        self._cols: dict[int, list[int]] = {}
        self._parse_mappings(mappings)

    # ── 解析 ──

    def _parse_mappings(self, mappings: str) -> None:
        # source/line/col/name 四段是全文件级相对值（跨行延续）；
        # gen_col 是行内相对值（相对同行前一段的增量，行首基准 0，换行重置）
        src_idx = 0
        src_line = 0
        src_col = 0
        name_idx = 0
        for gen_line, line_str in enumerate(mappings.split(";")):
            if not line_str:
                continue
            segments: list[_Segment] = []
            gen_col = 0
            for seg_str in line_str.split(","):
                if not seg_str:
                    continue
                fields = decode_vlq(seg_str)
                if not fields:
                    continue
                # FIX: 规范要求行内累加；此前直接取 fields[0] 当绝对值，
                # 多 segment 行（生产 minified bundle 常态）除首段外全部错位
                gen_col += fields[0]
                if len(fields) != 1 and len(fields) not in (4, 5):
                    raise SourceMapError(f"mapping 段字段数非法: {len(fields)}")
                seg = _Segment(gen_col=gen_col)
                if len(fields) >= 4:
                    src_idx += fields[1]
                    src_line += fields[2]
                    src_col += fields[3]
                    if not (0 <= src_idx < len(self.sources)):
                        raise SourceMapError(f"source 索引越界: {src_idx}")
                    if src_line < 0 or src_col < 0:
                        raise SourceMapError("source line/col 为负")
                    seg.source_idx = src_idx
                    seg.source_line = src_line
                    seg.source_col = src_col
                if len(fields) == 5:
                    name_idx += fields[4]
                    if not (0 <= name_idx < len(self.names)):
                        raise SourceMapError(f"name 索引越界: {name_idx}")
                    seg.name_idx = name_idx
                segments.append(seg)
            if segments:
                segments.sort(key=lambda s: s.gen_col)
                self._lines[gen_line] = segments
                self._cols[gen_line] = [s.gen_col for s in segments]

    # ── 查询 ──

    def original_position_for(self, line: int, column: int) -> Optional[OriginalPosition]:
        """查询生成位置 (line 1-based, column 0-based) 对应的原始位置。

        命中该行上 gen_col <= column 的最近段；无源信息段或行不存在返回 None。
        """
        segments = self._lines.get(line - 1)
        if not segments:
            return None
        cols = self._cols[line - 1]
        idx = bisect.bisect_right(cols, column) - 1
        if idx < 0:
            return None
        seg = segments[idx]
        if seg.source_idx is None or seg.source_line is None:
            return None
        name = self.names[seg.name_idx] if seg.name_idx is not None else None
        return OriginalPosition(
            source=self.sources[seg.source_idx],
            line=seg.source_line + 1,   # 0-based -> 1-based
            column=seg.source_col or 0,
            name=name,
        )

    def source_content(self, source: str) -> Optional[str]:
        """返回内嵌 sourcesContent 中该 source 的内容（无则 None）。"""
        try:
            idx = self.sources.index(source)
        except ValueError:
            return None
        if idx < len(self.sources_content):
            return self.sources_content[idx]
        return None


class ParserCache:
    """解析结果 LRU 缓存：按 key 缓存 SourceMapParser，指纹变化时失效。"""

    def __init__(self, maxsize: int = 16):
        self._maxsize = maxsize
        self._cache: OrderedDict[str, tuple[Any, SourceMapParser]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str, fingerprint: Any, loader: Callable[[], SourceMapParser]) -> SourceMapParser:
        """命中且指纹一致返回缓存；否则调用 loader 解析并写回（失败异常向上抛）。"""
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[0] == fingerprint:
                self._cache.move_to_end(key)
                return hit[1]
        parser = loader()
        with self._lock:
            self._cache[key] = (fingerprint, parser)
            self._cache.move_to_end(key)
            while len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)
        return parser


# 模块级共享缓存（上传通道与磁盘通道共用；key 带来源前缀避免冲突）
_parser_cache = ParserCache()


def load_parser_from_file(path: str, cache: ParserCache | None = None) -> SourceMapParser:
    """从磁盘加载 .map 文件（带 mtime 指纹的 LRU 缓存）。路径非法/解析失败抛异常。"""
    cache = cache or _parser_cache
    fp = os.path.getmtime(path)
    return cache.get(f"file:{os.path.abspath(path)}", fp, lambda: _parse_file(path))


def _parse_file(path: str) -> SourceMapParser:
    with open(path, encoding="utf-8") as f:
        return SourceMapParser(json.load(f))


def load_parser_from_dict(map_obj: dict, fingerprint: Any, cache_key: str,
                          cache: ParserCache | None = None) -> SourceMapParser:
    """从已解析的 dict 构建 parser（带指纹缓存，供上传通道复用）。"""
    cache = cache or _parser_cache
    return cache.get(cache_key, fingerprint, lambda: SourceMapParser(map_obj))


def is_frontend_frame(file: str | None) -> bool:
    """判断堆栈帧是否前端帧（.js/.mjs/... 后缀或 URL 形式）。"""
    if not file:
        return False
    f = file.lower()
    if f.startswith(("http://", "https://", "//")):
        return True
    return f.endswith(_FRONTEND_EXTS)


def _snippet_from_content(content: str, line: int, context_lines: int) -> Optional[str]:
    """从内嵌 sourcesContent 提取报错行上下片段（格式对齐 code_locator）。"""
    lines = content.splitlines()
    if not (1 <= line <= len(lines)):
        return None
    start = max(1, line - context_lines)
    end = min(len(lines), line + context_lines)
    out = []
    for i in range(start, end + 1):
        marker = ">>> " if i == line else "    "
        out.append(f"{marker}{i}: {lines[i - 1].rstrip()}")
    return "\n".join(out)


def resolve_frame(
    frame: dict,
    parser: SourceMapParser,
    context_lines: int,
) -> tuple[dict, Optional[dict]]:
    """解析单帧，返回 (frame', snippet|None)。失败降级返回 (原帧, None)。"""
    from app.runtime.collectors.code_locator import get_code_snippet

    file = str(frame.get("file") or "")
    line = int(frame.get("line") or 0)
    column = int(frame.get("column") or 0)
    if not is_frontend_frame(file) or line <= 0:
        return dict(frame), None

    pos = parser.original_position_for(line, column)
    if pos is None:
        return dict(frame), None

    new_frame = dict(frame)
    new_frame.update({
        "file": pos.source,
        "line": pos.line,
        "function": pos.name or frame.get("function") or "",
        "column": pos.column,
        "resolved": True,
        "original": {"file": file, "line": line, "column": column},
    })

    # 源码片段：sourcesContent 优先，白名单本地文件兑底
    content = parser.source_content(pos.source)
    snippet_text = _snippet_from_content(content, pos.line, context_lines) if content else None
    if snippet_text is not None:
        snippet = {
            "file": pos.source,
            "error_line": pos.line,
            "snippet": snippet_text,
            "found": True,
            "link": None,
        }
    else:
        snippet = get_code_snippet(pos.source, pos.line, context_lines).model_dump()
    return new_frame, snippet


def resolve_frames(
    frames: list[dict],
    parser: SourceMapParser,
    context_lines: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """用 parser 批量解析帧，返回 (resolved_frames, code_snippets)。

    resolved frame 保持 StackFrame 兼容形状（file/line/function 指向原始源码），
    并附 original（minified 原位置）与 resolved 标记；无法解析的帧原样保留。
    snippets 优先取内嵌 sourcesContent，其次走 code_locator（受白名单约束）。
    任何单帧失败静默降级（resolved=False），绝不抛异常。
    """
    from app.config import settings

    ctx = context_lines if context_lines is not None else settings.code_context_lines
    resolved_frames: list[dict] = []
    snippets: list[dict] = []

    for frame in frames or []:
        try:
            new_frame, snippet = resolve_frame(frame, parser, ctx)
            resolved_frames.append(new_frame)
            if snippet is not None:
                snippets.append(snippet)
        except Exception:
            logger.warning("source map 帧解析失败，保留原始帧: %r", frame, exc_info=True)
            resolved_frames.append(dict(frame))

    return resolved_frames, snippets
