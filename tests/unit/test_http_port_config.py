"""P1-C 端口与宿主兼容：HTTP_PORT / HTTP_HOST 环境变量支持（单元测试）。

背景：默认 HTTP 端口 8000 与开发圈最常用端口高频冲突（真实事故两次）；且
Trae 类宿主实测会丢弃 MCP args 里的附加 CLI 参数（npx 命令被自解析为缓存
exe 直启），CLI 方式传端口在这类宿主上不可用——环境变量是宿主界公认更可靠
的传参通道。

锁定的三层语义：
1. settings 层：HTTP_PORT / HTTP_HOST env 可配；默认端口 8710；旧键 PORT /
   HOST 经 AliasChoices 继续生效，同名冲突时新键优先。
2. CLI 解析层：--http-port / --http-host 显式传入时优先于 env。
3. resolve 层（mcp_server._resolve_http_bind）：CLI 未显式传参时回落 settings
   （即 env 生效）；is_default_port 只由「CLI 是否显式传了 --http-port」决定
   ——env 配置的端口冲突沿用「降级不退出」语义，宿主 MCP 保持可用。
"""

import argparse

import pytest

from app.config import Settings, settings
import app.mcp_server as mcp_server


@pytest.fixture(autouse=True)
def _clean_port_host_env(monkeypatch):
    """隔离宿主/本机 .env 泄漏进来的端口与地址配置，保证每个用例独立口径。"""
    for key in ("HTTP_PORT", "PORT", "HTTP_HOST", "HOST"):
        monkeypatch.delenv(key, raising=False)


def _make_settings() -> Settings:
    """绕开项目根 .env（开发者本机内容不可控），只用真实进程 env 构造。"""
    return Settings(_env_file=None)


# ── 1. settings 层：env 可配 + 默认值 ──


def test_env_http_port_overrides_settings(monkeypatch):
    monkeypatch.setenv("HTTP_PORT", "8555")
    assert _make_settings().http_port == 8555


def test_env_http_host_overrides_settings(monkeypatch):
    monkeypatch.setenv("HTTP_HOST", "0.0.0.0")
    assert _make_settings().http_host == "0.0.0.0"


def test_default_http_port_is_8710():
    assert _make_settings().http_port == 8710


def test_default_http_host_is_loopback():
    assert _make_settings().http_host == "127.0.0.1"


# ── 2. 旧键 HOST / PORT 向后兼容（既有 .env / 容器部署不破坏）──


def test_legacy_port_env_still_works(monkeypatch):
    monkeypatch.setenv("PORT", "9100")
    assert _make_settings().http_port == 9100


def test_legacy_host_env_still_works(monkeypatch):
    monkeypatch.setenv("HOST", "0.0.0.0")
    assert _make_settings().http_host == "0.0.0.0"


def test_new_env_key_wins_over_legacy(monkeypatch):
    monkeypatch.setenv("HTTP_PORT", "8555")
    monkeypatch.setenv("PORT", "9000")
    assert _make_settings().http_port == 8555


# ── 3. CLI 显式参数 > env（argparse + resolve 回落）──


def _resolve(argv: list[str]) -> tuple[str, int, bool]:
    options: argparse.Namespace = mcp_server._parse_runtime_args(argv)
    return mcp_server._resolve_http_bind(options)


def test_cli_port_beats_env(monkeypatch):
    monkeypatch.setenv("HTTP_PORT", "8555")
    _host, port, is_default = _resolve(["--http-port", "9999"])
    assert port == 9999
    assert is_default is False


def test_cli_host_beats_env(monkeypatch):
    monkeypatch.setenv("HTTP_HOST", "0.0.0.0")
    host, port, is_default = _resolve(["--http-host", "127.0.0.1"])
    assert host == "127.0.0.1"
    assert port == settings.http_port
    assert is_default is True


def test_cli_absent_falls_back_to_env_settings(monkeypatch):
    """CLI 未传 → 回落 settings：env 配置在 Trae 类丢弃 args 的宿主上生效。

    settings 单例在进程启动时一次性读入 env（生产路径），单测里等价模拟为
    把单例字段置成 env 加载后的值（env→Settings 的映射已由上面第 1 组用例
    单独锁定）。
    """
    monkeypatch.setattr(settings, "http_port", 8555)
    monkeypatch.setattr(settings, "http_host", "0.0.0.0")
    host, port, is_default = _resolve(["--http"])
    assert (host, port) == ("0.0.0.0", 8555)
    # env 指定的端口冲突沿用「降级不退出」语义：宿主 MCP 保持可用
    assert is_default is True


def test_resolve_without_env_uses_builtin_defaults():
    host, port, is_default = _resolve([])
    assert (host, port, is_default) == ("127.0.0.1", 8710, True)
