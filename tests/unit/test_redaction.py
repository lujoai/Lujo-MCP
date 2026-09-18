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


# ── U06 收尾：持锁日志 × JSONFormatter 回调 redact() 的自死锁回归 ──
#
# 现场（py-spy dump 实测）：setup_logging 装上的 JSONFormatter/RedactingFormatter
# 在 format() 里回调 redact()；若 _load_extra_rules / _warn_redaction_disabled_once
# 在持有各自非重入锁期间 logger.warning，同线程会再次进入同一把锁 → 全量 unit
# 卡死在 58%（本地与挂死现场均复现）。契约：warning 必须在锁释放后才发出。


def _thread_completes(target, timeout=5.0):
    import threading

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout=timeout)
    return not worker.is_alive()


def test_load_extra_rules_no_self_deadlock_under_reentrant_formatter(monkeypatch):
    """非法正则的 warning 回调 redact()（模拟 formatter）时不得自锁。"""
    import app.runtime.core.redaction as _r

    settings.redaction_extra_patterns = "(unclosed"
    _reset_extra_cache()

    def reentrant_warning(*args, **kwargs):
        # 模拟 JSONFormatter.format → redact() → _load_extra_rules 的同线程回入
        _r.redact('password = "x"')

    monkeypatch.setattr(_r.logger, "warning", reentrant_warning)
    finished = _thread_completes(_r._load_extra_rules)
    assert finished, "_load_extra_rules 持锁发日志导致同线程重入 _extra_lock 自死锁"
    assert _r._extra_cache == [], "重建完成后缓存应为空规则（非法正则被跳过）"


def test_disabled_warning_no_self_deadlock_under_reentrant_formatter(monkeypatch):
    """redaction 关闭的一次性 warning 同样不得在 _redaction_disabled_lock 内发出。"""
    import app.runtime.core.redaction as _r

    _r._redaction_disabled_warned = False
    settings.redaction_enabled = False

    def reentrant_warning(*args, **kwargs):
        _r.redact("whatever")

    monkeypatch.setattr(_r.logger, "warning", reentrant_warning)
    finished = _thread_completes(lambda: _r.redact("input text"))
    assert finished, "_warn_redaction_disabled_once 持锁发日志导致自死锁"
    _r._redaction_disabled_warned = False


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


# ── U05-FIX：灾难性回溯正则的配置期保护（方案 A：warning + 跳过）──
#
# 背景：嵌套量词/重叠重复结构（如 `(a+)+b`）在 ≥28 字符输入上呈指数级回溯，
# 经 redact() 生产路径实测 12.7s（见 U05-READONLY 审计）。该风险仅在作者配置
# 危险正则时触发（P2，非远程 P1）。既有契约「无效正则静默跳过，不阻断主流程」
# 决定了处置方式为跳过；危险正则**不得进入 `_extra_cache`**。

# 安全正则样本：必须保持兼容并继续生效（不得被危险形态检测误伤）
_SAFE_PATTERNS = [
    r"\b\d{17}[\dXx]\b",           # 身份证号
    r"\d{3}-\d{4}",                # 电话分段
    r"(?i)token[:=]\s*\S+",        # 大小写不敏感前缀
    r"[a-z]+@[a-z]+\.[a-z]{2,}",   # 邮箱
    r"(?:foo|bar)-baz",            # 非重叠交替
    r"prefix-\w+",                 # 单层量词
]

# 危险正则样本：已确认的明确嵌套量词 / 重叠重复结构
_DANGEROUS_PATTERNS = [
    r"(a+)+b",
    r"(a|a)*$",
    r"(\w+)+",
    r"(a*)*b",
    r"([a-z]+)+x",
]


def _reset_extra_cache():
    """清空额外规则缓存，使下次调用按当前 settings 重编译。"""
    import app.runtime.core.redaction as _r
    _r._extra_cache = None
    _r._extra_signature = None


def test_dangerous_extra_patterns_not_loaded():
    """危险正则必须被跳过，不得进入 _extra_cache。"""
    import app.runtime.core.redaction as _r
    for dangerous in _DANGEROUS_PATTERNS:
        settings.redaction_extra_patterns = dangerous
        _reset_extra_cache()
        rules = _r._load_extra_rules()
        assert rules == [], f"危险正则未被跳过: {dangerous!r}"
        assert _r._extra_cache == [], f"危险正则进入了缓存: {dangerous!r}"


def test_dangerous_pattern_does_not_block_redact():
    """危险正则被跳过后，28 字符恰配输入必须毫秒级返回（不阻塞）。"""
    import time
    settings.redaction_extra_patterns = r"(a+)+b"
    _reset_extra_cache()
    t0 = time.perf_counter()
    out = redact("a" * 28 + "X")
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, f"redact 被危险正则阻塞: {elapsed:.2f}s"
    # 默认规则仍生效（该输入无敏感内容，应原样返回）
    assert out == "a" * 28 + "X"


def test_dangerous_pattern_warning_is_safe_summary(caplog):
    """warning 不得输出完整正则内容，只输出序号或安全摘要。"""
    import app.runtime.core.redaction as _r
    settings.redaction_extra_patterns = r"(a+)+b"
    _reset_extra_cache()
    with caplog.at_level("WARNING"):
        _r._load_extra_rules()
    assert caplog.text, "危险正则未产生 warning"
    assert "(a+)+b" not in caplog.text, "warning 泄漏了完整正则内容"


def test_safe_extra_patterns_still_apply():
    """安全正则必须继续兼容并生效（不被危险形态检测误伤）。"""
    import app.runtime.core.redaction as _r
    for pattern in _SAFE_PATTERNS:
        settings.redaction_extra_patterns = pattern
        _reset_extra_cache()
        rules = _r._load_extra_rules()
        assert len(rules) == 1, f"安全正则被误拦: {pattern!r}"


def test_safe_pattern_sensitive_and_insensitive_examples():
    """安全正则的命中与不命中行为保持原样。"""
    settings.redaction_extra_patterns = r"\b\d{17}[\dXx]\b"
    _reset_extra_cache()
    assert "110101199003071234" not in redact("id=110101199003071234 done")
    assert "***" in redact("id=110101199003071234 done")
    # 不命中的输入保持原样
    assert redact("id=123 done") == "id=123 done"


def test_invalid_pattern_still_warns_and_skips(caplog):
    """非法正则保持既有 warning + skip 行为（回退兼容）。"""
    import app.runtime.core.redaction as _r
    settings.redaction_extra_patterns = "(unclosed"
    _reset_extra_cache()
    with caplog.at_level("WARNING"):
        rules = _r._load_extra_rules()
    assert rules == []
    assert "unclosed" in caplog.text or "无效的脱敏正则" in caplog.text


def test_mixed_config_keeps_safe_and_drops_dangerous(caplog):
    """混合配置：安全正则保留生效，危险正则被丢弃。"""
    import app.runtime.core.redaction as _r
    settings.redaction_extra_patterns = "\n".join([
        r"\d{3}-\d{4}",
        r"(a+)+b",
        r"[a-z]+@[a-z]+\.[a-z]{2,}",
        "(unclosed",
    ])
    _reset_extra_cache()
    with caplog.at_level("WARNING"):
        rules = _r._load_extra_rules()
    assert len(rules) == 2, f"应保留 2 条安全规则，实际 {len(rules)}"
    out = redact("call 555-1234 from a@b.com")
    assert "555-1234" not in out
    assert "a@b.com" not in out


def test_cache_rebuild_after_config_change_drops_old_rules():
    """配置变化后缓存必须重建，不复用旧规则（危险 → 安全、安全 → 危险 双向）。"""
    import app.runtime.core.redaction as _r
    # 先配置安全正则并确认生效
    settings.redaction_extra_patterns = r"\d{3}-\d{4}"
    _reset_extra_cache()
    assert len(_r._load_extra_rules()) == 1
    assert "555-1234" not in redact("call 555-1234")

    # 改为危险正则：缓存必须重建且不含旧的安全规则
    settings.redaction_extra_patterns = r"(a+)+b"
    rules = _r._load_extra_rules()
    assert rules == [], "配置变更后仍复用旧规则"
    # 旧安全检查不再命中（旧规则已被正确丢弃）
    assert redact("call 555-1234") == "call 555-1234"

    # 再改回安全正则：必须重新生效
    settings.redaction_extra_patterns = r"\d{3}-\d{4}"
    assert len(_r._load_extra_rules()) == 1
    assert "555-1234" not in redact("call 555-1234")


def test_dangerous_pattern_no_timeout_in_isolated_subprocess():
    """隔离子进程超时回归：危险正则 + 30 字符输入必须在硬超时内返回。

    U05-READONLY 审计证据：修复前 `(a+)+b` + 30 字符 >15s（子进程被杀）；
    28 字符 12.7s。本用例在独立可杀死子进程中验证修复后的实际耗时，
    pytest 主进程永远不会被拖死（10s 硬超时 + kill）。
    """
    import subprocess
    import sys
    from pathlib import Path

    repo = str(Path(__file__).resolve().parents[2])
    code = (
        "import sys, time\n"
        f"sys.path.insert(0, r'{repo}')\n"
        "from app.config import settings\n"
        "settings.redaction_enabled = True\n"
        "settings.redaction_extra_patterns = r'(a+)+b'\n"
        "from app.runtime.core.redaction import redact\n"
        "t0 = time.perf_counter()\n"
        "out = redact('a' * 30 + 'X')\n"
        "print('ELAPSED_MS=', round((time.perf_counter() - t0) * 1000))\n"
    )
    # 说明：子进程 warning 含中文，Windows 默认 stderr 编码（GBK）会让
    # subprocess.run 的 UTF-8 解码失败；显式指定 errors="replace" 规避。
    # env 不裁剪 PATH —— 避免与本次修复无关的进程启动副作用。
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=10,
        cwd=repo, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, f"子进程失败: {(proc.stderr or '')[-200:]}"
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("ELAPSED_MS=")]
    assert line, f"未取到耗时: {(proc.stdout or '')[-200:]}"
    elapsed_ms = int(line[0].split("=")[1])
    assert elapsed_ms < 3000, f"危险正则仍导致回溯：{elapsed_ms}ms（修复前 >15000ms）"
