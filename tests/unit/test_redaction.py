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
