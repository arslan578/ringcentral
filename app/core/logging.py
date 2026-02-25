"""
app/core/logging.py

Configures structured JSON logging for the entire application.
Uses Python's standard `logging` module with a custom JSON formatter
so logs are machine-parseable in production (Datadog, CloudWatch, etc.)
and human-readable in development.

Log file: logs/app.log (daily rotation, 7-day retention)
"""
import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "app.log"


class JSONFormatter(logging.Formatter):
    """
    Emits one JSON object per log line.
    Fields: timestamp_utc, level, logger, message, + any extras passed via `extra={}`.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Merge any extra fields supplied via logger.info(..., extra={...})
        for key, value in record.__dict__.items():
            if key not in {
                "args", "asctime", "created", "exc_info", "exc_text",
                "filename", "funcName", "levelname", "levelno", "lineno",
                "message", "module", "msecs", "msg", "name", "pathname",
                "process", "processName", "relativeCreated", "stack_info",
                "thread", "threadName", "taskName",
            }:
                log_entry[key] = value

        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, default=str)


def setup_logging(log_level: str = "INFO") -> None:
    """
    Call once at application startup (inside the lifespan handler).
    Sets up:
      - stdout handler (JSON in production, readable in dev)
      - rotating file handler (logs/app.log, daily, 7 backups)
    """
    LOG_DIR.mkdir(exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level, logging.INFO))

    # Remove any default handlers
    root_logger.handlers.clear()

    json_formatter = JSONFormatter()

    # ── Stdout handler ─────────────────────────────────────────────
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(json_formatter)
    root_logger.addHandler(stream_handler)

    # ── Rotating file handler ──────────────────────────────────────
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=LOG_FILE,
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
        utc=True,
    )
    file_handler.setFormatter(json_formatter)
    root_logger.addHandler(file_handler)

    # Silence noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Convenience wrapper — use throughout the app instead of logging.getLogger()."""
    return logging.getLogger(name)
