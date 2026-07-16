"""auto_test 演示 —— 直接调用，不走 MCP 协议"""
import json, threading, os, sys, time, asyncio

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "app", "web")
TEST_PORT = 8888

# 1. 启动临时 HTTP 托管测试页面
def serve_test():
    os.chdir(WEB_DIR)
    os.system(f"{sys.executable} -m http.server {TEST_PORT} --bind 127.0.0.1")

t = threading.Thread(target=serve_test, daemon=True)
t.start()
time.sleep(1)

# 2. 直接调用 auto_test
sys.path.insert(0, HERE)
os.environ["PYTHONPATH"] = HERE
from app.mcp.tools.auto_test_api import auto_test_handler

print("=" * 50)
print("  auto_test ▸ Playwright 自动遍历演示")
print("=" * 50)
url = f"http://127.0.0.1:{TEST_PORT}/auto_test_demo.html"
print(f"  目标: {url}")
print()

result = asyncio.run(auto_test_handler({"url": url, "max_actions": 10}))

print(f"  ⏺ 发现 {result.get('found_elements', 0)} 个可交互元素")
print(f"  ▶️ 执行 {result.get('executed_count', 0)} 个")
print(f"  ⏭ 跳过 {result.get('skipped_count', 0)} 个")
print(f"  🚫 控制台错误 {len(result.get('console_errors', []))} 个")
print(f"  🌐 网络错误 {len(result.get('network_errors', []))} 个")
print(f"  ⚠️  静默失败: {result.get('silent_failure_detected', False)}")
print()
print("  ── 执行详情 ──")
for ex in result.get("executed", []):
    tag = ex.get("tag", "?")
    txt = ex.get("text", "")[:20]
    err = ex.get("error", "")
    if err:
        short = err.split("(")[0][:50]
        print(f"  ✗ [{tag}] {txt} → {short}")
    else:
        changed = " ↪ 跳转" if ex.get("changed_url") else ""
        print(f"  ✓ [{tag}] {txt}{changed}")
print()
print("  ✅ 演示完成")
