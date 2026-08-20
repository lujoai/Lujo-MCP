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
        assert "postgres:" in content
        assert "redis:" in content
        assert "app:" in content
        assert "prometheus:" in content
        assert "restart: unless-stopped" in content
        assert "limits:" in content

    def test_env_production_example_keys(self):
        env_example = Path("deploy/env.production.example").read_text(encoding="utf-8")
        required_keys = ["API_KEY", "PG_PASSWORD", "LLM_MODEL", "PROMETHEUS_PORT"]
        for k in required_keys:
            assert f"{k}=" in env_example, f"缺少关键配置项: {k}"
