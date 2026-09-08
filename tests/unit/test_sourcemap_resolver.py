"""sourcemap_resolver 单元测试 —— VLQ 编解码 / 段查询边界 / 帧解析 / 缓存 / 降级。

fixture 构造方式：测试内实现 base64-VLQ 编码器（与解码器互逆），
用绝对坐标生成 delta 编码的 mappings，保证往返语义可验证。
"""

from __future__ import annotations

import json
import os
import time

import pytest

from app.runtime.collectors.sourcemap_resolver import (
    OriginalPosition,
    ParserCache,
    SourceMapError,
    SourceMapParser,
    decode_vlq,
    is_frontend_frame,
    load_parser_from_dict,
    load_parser_from_file,
    resolve_frames,
)

_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


# ── 测试用 VLQ 编码器（与被测解码器互逆）──


def _encode_vlq_value(n: int) -> str:
    v = ((-n) << 1) | 1 if n < 0 else (n << 1)
    out = ""
    while True:
        digit = v & 31
        v >>= 5
        if v:
            out += _B64[digit | 32]
        else:
            out += _B64[digit]
            return out


def _encode_segment(fields: list[int]) -> str:
    return "".join(_encode_vlq_value(x) for x in fields)


def build_map(
    absolute_lines: list[list[tuple]],
    sources: list[str],
    names: list[str],
    sources_content: list | None = None,
) -> dict:
    """绝对坐标 → 标准 delta 编码 mappings。

    absolute_lines 每行是段列表；段为 (gen_col,) 或
    (gen_col, src_idx, src_line, src_col, name_idx|None)，坐标为绝对值。
    """
    out_lines: list[str] = []
    prev_src = prev_line = prev_col = prev_name = 0
    for segs in absolute_lines:
        parts: list[str] = []
        prev_gcol = 0
        for seg in segs:
            if len(seg) == 1:
                fields = [seg[0] - prev_gcol]
                prev_gcol = seg[0]
            else:
                gc, si, sl, sc = seg[0], seg[1], seg[2], seg[3]
                ni = seg[4] if len(seg) > 4 else None
                fields = [gc - prev_gcol, si - prev_src, sl - prev_line, sc - prev_col]
                prev_src, prev_line, prev_col, prev_gcol = si, sl, sc, gc
                if ni is not None:
                    fields.append(ni - prev_name)
                    prev_name = ni
            parts.append(_encode_segment(fields))
        out_lines.append(",".join(parts))
    return {
        "version": 3,
        "sources": sources,
        "names": names,
        "sourcesContent": sources_content,
        "mappings": ";".join(out_lines),
    }


# ── VLQ 解码 ──


class TestDecodeVlq:
    def test_basic_known_values(self):
        # A=0, C=1(2>>1), I=4(8>>1)
        assert decode_vlq("AAAA") == [0, 0, 0, 0]
        assert decode_vlq("IACA") == [4, 0, 1, 0]

    def test_roundtrip(self):
        cases = [
            [0, 0, 0, 0],
            [1, 2, 3, 4],
            [15, -3, 1024, -1023],
            [12345, 0, -65536, 65535],
            [7],
            [0, 0, 0, 0, 0],
        ]
        for fields in cases:
            encoded = _encode_segment(fields)
            assert decode_vlq(encoded) == fields, fields

    def test_invalid_char_raises(self):
        with pytest.raises(SourceMapError):
            decode_vlq("A*A")

    def test_truncated_continuation_raises(self):
        # 'g' = 32（仅 continuation bit，无终止数字）
        with pytest.raises(SourceMapError):
            decode_vlq("g")


# ── 解析与查询 ──


def _simple_map() -> dict:
    # 生成行 1（0-based 0）: col 0 → src0:1:0 name0；col 50 → src1:10:4 name1
    # 生成行 2（0-based 1）: col 10 → src0:21:3（无 name）
    # 生成行 3（0-based 2）: 空
    # 生成行 4（0-based 3）: col 5 → 无 source 信息段
    return build_map(
        [
            [(0, 0, 0, 0, 0), (50, 1, 9, 4, 1)],
            [(10, 0, 20, 3)],
            [],
            [(5,)],
        ],
        sources=["src/app.ts", "src/util.ts"],
        names=["handleSubmit", "formatPrice"],
    )


class TestSourceMapParser:
    def test_exact_hit(self):
        p = SourceMapParser(_simple_map())
        pos = p.original_position_for(1, 0)
        assert pos == OriginalPosition("src/app.ts", 1, 0, "handleSubmit")
        pos = p.original_position_for(1, 50)
        assert pos == OriginalPosition("src/util.ts", 10, 4, "formatPrice")

    def test_column_between_segments_picks_earlier(self):
        p = SourceMapParser(_simple_map())
        pos = p.original_position_for(1, 49)
        assert pos is not None and pos.source == "src/app.ts"
        pos = p.original_position_for(1, 51)
        assert pos is not None and pos.source == "src/util.ts"

    def test_column_before_first_segment(self):
        # 该行首个段 gen_col=50，col<50 无命中；用只有 col 50 起始的行验证
        m = build_map([[(50, 0, 0, 0)]], sources=["a.ts"], names=[])
        p = SourceMapParser(m)
        assert p.original_position_for(1, 49) is None
        assert p.original_position_for(1, 50) is not None

    def test_line_without_mappings(self):
        p = SourceMapParser(_simple_map())
        assert p.original_position_for(3, 0) is None   # 空行
        assert p.original_position_for(99, 0) is None  # 不存在的行

    def test_segment_without_source_info(self):
        p = SourceMapParser(_simple_map())
        assert p.original_position_for(4, 5) is None

    def test_second_line_absolute_deltas(self):
        # 跨行相对值还原：行 2 的段是绝对 src0:21:4（1-based line 21）
        p = SourceMapParser(_simple_map())
        pos = p.original_position_for(2, 10)
        assert pos is not None
        assert pos.source == "src/app.ts"
        assert pos.line == 21
        assert pos.column == 3
        assert pos.name is None

    def test_root_not_dict_raises(self):
        with pytest.raises(SourceMapError):
            SourceMapParser([1, 2])

    def test_missing_mappings_raises(self):
        with pytest.raises(SourceMapError):
            SourceMapParser({"version": 3, "sources": [], "names": []})

    def test_source_index_out_of_range_raises(self):
        m = build_map([[(0, 5, 0, 0)]], sources=["only.ts"], names=[])
        with pytest.raises(SourceMapError):
            SourceMapParser(m)

    def test_version_missing_tolerated_with_warning(self):
        m = _simple_map()
        del m["version"]
        p = SourceMapParser(m)  # 仅 warning 不拒绝
        assert p.original_position_for(1, 0) is not None

    def test_source_content(self):
        m = _simple_map()
        m["sourcesContent"] = ["line1\nline2\nline3", None]
        p = SourceMapParser(m)
        assert p.source_content("src/app.ts") == "line1\nline2\nline3"
        assert p.source_content("src/util.ts") is None
        assert p.source_content("not-exist.ts") is None


# ── 帧识别 ──


class TestIsFrontendFrame:
    @pytest.mark.parametrize("file", [
        "app.9f3b2c.js", "https://cdn.example.com/app.js", "//cdn.example.com/app.mjs",
        "main.jsx", "index.ts", "module.cjs", "HTTP://X.COM/A.JS",
    ])
    def test_frontend(self, file):
        assert is_frontend_frame(file) is True

    @pytest.mark.parametrize("file", [
        "/app/services/user.py", "C:\\proj\\main.py", "", None, "readme.md",
    ])
    def test_not_frontend(self, file):
        assert is_frontend_frame(file) is False


# ── 帧解析 ──


class TestResolveFrames:
    def _parser(self, with_content: bool = False) -> SourceMapParser:
        m = build_map(
            [[(0, 0, 0, 0, 0), (48213, 1, 9, 4, 1)]],
            sources=["src/app.ts", "src/util.ts"],
            names=["handleSubmit", "formatPrice"],
        )
        if with_content:
            m["sourcesContent"] = [
                None,
                "".join(f"// line {i}\n" for i in range(1, 13)).replace(
                    "// line 10", "export const formatPrice = (x) => x * 100;"
                ),
            ]
        return SourceMapParser(m)

    def test_resolved_frame_shape(self):
        frames = [{"file": "app.9f3b2c.js", "line": 1, "column": 48213, "function": "t"}]
        resolved, _snippets = resolve_frames(frames, self._parser())
        assert len(resolved) == 1
        f = resolved[0]
        assert f["file"] == "src/util.ts"
        assert f["line"] == 10
        assert f["column"] == 4
        assert f["function"] == "formatPrice"       # name 优先于压缩后函数名
        assert f["resolved"] is True
        assert f["original"] == {"file": "app.9f3b2c.js", "line": 1, "column": 48213}

    def test_python_frame_passthrough(self):
        frames = [{"file": "/app/services/user.py", "line": 42, "function": "get_user"}]
        resolved, snippets = resolve_frames(frames, self._parser())
        assert resolved[0] == frames[0]
        assert snippets == []

    def test_frontend_miss_passthrough(self):
        # column 不在任何段之前命中（col 0 命中段 0 —— 用不存在映射的行验证）
        frames = [{"file": "app.js", "line": 9, "column": 0, "function": "x"}]
        resolved, _ = resolve_frames(frames, self._parser())
        assert resolved[0] == frames[0]

    def test_missing_column_on_multi_target_line_is_not_resolved(self):
        """缺列号 + 同行多个候选源码位置 → 不得按第 0 列猜测并冒充精确还原。

        本用例替代 v0.7.6 之前的 test_missing_column_defaults_zero：那条断言的
        正是缺陷本身（列号被折叠成 0，于是返回同行第一个段的 src/app.ts，
        而真实答案是 col 48213 的 src/util.ts:10 formatPrice）。
        """
        frames = [{"file": "app.9f3b2c.js", "line": 1, "function": "t"}]  # 无 column
        resolved, snippets = resolve_frames(frames, self._parser())
        assert resolved[0] == frames[0]          # 原样返回
        assert "resolved" not in resolved[0]     # 不标注已还原
        assert resolved[0]["file"] == "app.9f3b2c.js"
        assert snippets == []                    # 不给出可能错误的源码片段
        # 同一帧带上真实列号后仍然精确还原（证明降级只由缺列号触发）
        exact, _ = resolve_frames([{**frames[0], "column": 48213}], self._parser())
        assert exact[0]["file"] == "src/util.ts"
        assert exact[0]["resolved"] is True

    def test_missing_column_on_single_target_line_still_resolves(self):
        """唯一候选：整行只映射到一个源码位置时，缺列号不存在歧义，仍应还原。

        守住修复的边界——不能把非 minified / 单段行（旧 SDK、onerror 兜底等
        常见无列号来源）一并退化成不还原。
        """
        m = build_map(
            # 段元组为绝对坐标 (gen_col, src_idx, src_line, src_col, name_idx)：
            # 两段同属 src/app.ts 第 0 行，仅生成列/原始列/符号不同 → 唯一候选
            [[(0, 0, 0, 0, 0), (12, 0, 0, 5, 1)]],
            sources=["src/app.ts", "src/util.ts"],
            names=["handleSubmit", "formatPrice"],
        )
        frames = [{"file": "app.9f3b2c.js", "line": 1, "function": "t"}]
        resolved, _ = resolve_frames(frames, SourceMapParser(m))
        assert resolved[0]["file"] == "src/app.ts"
        assert resolved[0]["line"] == 1
        assert resolved[0]["resolved"] is True
        # 还原结果不带伪造的列号断言，仅确认未被降级
        assert resolved[0]["original"]["column"] is None

    def test_snippet_from_sources_content(self):
        frames = [{"file": "app.js", "line": 1, "column": 48213, "function": "t"}]
        _resolved, snippets = resolve_frames(frames, self._parser(with_content=True))
        assert len(snippets) == 1
        s = snippets[0]
        assert s["found"] is True
        assert s["file"] == "src/util.ts"
        assert s["error_line"] == 10
        assert ">>> 10:" in s["snippet"]

    def test_snippet_fallback_to_code_locator_whitelist(self):
        # 无 sourcesContent 时走 code_locator；非白名单绝对路径 → found=False
        frames = [{"file": "app.js", "line": 1, "column": 48213, "function": "t"}]
        _resolved, snippets = resolve_frames(frames, self._parser())
        assert snippets[0]["found"] is False
        assert snippets[0]["snippet"] == ""

    def test_parser_exception_degrades_to_original(self):
        class BoomParser:
            def original_position_for(self, line, column):
                raise RuntimeError("boom")

            def source_content(self, source):
                return None

        frames = [{"file": "app.js", "line": 1, "column": 5, "function": "t"}]
        resolved, _ = resolve_frames(frames, BoomParser())  # type: ignore[arg-type]
        assert resolved[0] == frames[0]

    def test_input_not_mutated(self):
        frames = [{"file": "app.9f3b2c.js", "line": 1, "column": 48213, "function": "t"}]
        snapshot = dict(frames[0])
        resolve_frames(frames, self._parser())
        assert frames[0] == snapshot


# ── ingest → resolver 跨模块契约 ──


class TestIngestToResolverSeam:
    """SDK 上报帧 → ingest 规范化 → source map 还原。

    v0.7.6 之前这条缝没有任何用例：两侧各自单测（test_ingest 只验到入库、
    本文件只验已规范化的帧），缺陷正好落在缝上——ingest 刻意不为缺列号伪造 0，
    resolver 却又把缺列号折叠成 0 并按第 0 列"精确还原"。
    """

    _MINIFIED = "https://cdn.example.com/static/js/app.9f3b2c.js"

    def _parser(self) -> SourceMapParser:
        # 复用多段歧义行 fixture：gen 1:0 → src/app.ts:1 handleSubmit，
        #                    gen 1:48213 → src/util.ts:10 formatPrice
        return TestResolveFrames()._parser()

    def test_real_column_survives_ingest_into_resolver(self):
        from app.mcp.tools.ingest_api import _parse_frames

        stored = _parse_frames(
            [{"file": self._MINIFIED, "line": 1, "column": 48213, "function": "t"}]
        )
        assert stored[0]["column"] == 48213
        resolved, _ = resolve_frames(stored, self._parser())
        assert resolved[0]["file"] == "src/util.ts"
        assert resolved[0]["line"] == 10
        assert resolved[0]["function"] == "formatPrice"
        assert resolved[0]["resolved"] is True

    def test_absent_column_is_not_fabricated_then_not_resolved(self):
        from app.mcp.tools.ingest_api import _parse_frames

        stored = _parse_frames([{"file": self._MINIFIED, "line": 1, "function": "t"}])
        assert "column" not in stored[0]                 # ingest 不伪造 0
        resolved, snippets = resolve_frames(stored, self._parser())
        assert "resolved" not in resolved[0]             # 契约必须延续到还原
        assert resolved[0]["file"] == self._MINIFIED     # 保持生成位置，不误导
        assert snippets == []

    def test_bool_column_is_treated_as_missing(self):
        """True 不是列号 1：JSON 中出现布尔值时按缺列号处理。"""
        from app.mcp.tools.ingest_api import _parse_frames

        frame = {"file": self._MINIFIED, "line": 1, "function": "t", "column": True}
        assert "column" not in _parse_frames([frame])[0]
        resolved, _ = resolve_frames([frame], self._parser())
        assert "resolved" not in resolved[0]

    def test_line_targets_reports_candidate_count(self):
        """歧义判定依据本身可测：多候选行 vs 唯一候选行 vs 无映射行。"""
        parser = self._parser()
        assert len(parser.line_targets(1)) == 2          # app.ts:1 与 util.ts:10
        assert parser.line_targets(9) == []              # 无映射行
        single = SourceMapParser(build_map(
            [[(0, 0, 0, 0, 0), (12, 0, 0, 5, 1)]],
            sources=["src/app.ts", "src/util.ts"],
            names=["handleSubmit", "formatPrice"],
        ))
        assert single.line_targets(1) == [("src/app.ts", 1)]


# ── 缓存 ──


class TestParserCache:
    def test_hit_avoids_reload(self):
        calls = []

        def loader():
            calls.append(1)
            return SourceMapParser(_simple_map())

        cache = ParserCache()
        p1 = cache.get("k", "fp1", loader)
        p2 = cache.get("k", "fp1", loader)
        assert p1 is p2
        assert len(calls) == 1

    def test_fingerprint_change_reloads(self):
        calls = []

        def loader():
            calls.append(1)
            return SourceMapParser(_simple_map())

        cache = ParserCache()
        cache.get("k", "fp1", loader)
        cache.get("k", "fp2", loader)
        assert len(calls) == 2

    def test_lru_eviction(self):
        cache = ParserCache(maxsize=2)
        loader = lambda: SourceMapParser(_simple_map())  # noqa: E731
        cache.get("a", 1, loader)
        cache.get("b", 1, loader)
        cache.get("a", 1, loader)  # a 变最新
        cache.get("c", 1, loader)  # 驱逐 b
        assert list(cache._cache.keys()) == ["a", "c"]

    def test_load_from_dict_cached(self):
        m = _simple_map()
        calls = []

        def loader():
            calls.append(1)
            return SourceMapParser(m)

        cache = ParserCache()
        p1 = load_parser_from_dict(m, "fp", "upload:app.js", cache)
        assert p1 is not None
        p2 = load_parser_from_dict(m, "fp", "upload:app.js", cache)
        assert p1 is p2  # 缓存命中返回同一实例
        # 缓存命中时 loader 不再被调用
        cache.get("upload:app.js", "fp", loader)
        assert calls == []


class TestLoadParserFromFile:
    def test_load_and_cache(self, tmp_path):
        map_file = tmp_path / "app.js.map"
        map_file.write_text(json.dumps(_simple_map()), encoding="utf-8")

        p = load_parser_from_file(str(map_file))
        assert p.original_position_for(1, 0) is not None

        # 二次加载命中缓存（mtime 未变）
        p2 = load_parser_from_file(str(map_file))
        assert p is p2

    def test_mtime_change_invalidates(self, tmp_path):
        map_file = tmp_path / "app.js.map"
        map_file.write_text(json.dumps(_simple_map()), encoding="utf-8")
        p1 = load_parser_from_file(str(map_file))
        # 手动推进 mtime（避免同秒精度问题）
        st = os.stat(str(map_file))
        os.utime(str(map_file), ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        p2 = load_parser_from_file(str(map_file))
        assert p1 is not p2

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_parser_from_file(str(tmp_path / "nope.map"))


# ── 性能 ──


class TestPerformance:
    def test_large_map_parse_and_query(self):
        # 2000 行 × 30 段 = 6 万段，解析应在秒级内完成
        lines = []
        for gl in range(2000):
            segs = []
            for k in range(30):
                segs.append((k * 100, gl % 5, gl + k, k * 2, k % 10))
            lines.append(segs)
        m = build_map(lines, sources=[f"s{i}.ts" for i in range(5)],
                      names=[f"n{i}" for i in range(10)])

        t0 = time.perf_counter()
        p = SourceMapParser(m)
        parse_s = time.perf_counter() - t0

        t0 = time.perf_counter()
        for i in range(1000):
            p.original_position_for((i % 2000) + 1, (i * 37) % 3000)
        query_s = time.perf_counter() - t0

        assert parse_s < 5.0, f"解析过慢: {parse_s:.2f}s"
        assert query_s < 0.5, f"查询过慢: {query_s:.2f}s"
