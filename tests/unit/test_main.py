"""单元测试：应用启动安全校验"""
import pytest

from app.main import validate_startup_configuration


def test_validate_startup_configuration_rejects_exposed_bind_without_api_key():
    with pytest.raises(RuntimeError, match="0.0.0.0"):
        validate_startup_configuration(host="0.0.0.0", api_key=None)


def test_validate_startup_configuration_allows_local_bind_without_api_key():
    validate_startup_configuration(host="127.0.0.1", api_key=None)


def test_validate_startup_configuration_allows_exposed_bind_with_api_key():
    validate_startup_configuration(host="0.0.0.0", api_key="secret")
