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


def test_validate_no_warn_on_non_loopback_bind_with_api_key(caplog):
    with caplog.at_level(logging.WARNING):
        validate_startup_configuration(host="192.168.1.10", api_key="secret")
    assert "非回环" not in caplog.text
