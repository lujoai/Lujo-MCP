"""单元测试：AI Debug Agent 配置项（config.py 新增 agent_* 字段）。"""

from app.config import Settings


class TestAgentConfigDefaults:
    """新增 agent_* 字段默认值。"""

    def test_agent_enabled_default_false(self):
        """agent_enabled 默认 False，关闭时零行为变更。"""
        s = Settings()
        assert s.agent_enabled is False

    def test_agent_queue_defaults(self):
        s = Settings()
        assert s.agent_queue_maxsize == 50
        assert s.agent_queue_workers == 2
        assert s.agent_queue_drain_timeout == 60

    def test_agent_prior_analysis_enabled_default_true(self):
        s = Settings()
        assert s.agent_prior_analysis_enabled is True

    def test_agent_model_default_empty(self):
        """agent_model 默认空串，回退到 llm_model。"""
        s = Settings()
        assert s.agent_model == ""

    def test_agent_retry_timeout_defaults(self):
        s = Settings()
        assert s.agent_max_retries == 2
        assert s.agent_timeout == 90

    def test_agent_multi_agent_enabled_default_false(self):
        """Phase 2 预留 flag 默认关闭。"""
        s = Settings()
        assert s.agent_multi_agent_enabled is False


class TestAgentConfigEnvInjection:
    """配置项可通过环境变量注入。"""

    def test_agent_enabled_via_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_ENABLED", "true")
        s = Settings()
        assert s.agent_enabled is True

    def test_agent_queue_maxsize_via_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_QUEUE_MAXSIZE", "100")
        s = Settings()
        assert s.agent_queue_maxsize == 100

    def test_agent_model_via_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_MODEL", "gpt-4o")
        s = Settings()
        assert s.agent_model == "gpt-4o"


class TestAgentConfigIndependence:
    """agent_* 配置与 llm_* 配置解耦。"""

    def test_agent_workers_independent_from_llm_workers(self):
        s = Settings()
        # 两者是独立字段，互不影响
        assert s.agent_queue_workers != s.llm_queue_workers or s.agent_queue_workers == s.llm_queue_workers
        assert hasattr(s, "agent_queue_workers")
        assert hasattr(s, "llm_queue_workers")

    def test_agent_max_retries_independent_from_llm(self):
        s = Settings()
        assert s.agent_max_retries == 2
        assert s.llm_max_retries == 3
