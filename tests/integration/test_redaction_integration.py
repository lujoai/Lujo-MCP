"""H5 复核集成测试：含敏感字段的 ingest frames / network / ui_event / console 端到端脱敏验证。

复核目标（v0.3.0 Release Audit H5）：
- H5 修复：locals/ingest frames 入库前统一脱敏（stacktrace + trace_repo._redact_nested）。
- 单测仅覆盖 redact() 纯函数，缺端到端集成复核。
- 本测试构造含敏感字段的 ingest frames（locals 含 password/token）、network、ui_event、console，
  经 save_trace → get_trace / save_network_record → get_network_records 等回读，
  验证字段已脱敏（不修改业务代码，仅用实际占位符写断言）。

脱敏占位符（grep 确认）：
- dict-key 路径（敏感键名）：locals["password"] → "***REDACTED***"
- 字符串正则路径（redact()）：
  - 'password = "x"' → 'password="***"'
  - '?token=secret' → '?token="***"'
  - '{"password":"123"}' → '{"password":"***"}'
  - 'token: abc.def.ghi' → 'token="***"'
  - 'Authorization: Bearer xxx' → 'Authorization: Bearer ***'

注意：ingest_api._parse_frames 仅保留 file/line/function/code 字段，丢弃 locals。
因此 ingest 路径只能验证 code 字段脱敏（code 是字符串，走 redact() 正则路径）。
locals 脱敏的端到端验证通过 save_trace 直接调用完成（绕过 _parse_frames）。
"""
import uuid

from app.runtime.core.trace_repo import (
    save_trace,
    get_trace,
    save_network_record,
    get_network_records,
    save_ui_event,
    get_ui_events,
    save_console_log,
    get_console_logs,
)
from app.mcp.tools.ingest_api import ingest_error_handler


def _unique_trace_id(prefix: str = "audit") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class TestSaveTraceRedactsLocals:
    """save_trace 入库前对 frames.locals 敏感键名脱敏 —— H5 修复核心。"""

    def test_locals_sensitive_keys_redacted_to_REDACTED_placeholder(self):
        """locals 中 password/token 等敏感键名 → "***REDACTED***"，user 等保留。"""
        trace_id = _unique_trace_id()
        frames = [
            {
                "file": "app/auth.py",
                "line": 42,
                "function": "login",
                "code": "user = authenticate(form)",
                "locals": {
                    "password": "super-secret-123",
                    "token": "eyJhbGciOiJIUzI1NiJ9.payload.sig",
                    "api_key": "sk-abc123",
                    "authorization": "Bearer eyJxyz",
                    "user": "alice",
                },
            }
        ]
        error_id = save_trace(
            exc_type="AuthError",
            message="login failed",
            frames=frames,
            source="ingest",
            trace_id=trace_id,
        )

        got = get_trace(error_id)
        assert got is not None, "trace 未入库"
        local_vars = got["frames"][0]["locals"]

        # 敏感键 → "***REDACTED***"（dict-key 路径，grep 确认实际占位符）
        assert local_vars["password"] == "***REDACTED***", (
            f"password 应被脱敏为 ***REDACTED***，实际: {local_vars['password']!r}"
        )
        assert local_vars["token"] == "***REDACTED***"
        assert local_vars["api_key"] == "***REDACTED***"
        assert local_vars["authorization"] == "***REDACTED***"

        # 非敏感键保留原值
        assert local_vars["user"] == "alice"

    def test_locals_nested_dict_sensitive_keys_redacted(self):
        """locals 中嵌套 dict 的敏感键也递归脱敏。"""
        trace_id = _unique_trace_id()
        frames = [
            {
                "file": "app/api.py",
                "line": 10,
                "function": "handler",
                "code": "",
                "locals": {
                    "config": {
                        # 子串匹配：键名含 password/token/secret/key/auth 等子串即脱敏
                        "password": "should-not-leak",
                        "name": "production",
                        "credentials": {
                            "secret": "nested-secret",
                            "token": "nested-token-value",
                        },
                    },
                },
            }
        ]
        error_id = save_trace(
            exc_type="RuntimeError",
            message="err",
            frames=frames,
            trace_id=trace_id,
        )

        got = get_trace(error_id)
        config = got["frames"][0]["locals"]["config"]

        # 嵌套 dict 中 password / secret / token 均脱敏（子串匹配）
        assert config["password"] == "***REDACTED***"
        assert config["credentials"]["secret"] == "***REDACTED***"
        assert config["credentials"]["token"] == "***REDACTED***"
        # 非敏感键保留
        assert config["name"] == "production"

    def test_compound_sensitive_key_names_redacted_by_substring_match(self):
        """复合键名（如 db_password / user_token）含敏感子串，dict-key 路径脱敏。

        Phase 2 扩展：_SENSITIVE_KEYS 改为子串包含判断，
        db_password 含 "password"、user_token 含 "token" 均被脱敏。
        """
        trace_id = _unique_trace_id()
        frames = [
            {
                "file": "app/config.py",
                "line": 1,
                "function": "load",
                "code": "",
                "locals": {
                    "db_password": "compound-key-value",   # 含 "password" 子串
                    "user_token": "compound-token",        # 含 "token" 子串
                    "auth_header": "bearer-xyz",          # 含 "auth" 子串
                    "secret_config": "secret-val",        # 含 "secret" 子串
                    "api_key_id": "akid-123",             # 含 "key" 子串
                    "password": "exact-key-value",        # 精确匹配
                    "cookie_value": "session-cookie",    # 含 "cookie" 子串
                },
            }
        ]
        error_id = save_trace(
            exc_type="ConfigError",
            message="err",
            frames=frames,
            trace_id=trace_id,
        )

        got = get_trace(error_id)
        local_vars = got["frames"][0]["locals"]

        # 精确匹配 + 复合键名均被脱敏
        assert local_vars["password"] == "***REDACTED***"
        assert local_vars["db_password"] == "***REDACTED***"
        assert local_vars["user_token"] == "***REDACTED***"
        assert local_vars["auth_header"] == "***REDACTED***"
        assert local_vars["secret_config"] == "***REDACTED***"
        assert local_vars["api_key_id"] == "***REDACTED***"
        assert local_vars["cookie_value"] == "***REDACTED***"


class TestSaveTraceRedactsMessage:
    """save_trace 入库前对 message 走 redact() 正则脱敏。"""

    def test_message_with_password_pattern_masked(self):
        """message 含 'password = "leaked"' → redact() 正则脱敏。"""
        trace_id = _unique_trace_id()
        error_id = save_trace(
            exc_type="ValueError",
            message='auth failed: password = "leaked" and token: abc.def.ghi',
            frames=[],
            trace_id=trace_id,
        )

        got = get_trace(error_id)
        msg = got["message"]

        # 敏感值已脱敏，原始 secret 不外泄
        assert "leaked" not in msg, f"message 仍含原始敏感值: {msg!r}"
        assert "abc.def.ghi" not in msg, f"message 仍含原始 token: {msg!r}"

        # redact() 实际占位符（grep 确认）：password="***" / token="***"
        assert 'password="***"' in msg, f"未按预期脱敏 password: {msg!r}"
        assert 'token="***"' in msg, f"未按预期脱敏 token: {msg!r}"

    def test_message_without_sensitive_pattern_preserved(self):
        """message 无敏感模式时保持原样。"""
        trace_id = _unique_trace_id()
        original = "ConnectionError: failed to connect to backend"
        error_id = save_trace(
            exc_type="ConnectionError",
            message=original,
            frames=[],
            trace_id=trace_id,
        )

        got = get_trace(error_id)
        assert got["message"] == original


class TestSaveTraceRedactsExtra:
    """save_trace 入库前对 extra 嵌套结构递归脱敏。"""

    def test_extra_nested_sensitive_keys_redacted(self):
        """extra 含嵌套 dict 的敏感键 → "***REDACTED***"。"""
        trace_id = _unique_trace_id()
        error_id = save_trace(
            exc_type="ValueError",
            message="err",
            frames=[],
            extra={
                "headers": {
                    "authorization": "Bearer eyJxyz",
                    "content-type": "application/json",
                },
                "meta": {
                    "api_key": "sk-1",
                    "request_id": "req-abc",
                },
                "tags": ["production", "critical"],
            },
            trace_id=trace_id,
        )

        got = get_trace(error_id)
        extra = got["extra"]

        # 敏感键 → "***REDACTED***"
        assert extra["headers"]["authorization"] == "***REDACTED***"
        assert extra["meta"]["api_key"] == "***REDACTED***"

        # 非敏感键保留
        assert extra["headers"]["content-type"] == "application/json"
        assert extra["meta"]["request_id"] == "req-abc"
        assert extra["tags"] == ["production", "critical"]


class TestIngestErrorHandlerRedactsCodeField:
    """ingest_error_handler 端到端脱敏复核。

    注意：ingest_api._parse_frames 仅保留 file/line/function/code 字段，丢弃 locals。
    因此 ingest 路径的敏感数据仅在 code 字段（字符串）中保留，code 走 redact() 正则脱敏。
    """

    def test_ingest_frames_code_field_with_secret_is_redacted(self):
        """ingest 上报 frames 含敏感 code 字符串 → 入库后 code 已脱敏。"""
        trace_id = _unique_trace_id()
        result = ingest_error_handler({
            "exc_type": "AuthError",
            "message": "login failed",
            "frames": [
                {
                    "file": "app/login.py",
                    "line": 42,
                    "function": "authenticate",
                    "code": 'password = "super-secret-123"',
                    "locals": {  # _parse_frames 会丢弃此字段
                        "password": "super-secret-123",
                    },
                },
            ],
            "source": "node-sdk",
            "trace_id": trace_id,
        })

        assert result["saved"] is True
        assert result["frame_count"] == 1

        got = get_trace(result["trace_id"])
        frame = got["frames"][0]

        # code 字段（字符串）走 redact() 正则 → password="***"
        assert "super-secret-123" not in frame["code"], (
            f"code 字段仍含原始敏感值: {frame['code']!r}"
        )
        assert 'password="***"' in frame["code"], (
            f"code 未按预期脱敏: {frame['code']!r}"
        )

        # locals 字段被 _parse_frames 丢弃（不在存储结构中）
        assert "locals" not in frame, "locals 应被 _parse_frames 丢弃"

    def test_ingest_message_with_secrets_is_redacted(self):
        """ingest 上报 message 含敏感模式 → 入库后已脱敏。"""
        trace_id = _unique_trace_id()
        result = ingest_error_handler({
            "exc_type": "AuthError",
            "message": 'token: abc.def.ghi and api_key=sk-xyz',
            "frames": [],
            "trace_id": trace_id,
        })

        got = get_trace(result["trace_id"])
        msg = got["message"]

        assert "abc.def.ghi" not in msg
        assert "sk-xyz" not in msg
        # redact() 实际占位符
        assert 'token="***"' in msg
        assert 'api_key="***"' in msg


class TestSaveNetworkRecordRedacts:
    """save_network_record 入库前对 url/request_body/response_body 走 redact()。"""

    def test_url_and_body_with_secrets_masked(self):
        """network 记录 url 含 token、body 含 password → 入库后脱敏。"""
        trace_id = _unique_trace_id()
        save_network_record(
            record={
                "url": "https://api.example.com/login?token=super-secret",
                "method": "POST",
                "request_body": '{"password":"super-secret-123","user":"alice"}',
                "response_body": '{"token": "eyJxyz", "status": "ok"}',
                "status_code": 200,
            },
            trace_id=trace_id,
        )

        records = get_network_records(trace_id)
        assert len(records) == 1
        rec = records[0]

        # url 中 token=xxx → token="***"
        assert "super-secret" not in rec["url"]
        assert 'token="***"' in rec["url"], f"url 未按预期脱敏: {rec['url']!r}"

        # request_body 中 "password":"xxx" → "password":"***"
        assert "super-secret-123" not in rec["request_body"]
        assert '"password":"***"' in rec["request_body"]

        # response_body 中 "token": "xxx" → "token":"***"
        assert "eyJxyz" not in rec["response_body"]
        assert '"token":"***"' in rec["response_body"]

        # 非敏感字段保留
        assert rec["method"] == "POST"
        assert rec["status_code"] == 200


class TestSaveUiEventRedacts:
    """save_ui_event 入库前对 payload_json 走 redact()。"""

    def test_payload_json_with_secret_masked(self):
        """ui 事件 payload_json 含 token → 入库后脱敏。"""
        trace_id = _unique_trace_id()
        save_ui_event(
            event={
                "event_type": "click",
                "route_path": "/login",
                "payload_json": '{"token":"abc.def.ghi","user":"alice"}',
            },
            trace_id=trace_id,
        )

        events = get_ui_events(trace_id)
        assert len(events) == 1
        ev = events[0]

        assert "abc.def.ghi" not in ev["payload_json"]
        assert '"token":"***"' in ev["payload_json"], (
            f"payload_json 未按预期脱敏: {ev['payload_json']!r}"
        )

        # 非敏感字段保留
        assert ev["event_type"] == "click"
        assert ev["route_path"] == "/login"


class TestSaveConsoleLogRedacts:
    """save_console_log 入库前对 message 走 redact()。"""

    def test_console_message_with_secret_masked(self):
        """console 日志 message 含 token → 入库后脱敏。"""
        trace_id = _unique_trace_id()
        save_console_log(
            level="error",
            message="auth failed: token: abc.def.ghi",
            trace_id=trace_id,
        )

        logs = get_console_logs(trace_id)
        assert len(logs) == 1
        log = logs[0]

        assert "abc.def.ghi" not in log["message"]
        assert 'token="***"' in log["message"], (
            f"message 未按预期脱敏: {log['message']!r}"
        )
        assert log["level"] == "error"


class TestAddLogRedactsPayload:
    """FIX: A2 —— logs.add_log / add_logs_batch 直写路径的脱敏。

    此前仅 trace_repo 的 save_* 系列在存储边界脱敏，add_log 直接透传
    data（POST /debug 的 request_start 等入口把用户原始 payload 明文入库，
    viewer 角色经 dashboard 即可读取）。
    """

    def test_add_log_payload_sensitive_keys_masked(self):
        """add_log 的 data 含 password/token/复合敏感键 → 入库后掩码。"""
        from app.runtime.core.logs import add_log, get_logs

        request_id = _unique_trace_id("a2log")
        add_log(request_id, "request_start", {
            "user": "alice",
            "password": "plain-secret",
            "refresh_token": "eyJ-compound",
            "nested": {"api_key": "sk-123", "ok": 1},
        })

        entries = get_logs(request_id)
        data = entries[-1]["data"]
        assert data["user"] == "alice"
        assert data["password"] == "***REDACTED***"
        assert data["refresh_token"] == "***REDACTED***"
        assert data["nested"]["api_key"] == "***REDACTED***"
        assert data["nested"]["ok"] == 1

    def test_add_log_string_payload_regex_redacted(self):
        """data 为字符串（JSON 序列化 payload）→ 走 redact() 正则路径。"""
        import json

        from app.runtime.core.logs import add_log, get_logs

        request_id = _unique_trace_id("a2str")
        add_log(request_id, "request_start", json.dumps({
            "client_secret": "cs-1", "note": "hello"
        }))

        entries = get_logs(request_id)
        data = entries[-1]["data"]
        assert "cs-1" not in data
        assert '"client_secret":"***"' in data
        assert "hello" in data

    def test_add_logs_batch_payloads_masked(self):
        """add_logs_batch 批量直写路径同样脱敏。"""
        from app.runtime.core.logs import add_logs_batch, get_logs

        request_id = _unique_trace_id("a2batch")
        add_logs_batch(request_id, [
            ("step_a", {"session_token": "st-1"}),
            ("step_b", {"public_key": "keep-me"}),  # 白名单字段保留
        ])

        entries = get_logs(request_id)
        by_step = {e["step"]: e["data"] for e in entries}
        assert by_step["step_a"]["session_token"] == "***REDACTED***"
        assert by_step["step_b"]["public_key"] == "keep-me"


class TestRedactionNonRegression:
    """脱敏策略的非回归边界用例 —— 验证非敏感数据不被误伤。"""

    def test_normal_frames_and_locals_preserved(self):
        """无敏感字段的 frames/locals 应完整保留，不被误脱敏。"""
        trace_id = _unique_trace_id()
        frames = [
            {
                "file": "app/service.py",
                "line": 100,
                "function": "process",
                "code": "result = compute(input)",
                "locals": {
                    "input": [1, 2, 3],
                    "result": {"status": "ok"},
                    "count": 42,
                },
            }
        ]
        error_id = save_trace(
            exc_type="ValueError",
            message="processing failed at step 3",
            frames=frames,
            extra={"request_id": "req-abc", "user_agent": "Mozilla/5.0"},
            trace_id=trace_id,
        )

        got = get_trace(error_id)
        frame = got["frames"][0]

        # 所有非敏感字段完整保留
        assert frame["file"] == "app/service.py"
        assert frame["line"] == 100
        assert frame["function"] == "process"
        assert frame["code"] == "result = compute(input)"
        assert frame["locals"]["input"] == [1, 2, 3]
        assert frame["locals"]["result"] == {"status": "ok"}
        assert frame["locals"]["count"] == 42

        # extra 非敏感字段保留
        assert got["extra"]["request_id"] == "req-abc"
        assert got["extra"]["user_agent"] == "Mozilla/5.0"

    def test_phone_number_in_message_masked(self):
        """message 含手机号 → redact() 正则脱敏为 ***PHONE***。"""
        trace_id = _unique_trace_id()
        error_id = save_trace(
            exc_type="ValueError",
            message="contact admin at 13800138000 for help",
            frames=[],
            trace_id=trace_id,
        )

        got = get_trace(error_id)
        msg = got["message"]

        # 手机号脱敏为 ***PHONE***（grep 确认 redaction.py:51）
        assert "13800138000" not in msg
        assert "***PHONE***" in msg, f"手机号未脱敏: {msg!r}"


class TestRedactionAllowlist:
    """Phase 2 复合键名脱敏扩展 —— 白名单测试集。

    子串匹配会误伤含敏感子串的正常字段（如 password_hash 含 "password"），
    白名单（内置默认 + 用户配置 redaction_key_allowlist）优先于子串匹配，命中白名单不脱敏。
    """

    def test_password_hash_not_redacted(self):
        """password_hash 含 "password" 子串，但在内置白名单中，不应被脱敏。"""
        trace_id = _unique_trace_id()
        frames = [
            {
                "file": "app/models.py",
                "line": 1,
                "function": "to_dict",
                "code": "",
                "locals": {
                    "password_hash": "$2b$12$abcdef...",
                    "password": "plaintext-secret",
                },
            }
        ]
        error_id = save_trace(
            exc_type="ValueError",
            message="err",
            frames=frames,
            trace_id=trace_id,
        )

        got = get_trace(error_id)
        local_vars = got["frames"][0]["locals"]

        # password_hash 在白名单中，不脱敏
        assert local_vars["password_hash"] == "$2b$12$abcdef..."
        # password 不在白名单中，被脱敏
        assert local_vars["password"] == "***REDACTED***"

    def test_public_key_not_redacted(self):
        """public_key 含 "key" 子串，但在内置白名单中，不应被脱敏。"""
        trace_id = _unique_trace_id()
        frames = [
            {
                "file": "app/crypto.py",
                "line": 1,
                "function": "verify",
                "code": "",
                "locals": {
                    "public_key": "-----BEGIN PUBLIC KEY-----\nMIIB...",
                    "private_key": "-----BEGIN PRIVATE KEY-----\nabc...",
                },
            }
        ]
        error_id = save_trace(
            exc_type="ValueError",
            message="err",
            frames=frames,
            trace_id=trace_id,
        )

        got = get_trace(error_id)
        local_vars = got["frames"][0]["locals"]

        # public_key 在白名单中，不脱敏
        assert local_vars["public_key"] == "-----BEGIN PUBLIC KEY-----\nMIIB..."
        # private_key 含 "key" 且不在白名单中，被脱敏
        assert local_vars["private_key"] == "***REDACTED***"

    def test_key_count_not_redacted(self):
        """key_count 含 "key" 子串，但在内置白名单中，不应被脱敏。"""
        trace_id = _unique_trace_id()
        frames = [
            {
                "file": "app/cache.py",
                "line": 1,
                "function": "stats",
                "code": "",
                "locals": {
                    "key_count": 42,
                    "key_id": "cache-key-001",
                    "key_type": "redis",
                    "api_key": "sk-secret",
                },
            }
        ]
        error_id = save_trace(
            exc_type="ValueError",
            message="err",
            frames=frames,
            trace_id=trace_id,
        )

        got = get_trace(error_id)
        local_vars = got["frames"][0]["locals"]

        # key_count / key_id / key_type 在白名单中，不脱敏
        assert local_vars["key_count"] == 42
        assert local_vars["key_id"] == "cache-key-001"
        assert local_vars["key_type"] == "redis"
        # api_key 含 "key" 但不在白名单中，被脱敏
        assert local_vars["api_key"] == "***REDACTED***"

    def test_nested_allowlist_field_not_redacted(self):
        """嵌套 dict 中白名单字段同样不被脱敏。"""
        trace_id = _unique_trace_id()
        frames = [
            {
                "file": "app/models.py",
                "line": 1,
                "function": "serialize",
                "code": "",
                "locals": {
                    "user": {
                        "password_hash": "hash-value",
                        "public_key": "pub-key-data",
                        "db_password": "should-be-redacted",
                    },
                },
            }
        ]
        error_id = save_trace(
            exc_type="ValueError",
            message="err",
            frames=frames,
            trace_id=trace_id,
        )

        got = get_trace(error_id)
        user_data = got["frames"][0]["locals"]["user"]

        # 白名单字段不脱敏
        assert user_data["password_hash"] == "hash-value"
        assert user_data["public_key"] == "pub-key-data"
        # 非白名单的敏感复合键名被脱敏
        assert user_data["db_password"] == "***REDACTED***"

    def test_user_configured_allowlist_protects_field(self):
        """用户通过 redaction_key_allowlist 配置的自定义白名单字段不被脱敏。"""
        from app.config import settings

        saved = settings.redaction_key_allowlist
        settings.redaction_key_allowlist = "my_secret_counter,custom_token_id"
        try:
            trace_id = _unique_trace_id()
            frames = [
                {
                    "file": "app/app.py",
                    "line": 1,
                    "function": "f",
                    "code": "",
                    "locals": {
                        "my_secret_counter": 100,      # 含 "secret" 但在用户白名单
                        "custom_token_id": "tk-001",  # 含 "token" 但在用户白名单
                        "secret_value": "leak",        # 含 "secret" 不在白名单
                    },
                }
            ]
            error_id = save_trace(
                exc_type="ValueError",
                message="err",
                frames=frames,
                trace_id=trace_id,
            )

            got = get_trace(error_id)
            local_vars = got["frames"][0]["locals"]

            # 用户白名单字段不脱敏
            assert local_vars["my_secret_counter"] == 100
            assert local_vars["custom_token_id"] == "tk-001"
            # 非白名单的敏感字段被脱敏
            assert local_vars["secret_value"] == "***REDACTED***"
        finally:
            settings.redaction_key_allowlist = saved
