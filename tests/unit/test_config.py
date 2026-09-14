"""单元测试：app/config.py Settings 类

覆盖 P1 M9：.env 含未知键时 Settings() 不崩 + 启动日志 warning。
"""

import logging
from pathlib import Path



class TestSettingsExtraEnvKeys:
    """M9：.env 含未声明键时 Settings() 不崩溃，且打印 warning。"""

    def test_settings_with_extra_env_keys_does_not_crash(self, tmp_path, monkeypatch):
        """额外 .env 键 → Settings() 不崩"""
        # 创建含额外键的 .env
        env_content = (
            "LLM_PROVIDER=openai\n"
            "UNKNOWN_KEY=some_value\n"
            "ANOTHER_EXTRA=123\n"
        )
        env_file = tmp_path / ".env"
        env_file.write_text(env_content)

        from pydantic import ConfigDict
        from pydantic_settings import BaseSettings

        class _TestSettings(BaseSettings):
            model_config = ConfigDict(
                extra="ignore",
                env_file=str(env_file),
                env_file_encoding="utf-8",
            )
            llm_provider: str = "openai"

        # 不应抛异常
        settings = _TestSettings()
        assert settings.llm_provider == "openai"

    def test_settings_ignores_legacy_pg_env_keys(self, tmp_path, monkeypatch):
        """WP6：PG 配置族已删除。.env 遗留的 PG_*/POSTGRES_*/DATABASE_URL 键
        经 extra="ignore" 被忽略：构造不崩、已知键正常加载、不产生同名属性。"""
        env_content = (
            "LLM_PROVIDER=openai\n"
            "PG_HOST=myhost\n"
            "PG_PORT=5433\n"
            "PG_PASSWORD=secret\n"
            "PG_ASYNC_ENABLED=true\n"
            "STORAGE_FALLBACK_TO_MEMORY=false\n"
            "CB_PG_MAX_FAILURES=9\n"
            "POSTGRES_PASSWORD=secret\n"
            "DATABASE_URL=postgresql://x\n"
        )
        env_file = tmp_path / ".env"
        env_file.write_text(env_content, encoding="utf-8")

        from app.config import Settings

        obj = Settings(_env_file=str(env_file))
        assert obj.llm_provider == "openai"
        for removed in ("pg_host", "pg_port", "pg_password", "pg_async_enabled",
                        "storage_fallback_to_memory", "cb_pg_max_failures"):
            assert not hasattr(obj, removed), f"{removed} 应在 WP6 被删除"

    def test_settings_warning_logged_for_extra_keys(self, tmp_path, monkeypatch, caplog):
        """额外键存在时，logger.warning 输出忽略的键名"""
        env_content = (
            "LLM_PROVIDER=openai\n"
            "FAKE_SECRET_KEY=top_secret\n"
            "ANOTHER_UNKNOWN=value\n"
        )
        env_file = tmp_path / ".env"
        env_file.write_text(env_content)

        from pydantic import ConfigDict
        from pydantic_settings import BaseSettings

        logger = logging.getLogger(__name__)

        class _TestSettings(BaseSettings):
            model_config = ConfigDict(
                extra="ignore",
                env_file=str(env_file),
                env_file_encoding="utf-8",
            )
            llm_provider: str = "openai"

            def model_post_init(self, __context: object) -> None:
                from dotenv import dotenv_values

                dotenv_values_map = dotenv_values(str(env_file))
                known_lower = {k.lower() for k in type(self).model_fields.keys()}
                extra_keys = {k for k in dotenv_values_map.keys() if k.lower() not in known_lower}
                if extra_keys:
                    logger.warning("Ignored extra .env keys: %s", sorted(extra_keys))

        with caplog.at_level(logging.WARNING):
            _TestSettings()

        assert any("Ignored extra .env keys" in record.message for record in caplog.records)
        warning_messages = [r.message for r in caplog.records if "Ignored extra .env keys" in r.message]
        assert len(warning_messages) >= 1
        # 应包含 FAKE_SECRET_KEY 和 ANOTHER_UNKNOWN
        combined = " ".join(warning_messages)
        assert "FAKE_SECRET_KEY" in combined
        assert "ANOTHER_UNKNOWN" in combined

    def test_settings_no_warning_when_no_extra_keys(self, tmp_path, monkeypatch, caplog):
        """无额外键时，不产生 warning"""
        env_content = "LLM_PROVIDER=zhipu\n"
        env_file = tmp_path / ".env"
        env_file.write_text(env_content)

        from pydantic import ConfigDict
        from pydantic_settings import BaseSettings

        logger = logging.getLogger(__name__)

        class _TestSettings(BaseSettings):
            model_config = ConfigDict(
                extra="ignore",
                env_file=str(env_file),
                env_file_encoding="utf-8",
            )
            llm_provider: str = "openai"

            def model_post_init(self, __context: object) -> None:
                from dotenv import dotenv_values

                dotenv_values_map = dotenv_values(str(env_file))
                known_lower = {k.lower() for k in type(self).model_fields.keys()}
                extra_keys = {k for k in dotenv_values_map.keys() if k.lower() not in known_lower}
                if extra_keys:
                    logger.warning("Ignored extra .env keys: %s", sorted(extra_keys))

        with caplog.at_level(logging.WARNING):
            _TestSettings()

        warning_messages = [r.message for r in caplog.records if "Ignored extra .env keys" in r.message]
        assert len(warning_messages) == 0

    def test_settings_missing_env_file_handled_gracefully(self, tmp_path, monkeypatch, caplog):
        """不存在 .env 文件时，Settings() 不崩"""
        env_file = tmp_path / ".env_nonexistent"
        # 不创建文件

        from pydantic import ConfigDict
        from pydantic_settings import BaseSettings

        logger = logging.getLogger(__name__)

        class _TestSettings(BaseSettings):
            model_config = ConfigDict(
                extra="ignore",
                env_file=str(env_file),
                env_file_encoding="utf-8",
            )
            llm_provider: str = "openai"

            def model_post_init(self, __context: object) -> None:
                from dotenv import dotenv_values

                # dotenv_values 对不存在的文件返回 {}，不会崩
                dotenv_values_map = dotenv_values(str(env_file))
                known_lower = {k.lower() for k in type(self).model_fields.keys()}
                extra_keys = {k for k in dotenv_values_map.keys() if k.lower() not in known_lower}
                if extra_keys:
                    logger.warning("Ignored extra .env keys: %s", sorted(extra_keys))

        with caplog.at_level(logging.WARNING):
            settings = _TestSettings()

        assert settings.llm_provider == "openai"
        warning_messages = [r.message for r in caplog.records if "Ignored extra .env keys" in r.message]
        assert len(warning_messages) == 0


class TestApiKeyNormalization:
    """M7：空串/纯空白 API_KEY 归一化为 None，避免"开而无锁"。"""

    @staticmethod
    def _build_settings_class(env_file: Path):
        """构造一个含 api_key 字段 + 与 config.py 一致 model_post_init 的 _TestSettings。"""
        from typing import Optional
        from pydantic import ConfigDict
        from pydantic_settings import BaseSettings

        logger = logging.getLogger("app.config")

        class _TestSettings(BaseSettings):
            model_config = ConfigDict(
                extra="ignore",
                env_file=str(env_file),
                env_file_encoding="utf-8",
            )
            api_key: Optional[str] = None

            def model_post_init(self, __context: object) -> None:
                from dotenv import dotenv_values

                dotenv_values_map = dotenv_values(str(env_file))
                known_lower = {k.lower() for k in type(self).model_fields.keys()}
                extra_keys = {k for k in dotenv_values_map.keys() if k.lower() not in known_lower}
                if extra_keys:
                    logger.warning("Ignored extra .env keys: %s", sorted(extra_keys))

                # M7: 空串/纯空白 API_KEY 视为未配置，归一化为 None，避免"开而无锁"
                if self.api_key is not None and not self.api_key.strip():
                    logger.warning("API_KEY 为空，已视为未配置，鉴权关闭")
                    self.api_key = None

        return _TestSettings

    def test_empty_api_key_normalized_to_none(self, tmp_path, monkeypatch, caplog):
        """.env 写 API_KEY=（空）→ api_key 归一化为 None，且产生 warning"""
        monkeypatch.delenv("API_KEY", raising=False)  # 隔离 conftest 全局 API_KEY，确保只读自身 .env
        env_file = tmp_path / ".env"
        env_file.write_text("API_KEY=\n")

        _TestSettings = self._build_settings_class(env_file)

        with caplog.at_level(logging.WARNING, logger="app.config"):
            settings = _TestSettings()

        assert settings.api_key is None
        warning_messages = [r.message for r in caplog.records if "API_KEY 为空" in r.message or "鉴权关闭" in r.message]
        assert len(warning_messages) >= 1

    def test_whitespace_api_key_normalized_to_none(self, tmp_path, monkeypatch, caplog):
        """.env 写 API_KEY=   （纯空白）→ api_key 归一化为 None"""
        monkeypatch.delenv("API_KEY", raising=False)  # 隔离 conftest 全局 API_KEY，确保只读自身 .env
        env_file = tmp_path / ".env"
        env_file.write_text("API_KEY=   \n")

        _TestSettings = self._build_settings_class(env_file)

        with caplog.at_level(logging.WARNING, logger="app.config"):
            settings = _TestSettings()

        assert settings.api_key is None

    def test_nonempty_api_key_preserved(self, tmp_path, monkeypatch, caplog):
        """.env 写 API_KEY=real-secret → api_key 保留原值，无归一化 warning"""
        monkeypatch.delenv("API_KEY", raising=False)  # 隔离 conftest 全局 API_KEY，确保只读自身 .env
        env_file = tmp_path / ".env"
        env_file.write_text("API_KEY=real-secret\n")

        _TestSettings = self._build_settings_class(env_file)

        with caplog.at_level(logging.WARNING, logger="app.config"):
            settings = _TestSettings()

        assert settings.api_key == "real-secret"
        warning_messages = [r.message for r in caplog.records if "API_KEY 为空" in r.message or "鉴权关闭" in r.message]
        assert len(warning_messages) == 0
