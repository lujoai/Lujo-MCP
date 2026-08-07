"""单元测试：PG 连接异常脱敏（N4-FU-3）。

验证 sanitize_pg_error 的：
- 分类正确性（凭据 / 数据库不存在 / 超时 / 不可达 / 未知）
- 脱敏保证：返回值绝不含 host/port/user/dbname/password 等 DSN 细节
- 不依赖 redaction_enabled 开关（安全关键路径始终开启）
"""

import pytest

from app.runtime.core.storage._pg_errors import sanitize_pg_error


def _dsn_leak_variants():
    """模拟 psycopg2 / asyncpg 可能出现在异常文本中的敏感片段。"""
    return [
        'password authentication failed for user "postgres"',
        'could not connect to server on host "10.0.0.5" (10.0.0.5), port 5432',
        'connection to server at "db.prod.internal", port 5432 failed',
        'database "app_prod" does not exist',
        'FATAL: role "admin" does not exist',
        'psycopg2.OperationalError: could not translate host name "db.internal" to address',
        'timeout expired while trying to connect to host "psql.local"',
    ]


class TestSanitizeClassification:
    def test_credentials_error(self):
        msg = sanitize_pg_error(RuntimeError('password authentication failed for user "postgres"'))
        assert "凭据错误" in msg

    def test_database_not_exist(self):
        msg = sanitize_pg_error(RuntimeError('database "app_prod" does not exist'))
        assert "数据库不存在" in msg

    def test_timeout(self):
        msg = sanitize_pg_error(RuntimeError('timeout expired while connecting to host "psql.local"'))
        assert "连接超时" in msg

    def test_unreachable(self):
        for text in (
            "could not connect to server on host",
            "connection refused",
            "network is unreachable",
            "failed to make a connection",
        ):
            msg = sanitize_pg_error(RuntimeError(text))
            assert "无法连接数据库" in msg, text

    def test_unknown_fallback(self):
        msg = sanitize_pg_error(RuntimeError("some unexpected catastrophic failure detail"))
        assert "未知错误" in msg


class TestNoDsnLeak:
    """脱敏保证：返回值绝不含 DSN 敏感参数。"""

    @pytest.mark.parametrize("raw", _dsn_leak_variants())
    def test_no_dsn_detail_in_message(self, raw):
        msg = sanitize_pg_error(RuntimeError(raw))
        # 返回值是固定语气的安全摘要，不应包含输入异常的任何原文
        assert raw not in msg
        assert "postgres" not in msg.lower().replace("postgresql", "")
        assert "5432" not in msg
        assert "app_prod" not in msg
        assert "admin" not in msg.lower().replace("administrator", "")

    def test_password_never_present(self):
        raw = 'password authentication failed for user "postgres"'
        msg = sanitize_pg_error(RuntimeError(raw))
        assert "password" not in msg.lower()
        assert "postgres" not in msg.lower().replace("postgresql", "")


class TestAlwaysOn:
    def test_not_dependent_on_redaction_enabled(self, monkeypatch):
        """即使红action 全局关闭，PG 错误脱敏仍生效。"""
        monkeypatch.setattr("app.config.settings.redaction_enabled", False)
        msg = sanitize_pg_error(RuntimeError('password authentication failed for user "postgres"'))
        assert "凭据错误" in msg
        assert "password" not in msg.lower()