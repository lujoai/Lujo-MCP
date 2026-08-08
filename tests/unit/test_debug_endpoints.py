"""P0-1 回归：debug.py 端点不因缺失 import time 而必然 500。

历史背景：list_sessions / debug_health 调用 time.time()，但文件顶部曾无
`import time`，两个端点必然 500。本测试直接调用端点函数证明 import 存在。
"""
from unittest.mock import patch


def test_debug_health_returns_timestamp():
    from app.api.debug import debug_health

    result = debug_health()
    assert result["status"] == "ok"
    assert isinstance(result["timestamp"], float)


def test_list_sessions_returns_ok():
    from app.api.debug import list_sessions

    with patch("app.api.debug.session_manager.list_active", return_value=[]):
        result = list_sessions()

    assert result["count"] == 0
    assert result["sessions"] == []


def test_list_sessions_uses_time_for_idle_seconds():
    """list_sessions 的 idle_seconds 计算依赖 time.time()，证明 import 有效。"""
    from app.api.debug import list_sessions

    fake_session = {
        "session_id": "s1",
        "created_at": 100.0,
        "last_active": 100.0,
        "metadata": {},
    }
    with patch("app.api.debug.session_manager.list_active", return_value=[fake_session]):
        result = list_sessions()

    assert result["count"] == 1
    idle = result["sessions"][0]["idle_seconds"]
    assert isinstance(idle, float)
