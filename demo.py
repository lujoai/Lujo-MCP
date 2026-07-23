"""面试演示脚本 —— 依次展示 health / debug / verify"""
import urllib.request
import json
BASE = "http://localhost:8000"

def req(method, path, data=None):
    body = json.dumps(data).encode() if data else None
    r = urllib.request.Request(f"{BASE}{path}", data=body, method=method)
    if body:
        r.add_header("Content-Type", "application/json")
    resp = urllib.request.urlopen(r)
    return resp.status, json.loads(resp.read().decode())

def show(title, status, body):
    print(f"\n{'='*50}")
    print(f"  {title}  (HTTP {status})")
    print(f"{'='*50}")
    if isinstance(body, dict):
        print(json.dumps(body, ensure_ascii=False, indent=2))
    else:
        print(body)

try:
    # 1. health
    s, h = req("GET", "/health")
    show("1. 健康检查", s, h)

    # 2. debug/run（核心数据流）
    s, d = req("POST", "/api/debug/run", {"payload": {"x": 1}, "metadata": {"source": "demo"}})
    show("2. 调试请求（payload→trace→context）", s, d)

    # 3. verify（静默失败检测）
    s, v = req("POST", "/api/debug/verify", {
        "actual": {"status_code": 200, "body": {"success": True}},
        "spec": {"kind": "api", "target": "POST /login",
                 "expect": {"status": 200, "body_rules": {"success": False}}}
    })
    show("3. 静默失败检测（200 但不符规范→silent_failure）", s, v)

    print("\n[OK] 演示完成")
except Exception as e:
    print(f"\n[FAIL] 错误: {e}")
    print("请确认服务已在运行：python -m app.main")
