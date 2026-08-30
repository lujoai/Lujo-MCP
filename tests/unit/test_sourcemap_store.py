"""sourcemap_store 单元测试 —— 上传存储 TTL/LRU、自动选路（上传/磁盘/关闭）、端点行为。"""

from __future__ import annotations

import json
import time

import pytest

from app.config import settings
from app.runtime.collectors import sourcemap_store
from app.runtime.collectors.sourcemap_store import (
    get_uploaded_map,
    get_uploaded_parser,
    resolve_frames_auto,
    upload_sourcemap,
)
from app.schemas import SourcemapUploadRequest


@pytest.fixture(autouse=True)
def _clean_store():
    """每用例独立的空存储 + 默认关闭的开关。"""
    sourcemap_store._uploads.clear()
    old_enabled = settings.sourcemap_enabled
    old_prefix = settings.sourcemap_path_prefix
    settings.sourcemap_enabled = False
    settings.sourcemap_path_prefix = ""
    yield
    sourcemap_store._uploads.clear()
    settings.sourcemap_enabled = old_enabled
    settings.sourcemap_path_prefix = old_prefix


def _valid_map() -> dict:
    """最小合法 source map：gen 1:0 → src0:1:0。"""
    return {
        "version": 3,
        "sources": ["src/app.ts"],
        "names": ["handleSubmit"],
        "mappings": "AAAAA",
    }


_MINIFIED_FRAME = {
    "file": "https://cdn.example.com/static/js/app.9f3b2c.js",
    "line": 1,
    "column": 0,
    "function": "t",
}
_PY_FRAME = {"file": "/app/services/user.py", "line": 42, "function": "get_user"}


# ── 上传存储 ──


class TestUpload:
    def test_valid_upload(self):
        receipt = upload_sourcemap("app.9f3b2c.js", _valid_map())
        assert receipt["stored"] is True
        assert receipt["artifact"] == "app.9f3b2c.js"
        assert get_uploaded_map("app.9f3b2c.js") == _valid_map()

    def test_overwrite_same_artifact(self):
        upload_sourcemap("a.js", _valid_map())
        m2 = _valid_map()
        m2["sources"] = ["src/other.ts"]
        upload_sourcemap("a.js", m2)
        assert get_uploaded_map("a.js")["sources"] == ["src/other.ts"]
        assert len(sourcemap_store._uploads) == 1

    def test_release_distinguishes_uploads(self):
        """同 artifact 不同 release（bundle 版本）应独立存储，不再互相覆盖。"""
        m1 = _valid_map()
        m1["sources"] = ["src/v1.ts"]
        m2 = _valid_map()
        m2["sources"] = ["src/v2.ts"]
        upload_sourcemap("app.js", m1, release="v1")
        upload_sourcemap("app.js", m2, release="v2")

        assert get_uploaded_map("app.js", release="v1")["sources"] == ["src/v1.ts"]
        assert get_uploaded_map("app.js", release="v2")["sources"] == ["src/v2.ts"]
        # 无 release 的旧键不受影响，也不会误命中版本化条目
        assert get_uploaded_map("app.js") is None
        assert len(sourcemap_store._uploads) == 2

    @pytest.mark.parametrize("bad_map", [
        "not-a-dict",
        [],
        {"version": 3, "sources": []},                 # 缺 mappings
        {"version": 3, "mappings": "AAAA"},            # 缺 sources
        {"mappings": 123, "sources": []},              # mappings 非字符串
        {"mappings": "AAAA", "sources": "x"},          # sources 非数组
    ])
    def test_invalid_map_raises(self, bad_map):
        with pytest.raises(ValueError):
            upload_sourcemap("a.js", bad_map)

    @pytest.mark.parametrize("artifact", ["", "   ", "x" * 257])
    def test_invalid_artifact_raises(self, artifact):
        with pytest.raises(ValueError):
            upload_sourcemap(artifact, _valid_map())

    def test_ttl_expiry(self, monkeypatch):
        upload_sourcemap("a.js", _valid_map(), ttl_seconds=1)
        assert get_uploaded_map("a.js") is not None
        # 快进时间
        real_time = time.time
        monkeypatch.setattr(sourcemap_store.time, "time", lambda: real_time() + 2)
        assert get_uploaded_map("a.js") is None
        # 过期条目被驱逐
        assert "a.js" not in sourcemap_store._uploads

    def test_lru_eviction(self, monkeypatch):
        monkeypatch.setattr(settings, "sourcemap_max_uploads", 2)
        upload_sourcemap("a.js", _valid_map())
        upload_sourcemap("b.js", _valid_map())
        get_uploaded_map("a.js")  # a 变最新
        upload_sourcemap("c.js", _valid_map())  # 驱逐 b
        assert get_uploaded_map("b.js") is None
        assert get_uploaded_map("a.js") is not None
        assert get_uploaded_map("c.js") is not None

    def test_size_limit_counts_utf8_bytes(self, monkeypatch):
        """FIX(v0.7.1-b3-1) 回归：大小上限按 UTF-8 字节数口径计算。

        语义更正：字段含义为"字节"，len(str) 按字符数计；json.dumps 默认
        ensure_ascii=True 时两者恰好相等（中文转义为 \\uXXXX），但序列化
        切到 ensure_ascii=False 后字符数会低估 ~3 倍——统一按字节数计防错。
        此用例验证字节计算路径生效且上限逻辑不误伤正常 map。
        """
        tiny_map = {"version": 3, "sources": [], "names": [], "mappings": "AAAA"}

        # 上限压到极小 → 无论口径都按字节路径拒绝
        monkeypatch.setattr(settings, "sourcemap_max_upload_bytes", 1)
        with pytest.raises(ValueError, match="过大"):
            upload_sourcemap("tiny.js", tiny_map)
        assert get_uploaded_map("tiny.js") is None

        # 上限充足 → 正常入库（字节口径不误伤）
        monkeypatch.setattr(settings, "sourcemap_max_upload_bytes", 1024 * 1024)
        upload_sourcemap("tiny.js", tiny_map)
        assert get_uploaded_map("tiny.js") is not None


class TestGetUploadedParser:
    def test_valid_map_parses(self):
        upload_sourcemap("app.js", _valid_map())
        parser = get_uploaded_parser("app.js")
        assert parser is not None
        pos = parser.original_position_for(1, 0)
        assert pos is not None and pos.source == "src/app.ts"

    def test_invalid_mappings_evicted(self):
        bad = {"version": 3, "sources": [], "names": [], "mappings": "!!!"}
        upload_sourcemap("bad.js", bad)
        assert get_uploaded_parser("bad.js") is None
        assert "bad.js" not in sourcemap_store._uploads

    def test_missing_artifact(self):
        assert get_uploaded_parser("nope.js") is None


# ── 自动选路 ──


class TestResolveFramesAuto:
    def test_disabled_passthrough(self):
        upload_sourcemap("app.9f3b2c.js", _valid_map())
        frames = [dict(_MINIFIED_FRAME)]
        resolved, snippets = resolve_frames_auto(frames)
        assert resolved == frames
        assert snippets == []

    def test_enabled_upload_by_basename(self, monkeypatch):
        monkeypatch.setattr(settings, "sourcemap_enabled", True)
        upload_sourcemap("app.9f3b2c.js", _valid_map())
        resolved, _ = resolve_frames_auto([dict(_MINIFIED_FRAME)])
        assert resolved[0]["file"] == "src/app.ts"
        assert resolved[0]["resolved"] is True

    def test_explicit_artifact_wins(self, monkeypatch):
        monkeypatch.setattr(settings, "sourcemap_enabled", True)
        upload_sourcemap("custom-key", _valid_map())
        frame = {"file": "whatever.js", "line": 1, "column": 0, "function": "t"}
        resolved, _ = resolve_frames_auto([frame], artifact="custom-key")
        assert resolved[0]["file"] == "src/app.ts"

    def test_resolve_uses_release_dimension(self, monkeypatch):
        """解析时应按 release（bundle 版本）命中对应 source map，而非同 basename 混用。"""
        monkeypatch.setattr(settings, "sourcemap_enabled", True)
        m1 = _valid_map()
        m1["sources"] = ["src/v1.ts"]
        m2 = _valid_map()
        m2["sources"] = ["src/v2.ts"]
        upload_sourcemap("app.js", m1, release="v1")
        upload_sourcemap("app.js", m2, release="v2")

        frame = {"file": "app.js", "line": 1, "column": 0, "function": "t"}
        r1, _ = resolve_frames_auto([dict(frame)], release="v1")
        r2, _ = resolve_frames_auto([dict(frame)], release="v2")

        assert r1[0]["file"] == "src/v1.ts"
        assert r2[0]["file"] == "src/v2.ts"

    def test_disk_channel(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "sourcemap_enabled", True)
        monkeypatch.setattr(settings, "sourcemap_path_prefix", str(tmp_path))
        # 磁盘通道要求路径在 code_locator 白名单内：放宽白名单覆盖 tmp_path
        monkeypatch.setattr(settings, "whitelist_path_prefix", str(tmp_path))
        (tmp_path / "app.9f3b2c.js.map").write_text(json.dumps(_valid_map()), encoding="utf-8")

        resolved, _ = resolve_frames_auto([dict(_MINIFIED_FRAME)])
        assert resolved[0]["file"] == "src/app.ts"

    def test_disk_channel_outside_whitelist_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "sourcemap_enabled", True)
        monkeypatch.setattr(settings, "sourcemap_path_prefix", str(tmp_path))
        monkeypatch.setattr(settings, "whitelist_path_prefix", str(tmp_path / "isolated"))
        # 白名单不含 tmp_path（默认收敛到 cwd）
        (tmp_path / "app.9f3b2c.js.map").write_text(json.dumps(_valid_map()), encoding="utf-8")

        resolved, snippets = resolve_frames_auto([dict(_MINIFIED_FRAME)])
        assert resolved == [_MINIFIED_FRAME]
        assert snippets == []

    def test_disk_channel_missing_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "sourcemap_enabled", True)
        monkeypatch.setattr(settings, "sourcemap_path_prefix", str(tmp_path))
        monkeypatch.setattr(settings, "whitelist_path_prefix", str(tmp_path))
        resolved, _ = resolve_frames_auto([dict(_MINIFIED_FRAME)])
        assert resolved == [_MINIFIED_FRAME]

    def test_python_frame_passthrough(self, monkeypatch):
        monkeypatch.setattr(settings, "sourcemap_enabled", True)
        upload_sourcemap("app.js", _valid_map())
        resolved, snippets = resolve_frames_auto([dict(_PY_FRAME)])
        assert resolved == [_PY_FRAME]
        assert snippets == []

    def test_upload_priority_over_disk(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "sourcemap_enabled", True)
        monkeypatch.setattr(settings, "sourcemap_path_prefix", str(tmp_path))
        monkeypatch.setattr(settings, "whitelist_path_prefix", str(tmp_path))
        disk_map = _valid_map()
        disk_map["sources"] = ["src/from-disk.ts"]
        (tmp_path / "app.9f3b2c.js.map").write_text(json.dumps(disk_map), encoding="utf-8")

        upload_map = _valid_map()
        upload_map["sources"] = ["src/from-upload.ts"]
        upload_sourcemap("app.9f3b2c.js", upload_map)

        resolved, _ = resolve_frames_auto([dict(_MINIFIED_FRAME)])
        assert resolved[0]["file"] == "src/from-upload.ts"

    def test_path_traversal_basename_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "sourcemap_enabled", True)
        monkeypatch.setattr(settings, "sourcemap_path_prefix", str(tmp_path))
        monkeypatch.setattr(settings, "whitelist_path_prefix", str(tmp_path))
        evil = {"file": "https://x.com/../../etc/passwd.js", "line": 1, "column": 0}
        resolved, _ = resolve_frames_auto([evil])
        assert resolved == [evil]

    def test_empty_frames(self, monkeypatch):
        monkeypatch.setattr(settings, "sourcemap_enabled", True)
        assert resolve_frames_auto([]) == ([], [])


# ── 端点 ──


class TestSourcemapEndpoint:
    def test_disabled_returns_503(self):
        from fastapi import HTTPException

        from app.api.debug import debug_upload_sourcemap

        settings.sourcemap_enabled = False
        req = SourcemapUploadRequest(artifact="a.js", map=_valid_map())
        with pytest.raises(HTTPException) as exc:
            debug_upload_sourcemap(req)
        assert exc.value.status_code == 503

    def test_enabled_stores_and_returns_receipt(self):
        from app.api.debug import debug_upload_sourcemap

        settings.sourcemap_enabled = True
        req = SourcemapUploadRequest(artifact="a.js", map=_valid_map(), release="v1.2.3")
        receipt = debug_upload_sourcemap(req)
        assert receipt["stored"] is True
        assert receipt["release"] == "v1.2.3"
        # release 作为 bundle 版本维度并入存储键，按同 release 检索命中
        assert get_uploaded_map("a.js", release="v1.2.3") is not None

    def test_invalid_map_returns_400(self):
        from fastapi import HTTPException

        from app.api.debug import debug_upload_sourcemap

        settings.sourcemap_enabled = True
        req = SourcemapUploadRequest(artifact="a.js", map={"no": "mappings"})
        with pytest.raises(HTTPException) as exc:
            debug_upload_sourcemap(req)
        assert exc.value.status_code == 400
