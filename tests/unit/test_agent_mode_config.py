"""AgentMode 配置与调度解析单元测试。"""

import pytest

from app.config import AgentMode, Settings


def test_agent_mode_defaults_to_off():
    s = Settings()
    assert s.get_agent_mode() == AgentMode.OFF
    assert s.is_agent_active is False


def test_agent_mode_explicit_values():
    s_single = Settings(agent_mode="single")
    assert s_single.get_agent_mode() == AgentMode.SINGLE
    assert s_single.is_agent_active is True

    s_dag = Settings(agent_mode="dag")
    assert s_dag.get_agent_mode() == AgentMode.DAG
    assert s_dag.is_agent_active is True

    s_loop = Settings(agent_mode="verify_loop")
    assert s_loop.get_agent_mode() == AgentMode.VERIFY_LOOP
    assert s_loop.is_agent_active is True

    s_off = Settings(agent_mode="off")
    assert s_off.get_agent_mode() == AgentMode.OFF
    assert s_off.is_agent_active is False


def test_agent_mode_case_and_whitespace_insensitivity():
    s = Settings(agent_mode="  DAG  ")
    assert s.get_agent_mode() == AgentMode.DAG


def test_agent_mode_backward_compatibility_fallback():
    # 1. 只有 agent_enabled
    s1 = Settings(agent_mode="off", agent_enabled=True)
    assert s1.get_agent_mode() == AgentMode.SINGLE

    # 2. agent_multi_agent_enabled
    s2 = Settings(agent_mode="off", agent_enabled=True, agent_multi_agent_enabled=True)
    assert s2.get_agent_mode() == AgentMode.DAG

    # 3. agent_verify_loop_enabled
    s3 = Settings(
        agent_mode="off",
        agent_enabled=True,
        agent_multi_agent_enabled=True,
        agent_verify_loop_enabled=True,
    )
    assert s3.get_agent_mode() == AgentMode.VERIFY_LOOP


def test_explicit_agent_mode_overrides_boolean_flags():
    # 即使老开关开启，显式指定的 agent_mode 优先生效
    s = Settings(
        agent_mode="single",
        agent_multi_agent_enabled=True,
        agent_verify_loop_enabled=True,
    )
    assert s.get_agent_mode() == AgentMode.SINGLE
