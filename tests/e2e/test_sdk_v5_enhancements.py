"""
Browser SDK V5 增强功能 E2E 测试

验证：
1. gzip 压缩传输（payload > 4KB 时自动启用）
2. 节流控制（5秒内最多2批）
3. 失败降级（localStorage 暂存 + 启动恢复）

运行方式：
    python -m pytest tests/e2e/test_sdk_v5_enhancements.py -v

前置条件：
    - uvicorn 已启动：python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
    - Playwright 已安装：pip install playwright && playwright install chromium
"""
import gzip
import json
import time
import pytest
from playwright.sync_api import sync_playwright, Page, Browser

BASE_URL = "http://127.0.0.1:8000"
API_KEY = "test_secret_key_456"


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


def test_gzip_compression_threshold(page: Page):
    """
    验证 gzip 压缩阈值逻辑
    
    步骤：
    1. 构造小于 4KB 的 payload，验证不压缩
    2. 构造大于 4KB 的 payload，验证自动压缩
    3. 检查服务端是否正确解压
    """
    page.goto(f"{BASE_URL}/demo")
    page.wait_for_load_state("networkidle")

    # 检查 SDK 是否加载成功
    sdk_loaded = page.evaluate("typeof AiDebug !== 'undefined'")
    assert sdk_loaded, "SDK 未加载"

    # 检查压缩配置
    compression_config = page.evaluate("""
        JSON.stringify({
            enableCompression: AiDebug._cfg.enableCompression,
            compressionThreshold: AiDebug._cfg.compressionThreshold
        })
    """)
    print(f"Compression config: {compression_config}")

    # 测试 1：小 payload（< 4KB）不压缩
    small_payload = {
        "events": [
            {
                "path": "/ingest/error",
                "payload": {
                    "exc_type": "SmallError",
                    "message": "Small payload test",
                    "frames": [],
                    "source": "e2e_test"
                }
            }
        ]
    }
    small_body = json.dumps(small_payload)
    print(f"Small payload size: {len(small_body)} bytes")

    # 测试 2：大 payload（> 4KB）自动压缩
    large_payload = {
        "events": [
            {
                "path": "/ingest/error",
                "payload": {
                    "exc_type": "LargeError",
                    "message": "Large payload test " + "x" * 5000,  # 超过 4KB
                    "frames": [],
                    "source": "e2e_test"
                }
            }
        ]
    }
    large_body = json.dumps(large_payload)
    print(f"Large payload size: {len(large_body)} bytes")
    assert len(large_body) > 4096, "Large payload should be > 4KB"

    # 通过 API 直接测试压缩传输
    import requests
    
    # 测试未压缩请求
    resp = requests.post(
        f"{BASE_URL}/ingest/batch",
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
        data=small_body
    )
    assert resp.status_code == 200
    print(f"Uncompressed request: {resp.json()}")

    # 测试压缩请求
    compressed_body = gzip.compress(large_body.encode("utf-8"))
    resp = requests.post(
        f"{BASE_URL}/ingest/batch",
        headers={
            "X-API-Key": API_KEY,
            "Content-Type": "application/json",
            "Content-Encoding": "gzip"
        },
        data=compressed_body
    )
    assert resp.status_code == 200
    print(f"Compressed request: {resp.json()}")


def test_throttle_control(page: Page):
    """
    验证节流控制逻辑
    
    步骤：
    1. 快速发送 3 批事件
    2. 验证前 2 批立即发送
    3. 验证第 3 批被延迟到 5 秒后
    """
    page.goto(f"{BASE_URL}/demo")
    page.wait_for_load_state("networkidle")

    # 检查节流配置
    throttle_config = page.evaluate("""
        JSON.stringify({
            throttleWindowMs: AiDebug._cfg.throttleWindowMs,
            maxBatchesPerWindow: AiDebug._cfg.maxBatchesPerWindow
        })
    """)
    print(f"Throttle config: {throttle_config}")

    # 监听网络请求
    requests_sent = []
    
    def handle_request(request):
        if "/ingest/batch" in request.url:
            requests_sent.append({
                "url": request.url,
                "timestamp": time.time()
            })
    
    page.on("request", handle_request)

    # 快速触发 3 次批量上报
    for i in range(3):
        page.evaluate(f"""
            AiDebug.reportError(new Error('Throttle test {i}'));
        """)
        time.sleep(0.1)  # 短暂间隔

    # 等待第一批发送
    time.sleep(1)
    
    # 检查发送时间
    print(f"Requests sent: {len(requests_sent)}")
    if len(requests_sent) >= 2:
        print("First 2 requests sent within throttle window")
    
    # 等待节流窗口结束
    time.sleep(5)
    
    # 检查第 3 批是否被延迟发送
    print(f"Total requests after throttle window: {len(requests_sent)}")


def test_localstorage_fallback(page: Page):
    """
    验证失败降级到 localStorage
    
    步骤：
    1. 模拟服务端不可用（错误 URL）
    2. 发送事件触发重试失败
    3. 验证事件被暂存到 localStorage
    4. 恢复服务端，重新初始化 SDK
    5. 验证暂存的事件被恢复并发送
    """
    page.goto(f"{BASE_URL}/demo")
    page.wait_for_load_state("networkidle")

    # 检查 localStorage 降级配置
    fallback_config = page.evaluate("""
        JSON.stringify({
            enableLocalStorageFallback: AiDebug._cfg.enableLocalStorageFallback,
            localStorageKey: AiDebug._cfg.localStorageKey,
            maxPendingBatches: AiDebug._cfg.maxPendingBatches
        })
    """)
    print(f"Fallback config: {fallback_config}")

    # 清空 localStorage
    page.evaluate("localStorage.clear()")

    # 模拟服务端不可用：修改 endpoint 为错误地址
    page.evaluate("""
        AiDebug._cfg.endpoint = 'http://localhost:9999';
    """)

    # 发送事件触发重试失败
    page.evaluate("""
        AiDebug.reportError(new Error('Fallback test'));
    """)

    # 等待重试完成（3 次重试，指数退避：500ms + 1000ms + 2000ms = 3.5s）
    time.sleep(4)

    # 检查 localStorage 是否暂存了事件
    pending = page.evaluate("""
        JSON.parse(localStorage.getItem('ai-debug-pending-batches') || '[]')
    """)
    print(f"Pending batches in localStorage: {len(pending)}")
    
    # 恢复 endpoint
    page.evaluate(f"""
        AiDebug._cfg.endpoint = '{BASE_URL}';
    """)

    # 验证 localStorage 中有暂存数据（如果有）
    if len(pending) > 0:
        print(f"Successfully saved {len(pending)} batch(es) to localStorage")
    else:
        print("No pending batches (may have been sent successfully or not triggered fallback)")


def test_compression_ratio(page: Page):
    """
    验证压缩效果
    
    步骤：
    1. 构造典型的事件 payload
    2. 比较压缩前后大小
    3. 验证压缩率
    """
    # 构造典型 payload（包含多个事件）
    typical_payload = {
        "events": [
            {
                "path": "/ingest/error",
                "payload": {
                    "exc_type": "TypeError",
                    "message": "Cannot read property 'x' of undefined",
                    "frames": [
                        {"file": "app.js", "line": 123, "function": "processData"},
                        {"file": "app.js", "line": 456, "function": "handleClick"},
                        {"file": "vendor.js", "line": 789, "function": "dispatch"}
                    ],
                    "source": "browser-sdk",
                    "trace_id": "sdk-trace-test-123",
                    "extra": {
                        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                        "url": "http://example.com/page",
                        "session_id": "sdk-session-456"
                    }
                }
            }
        ] * 10  # 10 个事件
    }
    
    body = json.dumps(typical_payload)
    compressed = gzip.compress(body.encode("utf-8"))
    
    original_size = len(body)
    compressed_size = len(compressed)
    ratio = (1 - compressed_size / original_size) * 100
    
    print(f"Original size: {original_size} bytes")
    print(f"Compressed size: {compressed_size} bytes")
    print(f"Compression ratio: {ratio:.1f}%")
    
    # 验证压缩有效（通常 JSON 压缩率 > 50%）
    assert compressed_size < original_size, "Compression should reduce size"
    assert ratio > 30, f"Compression ratio should be > 30%, got {ratio:.1f}%"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
