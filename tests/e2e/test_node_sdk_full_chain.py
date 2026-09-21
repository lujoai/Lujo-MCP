"""Node SDK -> real FastAPI /ingest/batch -> memory storage contract."""

import json
from pathlib import Path
import shutil
import subprocess
import urllib.parse
import urllib.request
import uuid

import pytest

from app.config import settings


BASE_URL = "http://127.0.0.1:8000"
API_KEY = settings.api_key or "test_secret_key_456"

# P3-TEST-4（§0.6.2 第 6 条裁定）：环境缺失门禁用带明确 reason 的 skipif 表达，
# 替代 assert node。缺失的是 PATH 中的 node 可执行文件（Node.js 运行时）——
# 此时无法执行 node_sdk_report.cjs 完成上报，后续服务端查询断言必然空手而归，
# 测试对象整体不可执行。注意边界：本 skipif 仅覆盖“运行时未安装”这一环境
# 门禁情形；把断言失败改成 skip 一律禁止。
NODE_EXECUTABLE = shutil.which("node")


@pytest.mark.skipif(
    NODE_EXECUTABLE is None,
    reason=(
        "缺少 Node.js 运行时：PATH 中找不到 node 可执行文件，无法执行 "
        "tests/e2e/node_sdk_report.cjs 向真实 ingest 服务器上报（Node SDK 逻辑"
        "另由 npm test --prefix node-sdk 覆盖，同样依赖 node 存在）"
    ),
)
def test_node_sdk_reports_to_real_ingest_server():
    trace_id = f"node-sdk-e2e-{uuid.uuid4().hex[:12]}"
    session_id = f"node-sdk-session-{uuid.uuid4().hex[:12]}"
    script = Path(__file__).with_name("node_sdk_report.cjs")
    completed = subprocess.run(
        [NODE_EXECUTABLE, str(script), BASE_URL, API_KEY, trace_id, session_id],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip())
    assert result == {"sent": 2, "failed": 0, "batches": 1, "attempts": 1}

    query = urllib.parse.urlencode({"session_id": session_id})
    request = urllib.request.Request(
        f"{BASE_URL}/ingest/network/{trace_id}?{query}",
        headers={"X-API-Key": API_KEY},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        stored = json.load(response)

    assert stored["found"] is True
    assert stored["count"] == 1
    assert stored["records"][0]["url"] == "https://service.test/node-sdk-e2e"
    assert stored["records"][0]["status_code"] == 503
