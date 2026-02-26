"""
app/core/logging.py

Configures logging for the entire application.

Two formatters:
  - Console (stdout): Clean, human-readable multi-line format for terminals.
  - File (logs/app.log): Structured JSON, one object per line, for
    machine parsing (Datadog, CloudWatch, grep, etc.)

Log file: logs/app.log (daily rotation, 7-day retention)

Windows multi-worker note:
  When running ``uvicorn --workers N`` (N > 1) on Windows, each worker
  process opens its own file handler to the same log file.  At midnight
  the TimedRotatingFileHandler tries ``os.rename(app.log, app.log.<date>)``
  but Windows blocks the rename when another process holds the file open,
  raising ``PermissionError [WinError 32]``.

  ``WindowsSafeTimedRotatingFileHandler`` catches this and lets the
  rollover proceed so logging never breaks.
"""
import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "app.log"

# Extra keys that should never appear in log output
_BUILTIN_ATTRS = {
    "args", "asctime", "created", "exc_info", "exc_text",
    "filename", "funcName", "levelname", "levelno", "lineno",
    "message", "module", "msecs", "msg", "name", "pathname",
    "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
}

# Keys we show prominently in the console; everything else goes to "details"
_HIGHLIGHT_KEYS = {
    "event", "message_id", "direction", "from_number", "to_number",
    "body_length", "body_preview", "account_id", "extension_id",
    "new_message_ids", "message_count", "attempt", "max_retries",
    "zapier_status_code", "status_code", "error", "reason",
}


# -----------------------------------------------------------------
# Windows-safe file rotation handler
# -----------------------------------------------------------------

class WindowsSafeTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """
    Drop-in replacement for TimedRotatingFileHandler that survives
    multi-worker deployments on Windows.
    """

    def rotate(self, source: str, dest: str) -> None:
        try:
            super().rotate(source, dest)
        except PermissionError:
            pass
        except FileNotFoundError:
            pass


# -----------------------------------------------------------------
# Console Formatter  (human-readable, multi-line)
# -----------------------------------------------------------------

class ConsoleFormatter(logging.Formatter):
    """
    Clean, readable terminal output.

    Format:
      [HH:MM:SS] LEVEL  | message
                         | key=value  key=value ...

    Level is color-coded via ANSI where supported.
    """

    LEVEL_TAGS = {
        "DEBUG":    "DEBUG",
        "INFO":     "INFO ",
        "WARNING":  "WARN ",
        "ERROR":    "ERROR",
        "CRITICAL": "CRIT ",
    }

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        tag = self.LEVEL_TAGS.get(record.levelname, record.levelname.ljust(5))

        # Gather extra fields
        extras = {}
        for key, value in record.__dict__.items():
            if key not in _BUILTIN_ATTRS and key in _HIGHLIGHT_KEYS:
                extras[key] = value

        lines = []
        lines.append(f"[{ts}] {tag} | {record.getMessage()}")

        if extras:
            parts = []
            for k, v in extras.items():
                parts.append(f"{k}={v}")
            detail_str = "  ".join(parts)
            lines.append(f"{'':>16} | {detail_str}")

        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            for exc_line in record.exc_text.splitlines():
                lines.append(f"{'':>16} | {exc_line}")

        return "\n".join(lines)


# -----------------------------------------------------------------
# JSON Formatter  (structured, machine-parseable — for log files)
# -----------------------------------------------------------------

class JSONFormatter(logging.Formatter):
    """
    Emits one JSON object per log line.
    Fields: timestamp_utc, level, logger, message, + any extras.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _BUILTIN_ATTRS:
                log_entry[key] = value

        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, default=str)


# -----------------------------------------------------------------
# Setup
# -----------------------------------------------------------------

def setup_logging(log_level: str = "INFO") -> None:
    """
    Call once at application startup (inside the lifespan handler).
    Sets up:
      - stdout handler  (clean readable format)
      - file handler    (JSON, daily rotation, 7 backups)
    """
    LOG_DIR.mkdir(exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level, logging.INFO))

    # Remove any default handlers
    root_logger.handlers.clear()

    # -- Stdout: human-readable -----------------------------------------
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(ConsoleFormatter())
    root_logger.addHandler(stream_handler)

    # -- File: structured JSON ------------------------------------------
    file_handler = WindowsSafeTimedRotatingFileHandler(
        filename=LOG_FILE,
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
        utc=True,
    )
    file_handler.setFormatter(JSONFormatter())
    root_logger.addHandler(file_handler)

    # Silence noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Convenience wrapper."""
    return logging.getLogger(name)
