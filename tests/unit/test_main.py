"""单元测试：应用启动安全校验"""
import logging

import pytest

from app.config import settings
from app.main import validate_startup_configuration


def test_validate_startup_configuration_rejects_exposed_bind_without_api_key(monkeypatch):
    # 隔离 .env 含 API_KEY=test_secret_key_456 的污染
    monkeypatch.setattr(settings, "api_key", None)
    with pytest.raises(RuntimeError, match="0.0.0.0"):
        validate_startup_configuration(host="0.0.0.0", api_key=None)


def test_validate_startup_configuration_allows_local_bind_without_api_key(monkeypatch):
    # 隔离 .env 污染，确保测试语义为"无 API_KEY"场景
    monkeypatch.setattr(settings, "api_key", None)
    validate_startup_configuration(host="127.0.0.1", api_key=None)


def test_validate_startup_configuration_allows_exposed_bind_with_api_key():
    # 显式传 api_key="secret"，函数不读 settings，无需 monkeypatch
    validate_startup_configuration(host="0.0.0.0", api_key="secret")


# S3-4: 非 loopback 绑定 + 无鉴权 → WARNING（不阻断启动）
def test_validate_warns_on_non_loopback_bind_without_api_key(monkeypatch, caplog):
    # 直接隔离 auth_enabled()，避免 .env 的 API_KEY/API_KEYS 污染
    monkeypatch.setattr("app.main.auth_enabled", lambda: False)
    with caplog.at_level(logging.WARNING):
        validate_startup_configuration(host="192.168.1.10", api_key=None)
    assert "192.168.1.10" in caplog.text
    assert "API_KEY" in caplog.text


def test_validate_no_warn_on_loopback_bind_without_api_key(monkeypatch, caplog):
    monkeypatch.setattr("app.main.auth_enabled", lambda: False)
    with caplog.at_level(logging.WARNING):
        validate_startup_configuration(host="127.0.0.1", api_key=None)
        validate_startup_configuration(host="localhost", api_key=None)
    assert "非回环" not in caplog.text


# R7-A1: 绑定检测用 ipaddress 语义，不用子串匹配
def test_validate_rejects_ipv6_unspecified_without_api_key(monkeypatch):
    """IPv6 通配 ``::`` 等价全网监听：无鉴权时必须硬拒绝（旧子串匹配漏掉）。"""
    monkeypatch.setattr(settings, "api_key", None)
    with pytest.raises(RuntimeError):
        validate_startup_configuration(host="::", api_key=None)


def test_validate_not_misled_by_address_containing_zero_subnet(monkeypatch, caplog):
    """合法地址 10.0.0.0 / 100.0.0.0 含 "0.0.0.0" 子串：不再被误杀成硬拒绝，
    走"非回环 + 无鉴权"WARNING 路径。"""
    monkeypatch.setattr("app.main.auth_enabled", lambda: False)
    with caplog.at_level(logging.WARNING):
        validate_startup_configuration(host="10.0.0.0", api_key=None)
        validate_startup_configuration(host="100.0.0.0", api_key=None)
    assert "10.0.0.0" in caplog.text
    assert "100.0.0.0" in caplog.text


def test_validate_hostname_bind_warns_without_api_key(monkeypatch, caplog):
    """无法解析为主机名/地址绑定（如自定义域名）：保留非回环 warning 路径。"""
    monkeypatch.setattr("app.main.auth_enabled", lambda: False)
    with caplog.at_level(logging.WARNING):
        validate_startup_configuration(host="debug.example.com", api_key=None)
    assert "debug.example.com" in caplog.text


def test_validate_no_warn_on_non_loopback_bind_with_api_key(caplog):
    with caplog.at_level(logging.WARNING):
        validate_startup_configuration(host="192.168.1.10", api_key="secret")
    assert "非回环" not in caplog.text


# ---------------------------------------------------------------------------
# P3-13: /internal/health 反代部署下不得信任 client.host 私网判定
# ---------------------------------------------------------------------------

def test_internal_health_with_forwarded_header_requires_key(monkeypatch):
    """反代场景：client.host 为私网 IP（旧逻辑放行）但携带转发头 → fail-closed 403。"""
    from types import SimpleNamespace

    from app.auth import key_rotation
    from app.main import internal_health

    # 无鉴权配置（隔离 .env 的 API_KEY 污染）
    monkeypatch.setattr(key_rotation, "get_valid_keys", list)

    class _FakeRequest:
        client = SimpleNamespace(host="192.168.1.10")
        headers = {"X-Forwarded-For": "203.0.113.5"}

    resp = internal_health(_FakeRequest)
    assert resp.status_code == 403


def test_internal_health_forwarded_with_valid_key_allowed(monkeypatch):
    """反代场景 + 有效 API Key → 放行（不再信任私网判定但 key 校验通过）。"""
    from types import SimpleNamespace

    from app.auth import key_rotation
    from app.main import internal_health

    monkeypatch.setattr(key_rotation, "get_valid_keys", lambda: ["secret-key"])

    class _FakeRequest:
        client = SimpleNamespace(host="192.168.1.10")
        headers = {"X-Real-IP": "203.0.113.5", "X-API-Key": "secret-key"}

    resp = internal_health(_FakeRequest)
    assert isinstance(resp, dict)
    assert resp["status"] in ("ok", "degraded", "unhealthy")
