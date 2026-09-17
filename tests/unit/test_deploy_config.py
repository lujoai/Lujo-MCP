"""单元测试：生产环境部署配置与文件合法性校验（仅使用标准库）"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# compose 数值环境变量（M2-DEVFIX）：这些变量若以 ${VAR:-} 形式注入空字符串，
# pydantic Settings 解析 int/float 失败 → app 容器启动即 Exited(1)。
_NUMERIC_LLM_VARS = (
    ("LLM_TIMEOUT", "llm_timeout", int),
    ("LLM_TEMPERATURE", "llm_temperature", float),
    ("LLM_MAX_RETRIES", "llm_max_retries", int),
)


def _compose_llm_defaults(compose_relpath: str) -> dict[str, str | None]:
    """提取 compose 文件中 LLM_* 数值变量的 ${VAR:-default} 默认值。

    标准库文本解析（仓库不依赖 YAML 解析库）；变量未以该形式声明时值为 None。
    """
    content = (_REPO_ROOT / compose_relpath).read_text(encoding="utf-8")
    defaults: dict[str, str | None] = {}
    for var, _field, _typ in _NUMERIC_LLM_VARS:
        m = re.search(rf"{var}:\s*\$\{{{var}:-([^}}]*)\}}", content)
        defaults[var] = m.group(1) if m else None
    return defaults


def _config_field_default(field: str) -> str | None:
    """读取 app/config.py 中 Settings 字段的字面默认值（如 "30" / "0.3"）。"""
    src = (_REPO_ROOT / "app" / "config.py").read_text(encoding="utf-8")
    m = re.search(rf"^\s*{field}:\s*\w+\s*=\s*([0-9][0-9.]*)\s*$", src, re.M)
    return m.group(1) if m else None


class TestDeployConfig:

    def test_prometheus_yaml_valid(self):
        prom_path = Path("deploy/prometheus.yml")
        assert prom_path.exists(), "deploy/prometheus.yml 必须存在"
        content = prom_path.read_text(encoding="utf-8")
        assert "scrape_configs:" in content
        assert "job_name: \"lujo-mcp\"" in content
        assert "targets: [\"app:8000\"]" in content

    def test_docker_compose_prod_valid(self):
        compose_path = Path("deploy/docker-compose.prod.yml")
        assert compose_path.exists(), "deploy/docker-compose.prod.yml 必须存在"
        content = compose_path.read_text(encoding="utf-8")
        assert "services:" in content
        assert "redis:" in content
        assert "app:" in content
        assert "prometheus:" in content
        assert "restart: unless-stopped" in content
        assert "limits:" in content
        # WP6（Step 3）：postgres 服务与 pgdata 卷已移除，不得复活
        assert "postgres" not in content, "postgres 服务应在 WP6 被移除"
        assert "pgdata" not in content, "pgdata 卷应在 WP6 被移除"
        assert "STORAGE_BACKEND: memory" in content, "app 运行时后端应为 memory"
        # Redis / Prometheus 未被误删（正向证据，设计文档 §8.5）
        assert "redisdata" in content
        assert "promdata" in content
        assert "lujo-internal-network" in content

    def test_docker_compose_dev_valid(self):
        """WP6：dev compose 只剩 redis + app（postgres/pgdata 已移除）。"""
        content = Path("docker-compose.yaml").read_text(encoding="utf-8")
        assert "redis:" in content
        assert "app:" in content
        assert "postgres" not in content
        assert "pgdata" not in content
        assert "STORAGE_BACKEND: memory" in content

    def test_env_production_example_keys(self):
        env_example = Path("deploy/env.production.example").read_text(encoding="utf-8")
        required_keys = ["API_KEY", "LLM_MODEL", "PROMETHEUS_PORT"]
        for k in required_keys:
            assert f"{k}=" in env_example, f"缺少关键配置项: {k}"
        # WP6：PG 配置键已删除，不得再出现在生产模板中
        for legacy in ("PG_PASSWORD=", "POSTGRES_PASSWORD=", "PG_DATABASE=", "PG_USER="):
            assert legacy not in env_example, f"{legacy} 应在 WP6 被移除"

    # ------------------------------------------------------------------
    # M2-DEVFIX：dev compose 数值 LLM 默认值不得为空字符串
    # ------------------------------------------------------------------

    def test_dev_numeric_llm_defaults_present_and_nonempty(self):
        """dev compose 三个数值变量必须带非空默认值（旧实现 ${VAR:-} 先红）。"""
        defaults = _compose_llm_defaults("docker-compose.yaml")
        for var, _field, _typ in _NUMERIC_LLM_VARS:
            raw = defaults[var]
            assert raw is not None, f"dev compose 缺少 {var}: ${{{var}:-default}} 形式声明"
            assert raw.strip() != "", (
                f"dev compose {var} 默认值为空字符串——容器内 pydantic 无法解析，"
                f"app 将启动即 Exited(1)（M2 动态验收已复现）"
            )

    def test_dev_numeric_llm_defaults_parse_and_match_config_authority(self):
        """默认值可分别解析为 int/float/int，且与 app/config.py 权威默认一致。"""
        defaults = _compose_llm_defaults("docker-compose.yaml")
        for var, field, typ in _NUMERIC_LLM_VARS:
            raw = defaults[var]
            authority_raw = _config_field_default(field)
            assert authority_raw is not None, f"app/config.py 缺少 {field} 字面默认值"
            authority = typ(authority_raw)
            assert typ(raw) == authority, (
                f"dev compose {var} 默认 {raw!r} 与权威口径 app/config.py.{field}="
                f"{authority_raw!r} 不一致（须以源码默认值为准）"
            )

    def test_prod_numeric_llm_defaults_keep_nonempty(self):
        """prod compose 三个数值默认非空可解析且未被改坏（锁定现状 30/0.2/3）。

        已知既有不一致：prod 的 LLM_TEMPERATURE 默认 0.2 与 config.py 权威
        默认 0.3 不同——该不一致先于 M2-DEVFIX 存在，prod 修复不在本轮范围
        （本轮仅修 dev），此处只锁定 prod 默认值存在、非空、可解析。
        """
        defaults = _compose_llm_defaults("deploy/docker-compose.prod.yml")
        assert int(defaults["LLM_TIMEOUT"]) == 30
        assert float(defaults["LLM_TEMPERATURE"]) == 0.2
        assert int(defaults["LLM_MAX_RETRIES"]) == 3
