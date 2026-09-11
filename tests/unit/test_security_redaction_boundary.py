"""B01/B02 regression tests for redaction at storage and response/log boundaries."""

import copy
import json
import logging
import sys

from app.api.debug import debug_echo, debug_run
from app.mcp.tools.debug_api import handler as mcp_debug_handler
from app.runtime.core.redaction import redact_nested
from app.runtime.core.trace_repo import get_network_records, get_trace, save_network_record, save_trace
from app.runtime.core import errors
from app.schemas import DebugRequest
from app.utils.logging import JSONFormatter


def test_redact_nested_parses_nested_json_before_redaction():
    raw = '{"credentials":{"password":{"value":"nested-secret"},"name":"Alice"}}'

    result = redact_nested(raw)

    assert isinstance(result, str)
    parsed = json.loads(result)
    assert parsed["credentials"]["password"] == "***REDACTED***"
    assert parsed["credentials"]["name"] == "Alice"
    assert raw not in result
    assert "nested-secret" not in result


def test_save_trace_redacts_source_before_storage():
    raw_source = 'ingest password="source-secret"'

    trace_id = save_trace("ValueError", "bad input", [], source=raw_source)

    stored = get_trace(trace_id)
    assert stored is not None
    assert stored["source"] != raw_source
    assert "source-secret" not in str(stored)


def test_errors_record_redacts_all_fields_without_mutating_input():
    exc_data = {
        "type": "ValueError",
        "message": 'password="error-secret"',
        "frames": [{"file": "app.py", "line": 1, "function": "f", "locals": {"token": "frame-secret"}}],
        "traceback": 'token="traceback-secret"',
    }
    original = copy.deepcopy(exc_data)

    error_id = errors.record(exc_data, source='source password="source-secret"')

    stored = errors.get_by_id(error_id)
    assert stored is not None
    assert stored["message"] != exc_data["message"]
    assert stored["source"] != 'source password="source-secret"'
    assert "error-secret" not in str(stored)
    assert "frame-secret" not in str(stored)
    assert "traceback-secret" not in str(stored)
    assert exc_data == original


def test_network_json_payload_is_redacted_without_mutating_input():
    raw_body = '{"credentials":{"token":{"value":"network-secret"},"name":"Alice"}}'
    record = {
        "method": "POST",
        "url": "https://example.test/submit?access_token=url-secret&next=/home",
        "request_body": raw_body,
        "response_body": "ok",
    }
    original = copy.deepcopy(record)
    trace_id = save_trace("NetworkError", "request failed", [])

    save_network_record(record, trace_id=trace_id)

    stored = get_network_records(trace_id)[0]
    assert "url-secret" not in stored["url"]
    parsed_body = json.loads(stored["request_body"])
    assert parsed_body["credentials"]["token"] == "***REDACTED***"
    assert parsed_body["credentials"]["name"] == "Alice"
    assert record == original


def test_mcp_debug_response_redacts_echo_without_mutating_payload():
    payload = {"password": "mcp-secret", "nested": {"token": "nested-secret"}, "name": "Alice"}
    original = copy.deepcopy(payload)

    output = mcp_debug_handler({"payload": payload})

    assert output["result"]["echo"]["password"] == "***REDACTED***"
    assert output["result"]["echo"]["nested"]["token"] == "***REDACTED***"
    assert output["result"]["echo"]["name"] == "Alice"
    assert payload == original
    assert "mcp-secret" not in json.dumps(output, ensure_ascii=False, default=str)


def test_http_debug_response_redacts_echo_without_mutating_payload():
    payload = {"password": "http-secret", "name": "Alice"}
    original = copy.deepcopy(payload)

    response = debug_run(DebugRequest(payload=payload))

    assert response.result["echo"]["password"] == "***REDACTED***"
    assert response.result["echo"]["name"] == "Alice"
    assert payload == original
    assert "http-secret" not in json.dumps(response.model_dump(), ensure_ascii=False, default=str)


def test_debug_echo_response_redacts_received_body_without_mutating_input(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "debug_endpoints_enabled", True)
    body = {"password": "echo-secret", "name": "Alice"}
    original = copy.deepcopy(body)

    response = debug_echo(body)

    assert response["received"]["password"] == "***REDACTED***"
    assert response["received"]["name"] == "Alice"
    assert body == original


def test_json_formatter_redacts_message_extra_and_exception():
    record = logging.LogRecord(
        "security-test",
        logging.ERROR,
        __file__,
        1,
        'request password="message-secret"',
        (),
        None,
    )
    record.payload = {"token": "extra-secret", "name": "Alice"}
    try:
        raise ValueError('token="exception-secret"')
    except ValueError:
        record.exc_info = sys.exc_info()

    rendered = json.loads(JSONFormatter().format(record))
    serialized = json.dumps(rendered, ensure_ascii=False, default=str)

    assert "message-secret" not in serialized
    assert "extra-secret" not in serialized
    assert "exception-secret" not in serialized
    assert rendered["payload"]["token"] == "***REDACTED***"
    assert rendered["payload"]["name"] == "Alice"
    assert rendered["exception"]["type"] == "ValueError"
    assert rendered["exception"]["traceback"]


def test_plain_formatter_redacts_message_and_traceback():
    from app.config import settings
    from app.utils import logging as logging_module

    record = logging.LogRecord(
        "security-test",
        logging.ERROR,
        __file__,
        1,
        'request token="plain-secret"',
        (),
        None,
    )
    try:
        raise RuntimeError('password="traceback-secret"')
    except RuntimeError:
        record.exc_info = sys.exc_info()

    logger = logging.getLogger("lujo-mcp")
    saved_handlers = logger.handlers[:]
    saved_format = settings.log_format
    try:
        logger.handlers.clear()
        settings.log_format = "text"
        logging_module.setup_logging()
        rendered = logger.handlers[0].formatter.format(record)
    finally:
        logger.handlers.clear()
        logger.handlers.extend(saved_handlers)
        settings.log_format = saved_format

    assert "plain-secret" not in rendered
    assert "traceback-secret" not in rendered
    assert "RuntimeError" in rendered
