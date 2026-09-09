"""Node SDK -> real FastAPI /ingest/batch -> memory storage contract."""

import json
from pathlib import Path
import shutil
import subprocess
import urllib.parse
import urllib.request
import uuid

from app.config import settings


BASE_URL = "http://127.0.0.1:8000"
API_KEY = settings.api_key or "test_secret_key_456"


def test_node_sdk_reports_to_real_ingest_server():
    node = shutil.which("node")
    assert node, "e2e runner must provide Node.js for the Node SDK contract"

    trace_id = f"node-sdk-e2e-{uuid.uuid4().hex[:12]}"
    session_id = f"node-sdk-session-{uuid.uuid4().hex[:12]}"
    script = Path(__file__).with_name("node_sdk_report.cjs")
    completed = subprocess.run(
        [node, str(script), BASE_URL, API_KEY, trace_id, session_id],
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
