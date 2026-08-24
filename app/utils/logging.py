"""结构化日志模块"""

import sys
import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.config import settings


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
            "message": record.getMessage(),
        }

        # 注入全部 extra 字段（trace_id / elapsed_ms / method / path / status / model 等）
        for key, value in record.__dict__.items():
            if key not in _LOGRECORD_STANDARD_FIELDS and key not in log_entry:
                log_entry[key] = value

        # 注入 exception 信息（含 traceback，旧实现丢失 traceback 行）
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = {
                "type": type(record.exc_info[1]).__name__,
                "message": str(record.exc_info[1]),
                "traceback": self.formatException(record.exc_info),
            }

        return json.dumps(log_entry, ensure_ascii=False, default=str)


def setup_logging() -> None:
    """初始化全局日志配置"""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    root = logging.getLogger("lujo-mcp")
    root.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    if settings.log_format == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
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
