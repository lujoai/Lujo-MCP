"""单元测试：E 批 PG-1 —— STORAGE_BACKEND=postgresql 启动告警（V8-5）

规格来源：DEV_PLAN.md 附录 B §3（原 PLAN_kb-sqlite-persist.md）S2-1：
- 覆盖 HTTP / 纯 stdio / 统一模式：三者共享同一启动点（app.config Settings
  构造；app/main.py 与 app/mcp_server.py 均经 ``from app.config import
  settings`` 在导入期触发），传输差异只在日志路由。
- 每进程最多一次；stdio 仅 stderr；memory 静默；非法 backend 仍 fail-fast。
- 告警不含凭据；不把 PG 自动降级 memory。
全部用例不连接真实 PostgreSQL。
"""

import logging

import pytest

import app.config as app_config
from app.config import Settings

_PG_MARKER = "STORAGE_BACKEND=postgresql"
_WARNING_MARKER = "实验性后端"


def _reset_pg_warning_flag(monkeypatch) -> None:
    """重置进程级一次性告警标志；monkeypatch 自动还原。

    hasattr 守卫让本文件在「告警未实现」的旧代码上同样可运行——
    红灯反映的是缺失的告警行为，而不是 monkeypatch 目标不存在。
    """
    if hasattr(app_config, "_pg_backend_warning_emitted"):
        monkeypatch.setattr(app_config, "_pg_backend_warning_emitted", False)


def _fresh_pg_settings(monkeypatch, **overrides) -> Settings:
    _reset_pg_warning_flag(monkeypatch)
    return Settings(storage_backend="postgresql", **overrides)


def _pg_records(caplog) -> list:
    return [r for r in caplog.records if _WARNING_MARKER in r.getMessage()]


class TestPgBackendStartupWarning:

    def test_http_startup_path_warns_for_postgresql(self, monkeypatch, caplog):
        """HTTP 入口共享启动点（app/main.py 导入链触发 Settings 构造）产生 warning。"""
        with caplog.at_level(logging.WARNING, logger="app.config"):
            _fresh_pg_settings(monkeypatch)
        records = _pg_records(caplog)
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert records[0].name == "app.config"
        assert _PG_MARKER in records[0].getMessage()

    def test_stdio_startup_path_warning_lands_on_stderr_not_stdout(self, monkeypatch, capsys):
        """stdio 入口：生产 stdio 日志基建（_configure_stdio_logging）就位时，
        告警只能出现在 stderr，stdout 协议流保持纯净。"""
        from app.mcp.transports.stdio import _configure_stdio_logging

        _configure_stdio_logging()
        _fresh_pg_settings(monkeypatch)
        captured = capsys.readouterr()
        assert _WARNING_MARKER in captured.err
        assert _WARNING_MARKER not in captured.out

    def test_warning_emitted_only_once_per_process(self, monkeypatch, caplog):
        """同一进程重复触发启动逻辑（重复构造 Settings）只产生一次 warning。"""
        with caplog.at_level(logging.WARNING, logger="app.config"):
            _fresh_pg_settings(monkeypatch)
            Settings(storage_backend="postgresql")
            Settings(storage_backend="postgresql")
        assert len(_pg_records(caplog)) == 1

    def test_memory_backend_emits_no_pg_warning(self, monkeypatch, caplog):
        """默认 memory 后端静默：不产生任何 PG 告警。"""
        _reset_pg_warning_flag(monkeypatch)
        with caplog.at_level(logging.WARNING, logger="app.config"):
            Settings(storage_backend="memory")
        assert _pg_records(caplog) == []

    def test_invalid_backend_silent_and_still_fail_fast(self, monkeypatch, caplog):
        """非法 backend 不触发 PG 告警，且仍由 factory 校验 fail-fast（不被告警吞掉）。"""
        _reset_pg_warning_flag(monkeypatch)
        with caplog.at_level(logging.WARNING, logger="app.config"):
            Settings(storage_backend="postgrsql")  # 拼写错误
        assert _pg_records(caplog) == []

        monkeypatch.setattr(app_config.settings, "storage_backend", "postgrsql")
        from app.runtime.core.storage import factory

        with pytest.raises(ValueError, match="Invalid STORAGE_BACKEND"):
            factory._validate_backend()

    def test_warning_contains_no_credentials(self, monkeypatch, caplog):
        """告警文案不包含密码 / API key / 连接串凭据。"""
        with caplog.at_level(logging.WARNING, logger="app.config"):
            _fresh_pg_settings(
                monkeypatch,
                pg_password="super-secret-pw",
                api_key="sk-test-secret",
                redis_url="redis://:redis-pass@localhost:6379/0",
            )
        records = _pg_records(caplog)
        assert len(records) == 1
        message = records[0].getMessage()
        assert "super-secret-pw" not in message
        assert "sk-test-secret" not in message
        assert "redis-pass" not in message

    def test_warning_does_not_downgrade_backend(self, monkeypatch):
        """告警只观察不改写：storage_backend 保持 postgresql，不自动降级 memory。"""
        settings_obj = _fresh_pg_settings(monkeypatch)
        assert settings_obj.storage_backend == "postgresql"
