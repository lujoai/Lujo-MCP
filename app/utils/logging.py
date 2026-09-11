"""结构化日志模块"""

import os
import sys
import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.runtime.core.redaction import is_sensitive_key, redact, redact_nested


# logging.LogRecord 标准属性（不应作为 extra 字段注入 JSON）
_LOGRECORD_STANDARD_FIELDS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "lineno", "funcName", "created", "msecs", "relativeCreated",
    "thread", "threadName", "processName", "process", "exc_info", "exc_text",
    "stack_info", "message", "asctime", "taskName",
})


class JSONFormatter(logging.Formatter):
    """JSON 格式日志输出"""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()) or "",
        }

        # 注入全部 extra 字段（trace_id / elapsed_ms / method / path / status / model 等）
        for key, value in record.__dict__.items():
            if key not in _LOGRECORD_STANDARD_FIELDS and key not in log_entry:
                log_entry[key] = (
                    "***REDACTED***"
                    if is_sensitive_key(key)
                    else redact_nested(value)
                )

        # 注入 exception 信息（含 traceback，旧实现丢失 traceback 行）
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = {
                "type": type(record.exc_info[1]).__name__,
                "message": redact(str(record.exc_info[1])) or "",
                "traceback": redact(self.formatException(record.exc_info)) or "",
            }

        return json.dumps(log_entry, ensure_ascii=False, default=str)


class RedactingFormatter(logging.Formatter):
    """文本日志格式器：格式化后再脱敏，避免异常堆栈绕过边界。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record)) or ""


def setup_logging() -> None:
    """初始化全局日志配置"""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    root = logging.getLogger("lujo-mcp")
    root.setLevel(level)

    # MCP stdio shares process stdout with the JSON-RPC protocol. In unified
    # local mode logs must stay on stderr or they corrupt frames seen by the
    # host client. Standalone HTTP keeps the historical stdout behaviour.
    log_stream = sys.stderr if os.environ.get("LUJO_MCP_STDIO_MODE") == "1" else sys.stdout
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(level)

    if settings.log_format == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            RedactingFormatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )

    # 避免重复添加 handler
    if not root.handlers:
        root.addHandler(handler)

    # 降低第三方库日志级别
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)

    root.info("logging initialized", extra={"level": settings.log_level})
