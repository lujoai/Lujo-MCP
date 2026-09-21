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

    def test_prod_numeric_llm_defaults_match_config_authority(self):
        """prod compose 三个数值默认非空可解析，且必须与 app/config.py 权威一致。

        M2-CONFIGSYNC：prod 的 LLM_TEMPERATURE 原 0.2 经独立审查裁定为
        无设计依据的历史漂移（commit 84a296f 批量 sync 引入，出生即 0.2，
        公开 preflight/TROUBLESHOOTING 文档推荐值均为权威 0.3），对齐为 0.3。
        timeout=30 / max_retries=3 与权威一直一致，一并纳入权威对齐断言。
        """
        defaults = _compose_llm_defaults("deploy/docker-compose.prod.yml")
        for var, field, typ in _NUMERIC_LLM_VARS:
            raw = defaults[var]
            authority_raw = _config_field_default(field)
            assert authority_raw is not None, f"app/config.py 缺少 {field} 字面默认值"
            assert raw is not None, f"prod compose 缺少 {var}: ${{{var}:-default}} 形式声明"
            assert raw.strip() != "", f"prod compose {var} 默认值为空字符串（启动崩溃风险）"
            assert typ(raw) == typ(authority_raw), (
                f"prod compose {var} 默认 {raw!r} 与权威口径 app/config.py.{field}="
                f"{authority_raw!r} 不一致（历史漂移须对齐权威值）"
            )

    def test_env_production_example_temperature_matches_authority(self):
        """生产配置模板的 LLM_TEMPERATURE 必须与 app/config.py 权威默认一致（0.3）。"""
        content = (_REPO_ROOT / "deploy/env.production.example").read_text(encoding="utf-8")
        m = re.search(r"^LLM_TEMPERATURE=([^\s#]+)", content, re.M)
        assert m is not None, "env.production.example 缺少 LLM_TEMPERATURE 声明"
        authority_raw = _config_field_default("llm_temperature")
        assert authority_raw is not None, "app/config.py 缺少 llm_temperature 字面默认值"
        assert float(m.group(1)) == float(authority_raw), (
            f"env.production.example LLM_TEMPERATURE={m.group(1)!r} "
            f"与权威 {authority_raw!r} 不一致"
        )

    # ------------------------------------------------------------------
    # M2-ENVSYNC：根 .env.example（复制即生效的开发示例）LLM 数值契约
    # ------------------------------------------------------------------

    def _env_example_llm_value(self, key: str) -> str | None:
        """提取根 .env.example 中 `KEY=value` 形式的 LLM 键值（标准库解析）。"""
        content = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        m = re.search(rf"^{key}=([^\s#]+)", content, re.M)
        return m.group(1) if m else None

    def test_env_example_llm_values_match_authority(self):
        """根 .env.example 的 LLM_TIMEOUT/LLM_TEMPERATURE 必须等于权威默认 30/0.3。

        M2-ENVSYNC：原 60/0.7 出自 6a610f0（2026-07-24 无说明杂务提交）的
        搭车漂移——该文件被用户"复制为 .env"后立即生效，示例值不得偏离
        app/config.py 源码权威默认。
        """
        for key, field, typ in (
            ("LLM_TIMEOUT", "llm_timeout", int),
            ("LLM_TEMPERATURE", "llm_temperature", float),
        ):
            raw = self._env_example_llm_value(key)
            authority_raw = _config_field_default(field)
            assert raw is not None, f".env.example 缺少 {key} 声明"
            assert authority_raw is not None, f"app/config.py 缺少 {field} 字面默认值"
            assert raw.strip() != "", f".env.example {key} 为空字符串"
            assert typ(raw) == typ(authority_raw), (
                f".env.example {key}={raw!r} 与权威口径 app/config.py.{field}="
                f"{authority_raw!r} 不一致（历史漂移须对齐权威值）"
            )

    def test_llm_numeric_defaults_consistent_across_all_surfaces(self):
        """源码/dev/prod/生产模板/开发示例五个表面的 timeout 与 temperature 全对齐。"""
        timeout_authority = int(_config_field_default("llm_timeout"))
        temp_authority = float(_config_field_default("llm_temperature"))
        dev = _compose_llm_defaults("docker-compose.yaml")
        prod = _compose_llm_defaults("deploy/docker-compose.prod.yml")
        prod_tpl = (_REPO_ROOT / "deploy/env.production.example").read_text(encoding="utf-8")
        surfaces = {
            "app/config.py": (timeout_authority, temp_authority),
            "dev compose": (int(dev["LLM_TIMEOUT"]), float(dev["LLM_TEMPERATURE"])),
            "prod compose": (int(prod["LLM_TIMEOUT"]), float(prod["LLM_TEMPERATURE"])),
            "env.production.example": (
                int(re.search(r"^LLM_TIMEOUT=([^\s#]+)", prod_tpl, re.M).group(1)),
                float(re.search(r"^LLM_TEMPERATURE=([^\s#]+)", prod_tpl, re.M).group(1)),
            ),
            ".env.example": (
                int(self._env_example_llm_value("LLM_TIMEOUT")),
                float(self._env_example_llm_value("LLM_TEMPERATURE")),
            ),
        }
        for surface, (timeout, temperature) in surfaces.items():
            assert timeout == timeout_authority, (
                f"{surface} LLM_TIMEOUT={timeout} 与权威 {timeout_authority} 不一致"
            )
            assert temperature == temp_authority, (
                f"{surface} LLM_TEMPERATURE={temperature} 与权威 {temp_authority} 不一致"
            )

    def test_llm_temperature_consistent_across_deploy_surfaces(self):
        """dev compose / prod compose / env.production.example 三处 temperature
        必须一致且等于 app/config.py 权威默认（M2-CONFIGSYNC 对齐契约）。"""
        authority = float(_config_field_default("llm_temperature"))
        dev = float(_compose_llm_defaults("docker-compose.yaml")["LLM_TEMPERATURE"])
        prod = float(_compose_llm_defaults("deploy/docker-compose.prod.yml")["LLM_TEMPERATURE"])
        example_content = (_REPO_ROOT / "deploy/env.production.example").read_text(encoding="utf-8")
        example_m = re.search(r"^LLM_TEMPERATURE=([^\s#]+)", example_content, re.M)
        assert example_m is not None, "env.production.example 缺少 LLM_TEMPERATURE"
        example = float(example_m.group(1))
        assert dev == prod == example == authority, (
            f"三处 temperature 漂移：dev={dev} prod={prod} "
            f"example={example} 权威={authority}"
        )

    # ------------------------------------------------------------------
    # W10：监听地址默认收紧（P2-SEC-1）与 /metrics 豁免收紧（P2-SEC-2）的
    # 跨表面一致性。这两条不是文案检查：默认值一改，容器可达性与监控链路
    # 都会静默失效（healthcheck 在容器内跑，照样显示健康）。
    # ------------------------------------------------------------------

    def test_host_default_is_loopback_and_surfaces_agree(self):
        """源码默认必须是回环，且 .env.example 与之一致。"""
        src = (_REPO_ROOT / "app" / "config.py").read_text(encoding="utf-8")
        m = re.search(r"^\s*host:\s*str\s*=\s*\"([^\"]+)\"", src, re.M)
        assert m is not None, "app/config.py 找不到 host 字面默认值"
        assert m.group(1) == "127.0.0.1", (
            f"默认监听地址应为回环（单用户本地定位），实际={m.group(1)!r}（P2-SEC-1）"
        )

        env = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        env_m = re.search(r"^HOST=([^\s#]+)", env, re.M)
        assert env_m is not None, ".env.example 缺少 HOST 声明（复制即生效的示例必须写清）"
        assert env_m.group(1) == m.group(1), (
            f".env.example HOST={env_m.group(1)!r} 与权威默认 {m.group(1)!r} 不一致"
        )

    def test_container_surfaces_pin_wildcard_host(self):
        """容器表面必须显式绑 0.0.0.0，同时端口发布仍限回环。

        源码默认收紧为 127.0.0.1 后，容器若继承默认就只监听容器回环：
        ports 发布与容器间抓取全部打不通，而 healthcheck 在容器内执行仍会
        显示健康 —— 这是最难发现的一类故障，故用断言钉住。
        """
        dockerfile = (_REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert re.search(r"^ENV HOST=0\.0\.0\.0$", dockerfile, re.M), (
            "Dockerfile 缺少 ENV HOST=0.0.0.0（docker run 直接跑会不可达）"
        )
        for compose_relpath in ("docker-compose.yaml", "deploy/docker-compose.prod.yml"):
            content = (_REPO_ROOT / compose_relpath).read_text(encoding="utf-8")
            assert re.search(r"^\s*HOST:\s*0\.0\.0\.0\s*$", content, re.M), (
                f"{compose_relpath} 的 app 服务缺少 HOST: 0.0.0.0"
            )
            assert "127.0.0.1:" in content, (
                f"{compose_relpath} 的端口发布必须仍限回环（P2-F1 不得回退）"
            )

    def test_prometheus_scrape_carries_credentials(self):
        """绑非回环后 /metrics 不再免鉴权 → 抓取必须带凭据，且密钥不落盘。"""
        prom = (_REPO_ROOT / "deploy" / "prometheus.yml").read_text(encoding="utf-8")
        assert "authorization:" in prom, (
            "prometheus.yml 缺少 authorization —— /metrics 豁免收紧后抓取会恒 401"
        )
        assert "credentials: ${LUJO_API_KEY}" in prom, (
            "凭据必须经环境变量展开，不得把 API Key 明文写进配置文件"
        )
        compose = (_REPO_ROOT / "deploy" / "docker-compose.prod.yml").read_text(
            encoding="utf-8"
        )
        assert "LUJO_API_KEY: ${API_KEY:" in compose, (
            "prod compose 未把 API_KEY 传给 prometheus 容器"
        )
        assert "--config.expand-env=true" in compose, (
            "prometheus 未开启 --config.expand-env，${LUJO_API_KEY} 不会被展开"
        )

    def test_env_production_example_documents_host(self):
        content = (_REPO_ROOT / "deploy" / "env.production.example").read_text(
            encoding="utf-8"
        )
        assert re.search(r"^HOST=", content, re.M), (
            "env.production.example 缺少 HOST 声明"
        )
