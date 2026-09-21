"""Browser SDK V5 传输增强 E2E 测试（真断言版）。

W5 重构（P1-TEST-3/4/5、P2-TEST-2、P3-TEST-2）：

- 节流控制（P1-TEST-3）：断言一个节流窗口内至多 maxBatchesPerWindow 批立即
  发出，超限批次被延迟到窗口结束后投递且不丢失（旧实现只 print 计数）；
- localStorage 失败降级（P1-TEST-4）：断言重试耗尽后批次真实写回
  localStorage（结构 + marker 命中），页面重新初始化后被消费并真实重发到
  服务端（旧实现只 print“可能存了也可能没存”）；
- 压缩（P1-TEST-5）：断言 SDK 侧真实发出的压缩请求——Content-Encoding: gzip
  请求头 + gzip 魔数字节 + 服务端 200 接受；压缩率按 SDK 实际发出的请求字节
  计算。旧实现用 Python gzip.compress 自测压缩率，从未触及 SDK 的压缩路径
  （SDK 实现见 browser-sdk/ai-debug.js 的 _sendBatchWithCompression /
  _sendBatchXhrCompressed：CompressionStream("gzip")）；
- 压缩阈值（P1-TEST-5 同一主题）：低于阈值不压缩、高于阈值自动压缩；
- 所有等待均为条件轮询（P2-TEST-2），范式参照 test_sdk_full_chain.py。

服务器由 tests/e2e/conftest.py 自动复用（身份校验通过）或自起 Lujo memory
实例（W1：P0-TEST-1 / P2-TEST-3），并把本模块的 BASE_URL 重写为实际地址，
无需手工启动 uvicorn。

运行方式：
    python -m pytest tests/e2e/test_sdk_v5_enhancements.py

前置条件：
    - Playwright 已安装：pip install playwright && playwright install chromium
"""
import gzip
import json
import time
import uuid

import pytest
from playwright.sync_api import sync_playwright, Page, Browser

BASE_URL = "http://127.0.0.1:8000"

# 失败降级用的“死端口”endpoint（本地未监听 → 连接拒绝；同类先例见
# test_sdk_full_chain.py 的 127.0.0.1:59999 与 demo 页 probeNetworkError）
_DEAD_ENDPOINT = "http://127.0.0.1:59998"


@pytest.fixture(scope="module")
def browser():
    """启动浏览器实例"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture(scope="module")
def page(browser: Browser):
    """创建页面"""
    page = browser.new_page()
    yield page
    page.close()


# ── 断言辅助（独立成函数：W5 红/绿自检对人为构造的不满足值逐一验证会抛 AssertionError） ──


def _assert_compression_config(cfg: dict) -> int:
    """压缩传输配置健全性；返回实际阈值（自适应构造测试载荷）。"""
    assert isinstance(cfg, dict), f"SDK 配置应为对象，实际 {type(cfg).__name__}"
    assert cfg.get("enableCompression") is True, f"enableCompression 应默认开启（V5 契约）: {cfg!r}"
    threshold = cfg.get("compressionThreshold")
    assert isinstance(threshold, int) and threshold > 0, f"compressionThreshold 应为正整数: {threshold!r}"
    return threshold


def _assert_throttle_config(cfg: dict) -> tuple[int, int]:
    """节流配置健全性；返回 (窗口毫秒, 窗口内批次数上限)。"""
    assert isinstance(cfg, dict), f"SDK 配置应为对象，实际 {type(cfg).__name__}"
    window_ms = cfg.get("throttleWindowMs")
    limit = cfg.get("maxBatchesPerWindow")
    assert isinstance(window_ms, int) and window_ms > 0, f"throttleWindowMs 应为正整数: {window_ms!r}"
    assert isinstance(limit, int) and limit >= 1, f"maxBatchesPerWindow 应 ≥ 1: {limit!r}"
    return window_ms, limit


def _assert_fallback_config(cfg: dict) -> str:
    """失败降级配置健全性；返回 localStorage 键名。"""
    assert isinstance(cfg, dict), f"SDK 配置应为对象，实际 {type(cfg).__name__}"
    assert cfg.get("enableLocalStorageFallback") is True, (
        f"enableLocalStorageFallback 应默认开启（V5 契约）: {cfg!r}"
    )
    key = cfg.get("localStorageKey")
    assert isinstance(key, str) and key, f"localStorageKey 应为非空字符串: {key!r}"
    assert isinstance(cfg.get("maxPendingBatches"), int) and cfg["maxPendingBatches"] >= 1, (
        f"maxPendingBatches 应 ≥ 1: {cfg.get('maxPendingBatches')!r}"
    )
    return key


def _assert_request_uncompressed(headers: dict, body) -> bytes:
    encoding = (headers.get("content-encoding") or "").lower()
    assert encoding == "", f"低于阈值的批次应以未压缩形态发送，实际 Content-Encoding: {encoding!r}"
    assert body, "请求体不应为空"
    return bytes(body)


def _assert_request_compressed(headers: dict, body) -> bytes:
    encoding = (headers.get("content-encoding") or "").lower()
    assert encoding == "gzip", (
        f"SDK 超过压缩阈值的请求应携带 Content-Encoding: gzip（SDK 侧压缩），实际 {encoding!r}"
    )
    assert body, "压缩请求体不应为空"
    raw = bytes(body)
    assert raw[:2] == b"\x1f\x8b", f"请求体应为 gzip 流（魔数 \\x1f\\x8b），实际 {raw[:4]!r}"
    return raw


def _assert_body_carries_marker(body: bytes, marker: str, *, where: str) -> dict:
    payload = json.loads(body.decode("utf-8"))
    assert isinstance(payload, dict) and isinstance(payload.get("events"), list) and payload["events"], (
        f"{where} 应为非空 events 批次 JSON: {body[:120]!r}"
    )
    assert marker in body.decode("utf-8", errors="replace"), f"{where} 应包含本次测试 marker {marker!r}"
    return payload


def _assert_server_accepted(status, *, phase: str) -> None:
    assert status == 200, f"{phase}请求应被服务端以 200 接受，实际 {status!r}"


def _assert_compression_effective(*, original_size: int, compressed_size: int, min_ratio_pct: float = 30.0) -> float:
    """压缩有效性（P1-TEST-5）：按 SDK 实际发出的请求字节计算压缩率。"""
    assert original_size > 0 and compressed_size > 0, (
        f"两侧体积应为正数: 原始={original_size}, 压缩后={compressed_size}"
    )
    assert compressed_size < original_size, (
        f"压缩应缩减传输体积: 原始 {original_size}B, 压缩后 {compressed_size}B"
    )
    ratio = (1 - compressed_size / original_size) * 100
    assert ratio > min_ratio_pct, (
        f"SDK gzip 压缩率应 > {min_ratio_pct:.0f}%（典型 JSON 远高于此），实测 {ratio:.1f}%"
    )
    return ratio


def _assert_window_not_exceeded(*, observed_count: int, limit: int) -> None:
    assert observed_count == limit, (
        f"一个节流窗口内应只有 {limit} 批被发送（maxBatchesPerWindow），"
        f"实际观察到 {observed_count} 批 —— 节流失效或窗口计算错误"
    )


def _assert_delayed_batch_delivered(*, total_count: int, expected: int) -> None:
    assert total_count == expected, (
        f"被节流延迟的批次应在窗口结束后仍被投递（合计 {expected}），"
        f"实际观察到 {total_count} —— 超限批次丢失"
    )


def _assert_batch_delayed_enough(*, first_ts: float, delayed_ts: float, window_s: float, min_fraction: float = 0.8) -> None:
    delay = delayed_ts - first_ts
    assert delay >= window_s * min_fraction, (
        f"延迟批次的发送时刻应不早于首批发送后 {min_fraction:.0%} 窗口"
        f"（应 ≥ {window_s * min_fraction:.2f}s，实际 {delay:.2f}s）"
    )


def _assert_pending_batches(pending, *, marker: str, key: str) -> None:
    assert isinstance(pending, list) and len(pending) >= 1, (
        f"重试耗尽后批次应写回 localStorage[{key!r}]（非空 list），实际读取到: {pending!r}"
    )
    hit = False
    for item in pending:
        if not (isinstance(item, dict) and isinstance(item.get("data"), str)):
            continue
        assert isinstance(item.get("timestamp"), (int, float)), (
            f"暂存批次应携带写回时间戳（TTL 过滤依据）: {item!r}"
        )
        try:
            payload = json.loads(item["data"])
        except ValueError:
            continue
        events = payload.get("events") if isinstance(payload, dict) else None
        if isinstance(events, list) and any(marker in json.dumps(ev, ensure_ascii=False) for ev in events):
            hit = True
    assert hit, f"暂存批次应包含本次测试 marker {marker!r}: {pending!r}"


def _assert_localstorage_drained(value) -> None:
    assert value is None, f"重放后 localStorage 键应被移除（_restorePendingBatches 消费），残留值: {value!r}"


# ── 采集与轮询基础设施（P2-TEST-2：条件轮询，禁固定长 sleep） ──


def _poll_until(page: Page, predicate, *, timeout_s: float, poll_s: float = 0.2):
    """条件轮询直至谓词为真或超时；每轮附带一次轻量 evaluate——既驱动 Playwright
    事件循环（确保 request 事件被派发到已注册的监听器），也充当心跳。
    返回最后一次谓词值（超时时为假值，由调用方断言）。"""
    deadline = time.time() + timeout_s
    value = None
    while True:
        try:
            page.evaluate("Date.now()")
        except Exception:
            pass
        value = predicate()
        if value:
            return value
        if time.time() >= deadline:
            return value
        time.sleep(poll_s)


class _BatchCapture:
    """采集页面内 SDK 真实发往 BASE_URL/ingest/batch 的 POST 请求。

    事件回调内只记录轻量字段与 request 对象引用；headers/body/response 在
    主线程惰性提取（避免在事件回调内做驱动往返调用）。"""

    def __init__(self, page: Page):
        self._page = page
        self.entries: list[dict] = []
        self._handler = self._on_request
        page.on("request", self._handler)

    def _on_request(self, request):
        try:
            url = request.url
            method = request.method
        except Exception:
            return
        if method != "POST" or not url.startswith(f"{BASE_URL}/ingest/batch"):
            return
        self.entries.append({"request": request, "ts": time.time(), "url": url})

    def close(self):
        try:
            self._page.remove_listener("request", self._handler)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def headers_of(self, entry: dict) -> dict:
        if "headers" not in entry:
            try:
                entry["headers"] = dict(entry["request"].headers)
            except Exception:
                entry["headers"] = {}
        return entry["headers"]

    def body_of(self, entry: dict):
        if "body" not in entry:
            try:
                entry["body"] = entry["request"].post_data_buffer
            except Exception:
                entry["body"] = None
        return entry["body"]

    def status_of(self, entry: dict, *, timeout_s: float = 15.0):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                resp = entry["request"].response()
            except Exception:
                resp = None
            if resp is not None:
                try:
                    return resp.status
                except Exception:
                    return None
            time.sleep(0.1)
        return None


def _gunzip_if_needed(body: bytes) -> bytes:
    return gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body


def _entry_plain_text(cap: _BatchCapture, entry: dict) -> str:
    """批次请求体的明文文本（gzip 则先解压）；供 marker 检索。"""
    body = cap.body_of(entry)
    if not body:
        return ""
    try:
        return _gunzip_if_needed(bytes(body)).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _fresh_demo_page(page: Page) -> None:
    """重置到干净的 /demo 页面。

    节流计数器 / 批次队列 / endpoint 配置均为 SDK 模块级状态，reload 即重置；
    首次 goto 可能重放此前测试遗留的 localStorage 暂存（未被采集、不计数），
    清空后再 reload 得到确定性干净状态。"""
    page.goto(f"{BASE_URL}/demo")
    page.evaluate("localStorage.clear()")
    page.reload()
    page.wait_for_load_state("networkidle")
    assert page.evaluate("typeof AiDebug !== 'undefined'"), "SDK 未加载（/demo 页缺少 AiDebug）"
    assert page.evaluate("AiDebug._inited"), "SDK 未完成初始化"


# ── 测试 ──


def test_gzip_compression_threshold(page: Page):
    """V5 压缩阈值（SDK 侧）：低于阈值不压缩发送，超过阈值真实压缩并被服务端接受。"""
    _fresh_demo_page(page)
    threshold = _assert_compression_config(page.evaluate("AiDebug._getPublicConfig()"))

    small_marker = f"e2e-v5-small-{uuid.uuid4().hex[:8]}"
    large_marker = f"e2e-v5-large-{uuid.uuid4().hex[:8]}"

    with _BatchCapture(page) as cap:
        # 小载荷：序列化后低于阈值（消息为纯 ASCII，JS 字符串长度与 UTF-8 字节数一致）
        page.evaluate(
            "(m) => { AiDebug.reportError(new Error(m)); AiDebug.flush(); }",
            small_marker,
        )
        small_entry = _poll_until(
            page,
            lambda: next((e for e in cap.entries if small_marker in _entry_plain_text(cap, e)), None),
            timeout_s=10.0,
        )
        assert small_entry is not None, "10s 内未观察到小载荷批次被发送（flush 为同步决策）"

        # 大载荷：序列化后超过阈值（阈值 + 200 字符余量）
        page.evaluate(
            "([m, pad]) => { AiDebug.reportError(new Error(m + ':' + 'x'.repeat(pad))); AiDebug.flush(); }",
            [large_marker, threshold + 200],
        )
        large_entry = _poll_until(
            page,
            lambda: next((e for e in cap.entries if large_marker in _entry_plain_text(cap, e)), None),
            timeout_s=10.0,
        )
        assert large_entry is not None, "10s 内未观察到压缩批次被发送（压缩决策同步，仅压缩流为异步）"

    small_body = _assert_request_uncompressed(cap.headers_of(small_entry), cap.body_of(small_entry))
    assert len(small_body) < threshold, (
        f"小载荷序列化后应低于阈值 {threshold}B，实际 {len(small_body)}B（测试自检失败）"
    )
    _assert_body_carries_marker(small_body, small_marker, where="未压缩批次")

    large_body = _assert_request_compressed(cap.headers_of(large_entry), cap.body_of(large_entry))
    large_plain = gzip.decompress(large_body)
    _assert_body_carries_marker(large_plain, large_marker, where="压缩批次（解压后）")
    assert len(large_plain) > threshold, (
        f"大载荷序列化后应超过阈值 {threshold}B，实际 {len(large_plain)}B（测试自检失败）"
    )
    # 服务端 200 = _bounded_gzip_decompress 解压 + 批次入库双双通过（app/api/ingest.py）
    _assert_server_accepted(cap.status_of(large_entry), phase="压缩")
    _assert_server_accepted(cap.status_of(small_entry), phase="未压缩")


def test_throttle_control(page: Page):
    """V5 节流控制：窗口内至多 maxBatchesPerWindow 批，超限批次延迟到窗口后投递且不丢失。"""
    _fresh_demo_page(page)
    window_ms, limit = _assert_throttle_config(page.evaluate("AiDebug._getPublicConfig()"))
    window_s = window_ms / 1000.0
    markers = [f"e2e-v5-throttle-{uuid.uuid4().hex[:8]}-{i}" for i in range(limit + 1)]

    # batchSize=1（测试辅助 _setConfig，SDK 官方 e2e 后门）：每次 reportError 立即触发
    # flush，同一 JS 任务内连续 limit+1 次上报 → limit+1 个发送决策：前 limit 个满足
    # 窗口配额立即发送，超限批次进入延迟队列（_pendingBatches + _drainPendingBatches）。
    page.evaluate("AiDebug._setConfig('batchSize', 1)")

    with _BatchCapture(page) as cap:
        t_eval = time.time()
        page.evaluate(
            "(ms) => ms.forEach((m) => AiDebug.reportError(new Error(m)))",
            markers,
        )

        # 前 limit 批应随即发出（flush 为同步决策）
        first_batch = _poll_until(
            page,
            lambda: cap.entries[:limit] if len(cap.entries) >= limit else None,
            timeout_s=3.0,
        )
        assert first_batch is not None, (
            f"前 {limit} 批应在触发后 3s 内立即发送，实际仅观察到 {len(cap.entries)} 批"
        )

        # 窗口 75% 处的观察点：超限批次必须尚未发出。该等待锚定在触发时刻（evaluate
        # 之前取的 t_eval，早于任何真实发送），证明“窗口内未发送”必须等待窗口推进，
        # 不是盲等固定时长；SDK 侧延迟队列的触发点是 t1 + windowMs 且 t1 ≥ t_eval，
        # 故 75% 观察点必然早于合法延迟投递时刻（留有 25% 余量吸收事件派发延迟）。
        obs_deadline = t_eval + window_s * 0.75
        while time.time() < obs_deadline:
            try:
                page.evaluate("Date.now()")
            except Exception:
                pass
            time.sleep(0.1)
        _assert_window_not_exceeded(observed_count=len(cap.entries), limit=limit)

        # 窗口结束后，超限批次应被延迟队列投递（而非丢失）
        delivered = _poll_until(
            page,
            lambda: cap.entries[limit:] if len(cap.entries) >= limit + 1 else None,
            timeout_s=window_s + 10.0,
        )
        assert delivered is not None, (
            f"被节流延迟的批次应在窗口结束后投递（{window_s:.1f}s + 余量），"
            f"合计仍只有 {len(cap.entries)} 批 —— 超限批次丢失"
        )
        _assert_delayed_batch_delivered(total_count=len(cap.entries), expected=limit + 1)
        _assert_batch_delayed_enough(
            first_ts=cap.entries[0]["ts"],
            delayed_ts=cap.entries[limit]["ts"],
            window_s=window_s,
        )

    # 延迟批次的内容确为超限的那一批（marker 校验，防止误配其它批次）
    delayed_body = _gunzip_if_needed(bytes(cap.body_of(cap.entries[limit]) or b""))
    _assert_body_carries_marker(delayed_body, markers[limit], where="延迟批次")
    _assert_server_accepted(cap.status_of(cap.entries[limit]), phase="延迟")


def test_localstorage_fallback(page: Page):
    """V5 失败降级：重试耗尽后批次真实写回 localStorage，重新初始化后被消费并真实重发。"""
    _fresh_demo_page(page)
    key = _assert_fallback_config(page.evaluate("AiDebug._getPublicConfig()"))
    marker = f"e2e-v5-fallback-{uuid.uuid4().hex[:10]}"

    with _BatchCapture(page) as cap:
        # endpoint 指向死端口：所有 XHR 尝试连接拒绝，3 次指数退避重试后写回 localStorage
        page.evaluate(
            "([ep, m]) => { AiDebug._setConfig('endpoint', ep); AiDebug.reportError(new Error(m)); }",
            [_DEAD_ENDPOINT, marker],
        )

        # 条件轮询（P2-TEST-2）：批次定时 flush（1s）+ 3 次重试退避（上限 ~3.5s）
        # 之后才写回；旧实现固定 sleep(4) 在退避抖动到达上界时会漏看
        pending = _poll_until(
            page,
            lambda: page.evaluate(
                "(k) => { try { return JSON.parse(localStorage.getItem(k) || '[]'); } catch (e) { return []; } }",
                key,
            )
            or None,
            timeout_s=25.0,
            poll_s=0.25,
        )
        assert pending is not None, (
            f"重试耗尽后 25s 内仍未见批次写回 localStorage[{key!r}] —— 失败降级未生效"
        )
        _assert_pending_batches(pending, marker=marker, key=key)

        # 服务恢复 = 页面重新初始化：reload 重跑 demo init（endpoint 自动恢复
        # window.location.origin），_restorePendingBatches 消费暂存并重发。
        # request 监听器跨导航存活，可捕获加载期间的重放发送。
        page.reload()
        page.wait_for_load_state("networkidle")

        resent = _poll_until(
            page,
            lambda: next((e for e in cap.entries if marker in _entry_plain_text(cap, e)), None),
            timeout_s=20.0,
        )
        assert resent is not None, "reload 后 20s 内未观察到暂存批次被重发到真实服务端"
        _assert_server_accepted(cap.status_of(resent), phase="重放发送")

    # 重放后 localStorage 键应被移除（暂存被消费，不会二次重放）
    drained = _poll_until(
        page,
        lambda: page.evaluate("(k) => localStorage.getItem(k) === null", key) or None,
        timeout_s=10.0,
    )
    assert drained is not None, "重放后 localStorage 键未被移除"
    _assert_localstorage_drained(page.evaluate("(k) => localStorage.getItem(k)", key))


def test_compression_ratio(page: Page):
    """V5 压缩有效性（P1-TEST-5 重写）：压缩率按 SDK 实际发出的请求字节计算。

    旧实现构造 JSON 后用 Python gzip.compress 计算压缩率——测的是 Python 标准库，
    SDK 的压缩路径（CompressionStream → Content-Encoding: gzip）从未被触及。"""
    _fresh_demo_page(page)
    threshold = _assert_compression_config(page.evaluate("AiDebug._getPublicConfig()"))
    marker = f"e2e-v5-ratio-{uuid.uuid4().hex[:10]}"

    with _BatchCapture(page) as cap:
        # 典型载荷：10 个相似但互异的错误事件（类型 + 消息 + 真实堆栈帧 + extra），
        # 每条消息带 ~600 字符典型诊断文本，整批必然超过压缩阈值
        page.evaluate(
            """(args) => {
                for (let i = 0; i < args.count; i++) {
                    AiDebug.reportError(new TypeError(
                        args.marker + '-' + i +
                        ": Cannot read properties of undefined (reading 'value')" +
                        " at processBatch (batch.js:" + (100 + i) + "): " +
                        'd'.repeat(args.pad)
                    ));
                }
                AiDebug.flush();
            }""",
            {"marker": marker, "count": 10, "pad": 600},
        )
        entry = _poll_until(
            page,
            lambda: next((e for e in cap.entries if marker in _entry_plain_text(cap, e)), None),
            timeout_s=15.0,
        )
        assert entry is not None, "15s 内未观察到包含本次 marker 的压缩批次"

    compressed = _assert_request_compressed(cap.headers_of(entry), cap.body_of(entry))
    original = gzip.decompress(compressed)
    _assert_body_carries_marker(original, marker, where="压缩批次（解压后）")
    assert len(original) > threshold, (
        f"压缩率测试载荷应超过阈值 {threshold}B，实际 {len(original)}B（测试自检失败）"
    )
    _assert_server_accepted(cap.status_of(entry), phase="压缩批次")
    _assert_compression_effective(
        original_size=len(original),
        compressed_size=len(compressed),
        min_ratio_pct=30.0,
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
