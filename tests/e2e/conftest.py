import threading
import time
import urllib.request
import pytest
import uvicorn
from app.main import app


@pytest.fixture(scope="session", autouse=True)
def e2e_server():
    """Ensure a local test server is running for e2e tests."""
    url = "http://127.0.0.1:8000/demo"
    # Check if server is already running
    try:
        with urllib.request.urlopen(url, timeout=1) as resp:
            if resp.status == 200:
                yield
                return
    except Exception:
        pass

    config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to start
    started = False
    for _ in range(30):
        time.sleep(0.1)
        try:
            with urllib.request.urlopen(url, timeout=0.5) as resp:
                if resp.status == 200:
                    started = True
                    break
        except Exception:
            pass

    if not started:
        raise RuntimeError("Failed to start e2e test uvicorn server")

    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=3)
