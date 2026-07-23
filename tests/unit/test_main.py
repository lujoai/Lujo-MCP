"""单元测试：应用启动安全校验"""
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
