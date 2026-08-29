"""v0.7.0 Minor：.env.example 安全配置段完整性守护测试。

历史缺口（第 6 轮 Minor）：.env.example 缺 API_KEYS / RBAC / CORS 等安全项，
用户照抄样例部署后免鉴权且不自知。本测试守护安全相关键必须出现在样例中，
且样例不得包含真实密钥。
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"

# 对照 app/config.py 的 Settings 安全相关字段（缺一个都会让样例部署漏配）
REQUIRED_SECURITY_KEYS = (
    "API_KEY",
    "API_KEYS",
    "RBAC_ENABLED",
    "RBAC_ROLE_MAPPING",
    "CORS_ORIGINS",
    "BEACON_TOKEN_TTL_SECONDS",
    "BEACON_TOKEN_SCOPE",
    "METRICS_AUTH_ENABLED",
    "TRUSTED_PROXY_COUNT",
)


def test_security_keys_documented_in_env_example():
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    for key in REQUIRED_SECURITY_KEYS:
        line = f"{key}="
        assert line in text, f".env.example 缺少安全配置项 {key}（样例部署漏配风险）"


def test_env_example_carries_no_real_secret():
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        # 密钥类键值必须是空串或占位符，不得是疑似真实凭据的长随机串
        if key.strip() in ("API_KEY", "API_KEYS", "QDRANT_API_KEY", "OPENAI_API_KEY"):
            assert value.strip() in ("", "your_openai_api_key_here"), (
                f"{key} 样例值疑似真实密钥: {value[:8]}..."
            )


def test_rbac_keys_have_advice_comment():
    """安全建议注释随键交付（默认值 + fail-closed 语义说明）。"""
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "fail-closed" in text or "最小权限" in text
    assert "免鉴权" in text
