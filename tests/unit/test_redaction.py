"""redaction 脱敏模块单测"""
import pytest

from app.config import settings
from app.runtime.core.redaction import redact


@pytest.fixture(autouse=True)
def _reset_redaction():
    """每个用例前后恢复默认脱敏配置，避免相互污染。"""
    saved = (settings.redaction_enabled, settings.redaction_extra_patterns)
    settings.redaction_enabled = True
    settings.redaction_extra_patterns = ""
    yield
    settings.redaction_enabled, settings.redaction_extra_patterns = saved


def test_password_masked():
    assert redact('password = "secret123"') == 'password="***"'
    assert redact("pwd: hello") == 'pwd="***"'
    assert redact("passwd='abc'") == 'passwd="***"'


def test_apikey_token_masked():
    assert redact('api_key = "sk-xxxx"') == 'api_key="***"'
    assert redact("token: abc.def.ghi") == 'token="***"'
    assert redact("api-key=BearerZ9") == 'api-key="***"'


def test_authorization_bearer_masked():
    out = redact("Authorization: Bearer eyJhbGciOiJIUzI1")
    assert "eyJhbGciOiJIUzI1" not in out
    assert "Bearer" in out  # 保留 scheme，只掩值


def test_phone_masked():
    assert redact("contact 13800138000 now") == "contact ***PHONE*** now"


def test_disabled_returns_original():
    settings.redaction_enabled = False
    raw = 'password = "secret123"'
    assert redact(raw) == raw


# ---------------------------------------------------------------------------
# FIX(v0.7.1-b4-6): 脱敏关闭时仅首次告警（此前每次调用刷屏）
# ---------------------------------------------------------------------------


def test_disabled_warns_only_once(caplog):
    import logging

    from app.runtime.core import redaction as redaction_module

    redaction_module._redaction_disabled_warned = False  # 重置节流标志
    saved = settings.redaction_enabled
    settings.redaction_enabled = False
    try:
        with caplog.at_level(logging.WARNING, logger="lujo-mcp.redaction"):
            redact('password = "a"')
            redact('token = "b"')
            redact('secret = "c"')
        warns = [r for r in caplog.records if "redaction is disabled" in r.getMessage()]
        assert len(warns) == 1, f"脱敏关闭应只告警 1 次，实际 {len(warns)}"
    finally:
        settings.redaction_enabled = saved
        redaction_module._redaction_disabled_warned = False


def test_none_and_non_string_passthrough():
    assert redact(None) is None
    assert redact("") == ""
    assert redact(12345) == 12345  # 非字符串原样返回


def test_no_false_positive_on_plain_text():
    assert redact("just a normal log line") == "just a normal log line"


def test_extra_patterns_applied():
    # 自定义：掩码身份证号（18 位）
    settings.redaction_extra_patterns = r"\b\d{17}[\dXx]\b"
    out = redact("id=110101199003071234 done")
    assert "110101199003071234" not in out
    assert "***" in out


def test_invalid_extra_pattern_skipped():
    settings.redaction_extra_patterns = "(unclosed\npassword = \"x\""
    # 无效正则被跳过，默认规则仍生效
    assert redact('password = "x"') == 'password="***"'


def test_json_password_masked():
    assert redact('{"password":"123456"}') == '{"password":"***"}'
    assert redact('{"pwd":"secret"}') == '{"pwd":"***"}'
    assert redact('{"passwd":"abc"}') == '{"passwd":"***"}'


def test_json_api_key_token_masked():
    assert redact('{"api_key":"sk-xxx"}') == '{"api_key":"***"}'
    assert redact('{"token":"abc"}') == '{"token":"***"}'
    assert redact('{"secret":"xyz"}') == '{"secret":"***"}'
    assert redact('{"authorization":"Bearer xxx"}') == '{"authorization":"***"}'


def test_json_nested_password_masked():
    assert redact('{"user":{"password":"123"}}') == '{"user":{"password":"***"}}'
    assert redact('{"data":{"api_key":"sk-123"}}') == '{"data":{"api_key":"***"}}'


def test_json_no_false_positive():
    assert redact('{"username":"admin"}') == '{"username":"admin"}'
    assert redact('{"email":"test@example.com"}') == '{"email":"test@example.com"}'


# ── CR-2 回归：下划线/连字符复合敏感键（\b 词边界在 '_' 处不成立导致此前漏脱敏）──


def test_underscore_compound_keys_masked():
    """refresh_token / client_secret / session_token 等复合键的 kv 形态必须脱敏。"""
    assert redact("refresh_token=eyJhbGciOiJIUzI1NiJ9.sig") == 'refresh_token="***"'
    assert redact("client_secret: abc-123") == 'client_secret="***"'
    assert redact("session_token=xyz") == 'session_token="***"'
    assert redact("api_secret=s3cr3t") == 'api_secret="***"'
    assert redact("id_token=eyJ") == 'id_token="***"'
    assert redact("my_secret_value=v") == 'my_secret_value="***"'


def test_compound_key_suffix_key_masked():
    """以 _key/-key 结尾的复合键（api_key / access_key / consumer_key）必须脱敏。"""
    assert redact("access_key=AKIA123") == 'access_key="***"'
    assert redact("consumer_key=ck-1") == 'consumer_key="***"'
    assert redact("X-API-KEY: v1") == 'X-API-KEY="***"'


def test_compound_keys_not_overredacted():
    """keyword / monkey / author 等正常词不得误伤。"""
    assert redact("keyword=rank") == "keyword=rank"
    assert redact("monkey=see") == "monkey=see"
    assert redact("author=alice") == "author=alice"


def test_is_sensitive_key_author_not_redacted():
    """R7-S2 回归：dict 键名路径（is_sensitive_key / redact_nested）不得误伤 author。

    此前 _SENSITIVE_SUBSTRINGS 的裸 "auth" 子串命中 "author"，git blame 归因
    字段在送 LLM 前（context_prep._redact_value_for_llm）被整值掩码，
    "这行谁改的" 核心信息失效。
    """
    from app.runtime.core.redaction import is_sensitive_key, redact_nested

    assert not is_sensitive_key("author")
    assert not is_sensitive_key("author_email")
    assert not is_sensitive_key("authority")

    blame = {"file": "app/a.py", "line": 3, "author": "Alice <a@x.com>", "date": "2026-01-01"}
    out = redact_nested(blame)
    assert out["author"] == "Alice <a@x.com>"

    # 收紧白名单不得引入 CR-2 回归：authorization 头仍是敏感键
    assert is_sensitive_key("authorization")
    assert is_sensitive_key("auth_header")
    assert redact_nested({"authorization": "Bearer xyz"})["authorization"] == "***REDACTED***"


def test_json_compound_keys_masked():
    """JSON 字符串形态的复合敏感键（浏览器 SDK 最常见的序列化形态）必须脱敏。"""
    assert redact('{"refresh_token":"eyJxxx"}') == '{"refresh_token":"***"}'
    assert redact('{"client_secret":"cs-1"}') == '{"client_secret":"***"}'
    assert redact('{"session_token": "st-1"}') == '{"session_token":"***"}'


def test_url_query_compound_token_masked():
    """URL 查询串中的复合 token 参数必须脱敏。"""
    out = redact("https://api.example.com/auth?refresh_token=eyJsecret&next=/home")
    assert "eyJsecret" not in out
    assert 'refresh_token="***"' in out


def test_capture_exception_locals_compound_keys_redacted():
    """CR-2 捕获路径：capture_exception 的 locals 复合敏感键 → ***REDACTED***。"""
    from app.runtime.collectors.stacktrace import capture_exception

    try:
        refresh_token = "eyJ-compound-secret"  # noqa: F841
        client_secret = "cs-secret"  # noqa: F841
        password_hash = "$2b$12$abc"  # noqa: F841  # 白名单字段应保留
        raise RuntimeError("auth failed")
    except RuntimeError as e:
        data = capture_exception(e, source="test")

    local_vars = data["frames"][0]["locals"]
    assert local_vars["refresh_token"] == "***REDACTED***"
    assert local_vars["client_secret"] == "***REDACTED***"
    # 白名单字段（trace_repo._DEFAULT_ALLOWLIST）在捕获期仍保留原值
    assert local_vars["password_hash"] == "'$2b$12$abc'"


def test_capture_exception_message_redacted():
    """exception_hook 路径：capture_exception 返回的 message 经 _redact_exception_data 后被脱敏"""
    from app.runtime.collectors.stacktrace import capture_exception
    from app.runtime.hooks.exception_hook import _redact_exception_data

    try:
        raise ValueError('login failed password="super_secret"')
    except ValueError as e:
        data = capture_exception(e, source="test")

    _redact_exception_data(data)
    assert "super_secret" not in data["message"]
    assert "***" in data["message"]


def test_capture_exception_traceback_redacted():
    """exception_hook 路径：capture_exception 返回的 traceback 经 _redact_exception_data 后被脱敏"""
    from app.runtime.collectors.stacktrace import capture_exception
    from app.runtime.hooks.exception_hook import _redact_exception_data

    secret_token = "ghp_abc123secrettoken"
    try:
        # 把敏感值放进局部变量，它会出现在 traceback 的 repr 中
        api_token = secret_token  # noqa: F841  # 故意留在局部变量，供 traceback 捕获并测试按键名脱敏
        raise RuntimeError("error with token in context")
    except RuntimeError as e:
        data = capture_exception(e, source="test")

    _redact_exception_data(data)
    # traceback 文本中不应出现原始 token 值
    # 注意：token 值可能以 repr 形式出现在局部变量中
    # redact 的正则会对 token=xxx 形式做掩码
    assert "ghp_abc123secrettoken" not in data["traceback"]


def test_capture_exception_locals_repr_truncated():
    """P2-D5：超大局部变量的 repr 被截断，防止数十 MB 字符串膨胀 OOM。"""
    from app.runtime.collectors.stacktrace import capture_exception

    def boom():
        huge_local = "x" * 500000  # ~500KB，捕获期应被截断
        raise ValueError("boom")

    try:
        boom()
    except ValueError as e:
        data = capture_exception(e, source="test")

    hit = None
    for f in data["frames"]:
        if "locals" in f and "huge_local" in f["locals"]:
            hit = f["locals"]["huge_local"]
            break
    assert hit is not None, "应能捕获到 boom 帧的 huge_local 局部变量"
    assert "<truncated" in hit
    assert len(hit) < 20000


def test_format_trace_for_ai_redacted():
    """format_trace_for_ai 输出文本中敏感信息已被 redact() 掩码"""
    from app.runtime.collectors.stacktrace import format_trace_for_ai

    exc_data = {
        "type": "ValueError",
        "message": 'password="leaked_pwd"',
        "frame_count": 0,
        "frames": [],
    }
    output = format_trace_for_ai(exc_data)
    assert "leaked_pwd" not in output
    assert "***" in output
