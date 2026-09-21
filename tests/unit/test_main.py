"""单元测试：应用启动安全校验"""
import logging

import pytest

from app.config import settings
from app.main import validate_startup_configuration


def test_validate_startup_configuration_rejects_exposed_bind_without_api_key(monkeypatch):
    # 隔离 .env 含 API_KEY=test_secret_key_456 的污染
    monkeypatch.setattr(settings, "api_key", None)
    with pytest.raises(RuntimeError, match="0.0.0.0"):
        validate_startup_configuration(host="0.0.0.0", api_key=None)


def test_validate_startup_configuration_allows_local_bind_without_api_key(monkeypatch):
    # 隔离 .env 污染，确保测试语义为"无 API_KEY"场景
    monkeypatch.setattr(settings, "api_key", None)
    validate_startup_configuration(host="127.0.0.1", api_key=None)


def test_validate_startup_configuration_allows_exposed_bind_with_api_key():
    # 显式传 api_key="secret"，函数不读 settings，无需 monkeypatch
    validate_startup_configuration(host="0.0.0.0", api_key="secret")


# S3-4: 非 loopback 绑定 + 无鉴权 → WARNING（不阻断启动）
def test_validate_warns_on_non_loopback_bind_without_api_key(monkeypatch, caplog):
    # 直接隔离 auth_enabled()，避免 .env 的 API_KEY/API_KEYS 污染
    monkeypatch.setattr("app.main.auth_enabled", lambda: False)
    with caplog.at_level(logging.WARNING):
        validate_startup_configuration(host="192.168.1.10", api_key=None)
    assert "192.168.1.10" in caplog.text
    assert "API_KEY" in caplog.text


def test_validate_no_warn_on_loopback_bind_without_api_key(monkeypatch, caplog):
    monkeypatch.setattr("app.main.auth_enabled", lambda: False)
    with caplog.at_level(logging.WARNING):
        validate_startup_configuration(host="127.0.0.1", api_key=None)
        validate_startup_configuration(host="localhost", api_key=None)
    assert "非回环" not in caplog.text


# R7-A1: 绑定检测用 ipaddress 语义，不用子串匹配
def test_validate_rejects_ipv6_unspecified_without_api_key(monkeypatch):
    """IPv6 通配 ``::`` 等价全网监听：无鉴权时必须硬拒绝（旧子串匹配漏掉）。"""
    monkeypatch.setattr(settings, "api_key", None)
    with pytest.raises(RuntimeError):
        validate_startup_configuration(host="::", api_key=None)


# S2-F3: HOST 为空串/纯空白 = INADDR_ANY（CPython bind(("",port)) 语义；
# uvicorn 0.49 config.py 直接 sock.bind((self.host, self.port)) 透传），
# 必须与 0.0.0.0 / :: 同判为通配。此前 is_unspecified_bind('') 因 ValueError
# 返回 False，启动期只打 WARNING 不拒绝 —— 两道守卫同时沉默的根因之一。
def test_validate_rejects_empty_host_without_api_key(monkeypatch):
    """HOST=''（.env 空值，pydantic 实测不回落默认而是得到 ''）→ 启动期硬拒绝。"""
    monkeypatch.setattr(settings, "api_key", None)
    with pytest.raises(RuntimeError):
        validate_startup_configuration(host="", api_key=None)


def test_validate_rejects_whitespace_host_without_api_key(monkeypatch):
    """HOST='   '（引号包裹的空白串，pydantic 实测不 strip）→ 同样硬拒绝。"""
    monkeypatch.setattr(settings, "api_key", None)
    with pytest.raises(RuntimeError):
        validate_startup_configuration(host="   ", api_key=None)


def test_validate_not_misled_by_address_containing_zero_subnet(monkeypatch, caplog):
    """合法地址 10.0.0.0 / 100.0.0.0 含 "0.0.0.0" 子串：不再被误杀成硬拒绝，
    走"非回环 + 无鉴权"WARNING 路径。"""
    monkeypatch.setattr("app.main.auth_enabled", lambda: False)
    with caplog.at_level(logging.WARNING):
        validate_startup_configuration(host="10.0.0.0", api_key=None)
        validate_startup_configuration(host="100.0.0.0", api_key=None)
    assert "10.0.0.0" in caplog.text
    assert "100.0.0.0" in caplog.text


def test_validate_hostname_bind_warns_without_api_key(monkeypatch, caplog):
    """无法解析为主机名/地址绑定（如自定义域名）：保留非回环 warning 路径。"""
    monkeypatch.setattr("app.main.auth_enabled", lambda: False)
    with caplog.at_level(logging.WARNING):
        validate_startup_configuration(host="debug.example.com", api_key=None)
    assert "debug.example.com" in caplog.text


def test_validate_no_warn_on_non_loopback_bind_with_api_key(caplog):
    with caplog.at_level(logging.WARNING):
        validate_startup_configuration(host="192.168.1.10", api_key="secret")
    assert "非回环" not in caplog.text


# S9: 脱敏关闭是受支持的显式 opt-out，但必须在启动校验里显式告警（不可事后补）。
def test_validate_warns_when_redaction_disabled(monkeypatch, caplog):
    """redaction_enabled=False → 启动校验恰好产生一条含固定标记的 warning。"""
    monkeypatch.setattr(settings, "redaction_enabled", False)
    with caplog.at_level(logging.WARNING):
        validate_startup_configuration(host="127.0.0.1", api_key="secret")
    marked = [
        record
        for record in caplog.records
        if "REDACTION_DISABLED_AT_STARTUP" in record.getMessage()
    ]
    assert len(marked) == 1
    # 干净场景（回环 + 有鉴权）下校验函数自身恰好一条 warning。注意不要断言
    # caplog.records 总数：全量套件中 logging formatter 已装载，其 redact() 回调
    # 会追加 "lujo-mcp.redaction" 子 logger 的既有一次性告警（FIX(v0.7.1-b4-6)），
    # 那是环境产物而非校验函数产物；同理也不得靠 import 顺序屏蔽它。
    own = [
        record
        for record in caplog.records
        if record.name == "lujo-mcp" and record.levelno == logging.WARNING
    ]
    assert len(own) == 1
    # 固定文案，不含配置值/敏感内容（不出现 key=value 形态）
    assert "=" not in marked[0].getMessage()


def test_validate_no_warning_when_redaction_enabled(monkeypatch, caplog):
    """redaction_enabled=True（默认）→ 不产生该标记的 warning。"""
    monkeypatch.setattr(settings, "redaction_enabled", True)
    with caplog.at_level(logging.WARNING):
        validate_startup_configuration(host="127.0.0.1", api_key="secret")
    assert "REDACTION_DISABLED_AT_STARTUP" not in caplog.text


# ---------------------------------------------------------------------------
# P3-13: /internal/health 反代部署下不得信任 client.host 私网判定
# ---------------------------------------------------------------------------

def test_internal_health_with_forwarded_header_requires_key(monkeypatch):
    """反代场景：client.host 为私网 IP（旧逻辑放行）但携带转发头 → fail-closed 403。"""
    from types import SimpleNamespace

    from app.auth import key_rotation
    from app.main import internal_health

    # 无鉴权配置（隔离 .env 的 API_KEY 污染）
    monkeypatch.setattr(key_rotation, "get_valid_keys", list)

    class _FakeRequest:
        client = SimpleNamespace(host="192.168.1.10")
        headers = {"X-Forwarded-For": "203.0.113.5"}

    resp = internal_health(_FakeRequest)
    assert resp.status_code == 403


def test_internal_health_forwarded_with_valid_key_allowed(monkeypatch):
    """反代场景 + 有效 API Key → 放行（不再信任私网判定但 key 校验通过）。"""
    from types import SimpleNamespace

    from app.auth import key_rotation
    from app.main import internal_health

    monkeypatch.setattr(key_rotation, "get_valid_keys", lambda: ["secret-key"])

    class _FakeRequest:
        client = SimpleNamespace(host="192.168.1.10")
        headers = {"X-Real-IP": "203.0.113.5", "X-API-Key": "secret-key"}

    resp = internal_health(_FakeRequest)
    assert isinstance(resp, dict)
    assert resp["status"] in ("ok", "degraded", "unhealthy")


# ---------------------------------------------------------------------------
# W9 / P3-SEC-4: POST /debug 的回显必须是脱敏副本
# ---------------------------------------------------------------------------

def test_debug_echo_is_redacted():
    """/debug 不得把调用方 POST 进来的密钥原样回显到 HTTP 响应里。

    落库那份一直是干净的（add_log 内部过 redact_nested），漏的只有响应体；
    对照 app/api/debug.py 的同名端点，那里一直是 redact_nested(req.payload)。
    """
    import json

    from app.main import debug

    secret = "hunter2-super-secret"
    resp = debug({"password": secret, "note": "keep-me"})

    echoed = resp["result"]["echo"]
    assert echoed["password"] != secret, "响应体原样回显了密钥（P3-SEC-4）"
    assert secret not in json.dumps(echoed, ensure_ascii=False)
    assert echoed["note"] == "keep-me", "非敏感字段不得被一并抹掉"
    assert resp["request_id"]


# ---------------------------------------------------------------------------
# W13 / P3-STORE-4: KB 持久化降级必须在 health 上可见
# ---------------------------------------------------------------------------

def _force_kb_persist_degraded(monkeypatch):
    """让 KB 持久化「被要求但初始化失败」→ factory 降级 NoOp。"""
    import app.runtime.core.storage.factory as factory

    monkeypatch.setattr(settings, "kb_persist_enabled", True)
    monkeypatch.setattr(factory, "_knowledge_store", None)

    def _boom(*args, **kwargs):
        raise RuntimeError("sqlite notebook unavailable")

    monkeypatch.setattr(
        "app.runtime.core.storage.sqlite_kb_store.SQLiteKnowledgeBaseStore", _boom
    )
    return factory


def test_health_degraded_when_kb_persistence_unavailable(monkeypatch):
    """持久化被请求却降级 no-op 时，/health 不得再报 ok。

    旧实现 ``storage_ok = True`` 是硬编码：本地笔记本坏了（经验不再跨重启保留）
    而健康检查一切正常，使用者无从得知。
    """
    from app.main import health

    # llm_ok 置真，才能把 status 落在 degraded 而不是 unhealthy（两者都不 ok 时
    # 既有语义是 unhealthy）——本用例要考的是 storage 这一维。
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    _force_kb_persist_degraded(monkeypatch)

    assert health()["status"] == "degraded", "KB 持久化降级但 /health 仍报 ok（P3-STORE-4）"


def test_internal_health_exposes_kb_persist_state(monkeypatch):
    """/internal/health 必须区分 disabled / sqlite / degraded 三态。

    ``storage`` 字段保持后端名不变（e2e 的服务器身份校验按 service/version/storage
    三项比对，改它会破坏复用判定），新状态走独立的 ``kb_persist`` 字段。
    """
    from types import SimpleNamespace

    from app.auth import key_rotation
    from app.main import internal_health

    monkeypatch.setattr(key_rotation, "get_valid_keys", list)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")

    class _FakeRequest:
        client = SimpleNamespace(host="127.0.0.1")
        headers = {}

    factory = _force_kb_persist_degraded(monkeypatch)
    payload = internal_health(_FakeRequest)
    assert payload["kb_persist"] == "degraded"
    assert payload["storage"] == settings.storage_backend, "storage 字段不得改变语义"
    assert payload["status"] == "degraded"
    assert factory.kb_persist_degraded() is True

    # 显式关闭持久化 → disabled，且不判为不健康
    monkeypatch.setattr(settings, "kb_persist_enabled", False)
    monkeypatch.setattr(factory, "_knowledge_store", None)
    payload_off = internal_health(_FakeRequest)
    assert payload_off["kb_persist"] == "disabled"
    assert payload_off["status"] == "ok"
    assert factory.kb_persist_degraded() is False
