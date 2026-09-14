"""单元测试：生产环境部署配置与文件合法性校验（仅使用标准库）"""

from pathlib import Path


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
