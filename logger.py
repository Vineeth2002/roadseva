# logger.py — RoadSeva Structured Logging
# Uses Python stdlib only — no structlog dependency.
# Every log line is JSON so Render/UptimeRobot/grep can parse it easily.

import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

# ── Per-request context (set by middleware on every request) ──────────────────
request_id_var: ContextVar[str] = ContextVar("request_id", default="")
staff_id_var:   ContextVar[str] = ContextVar("staff_id",   default="anonymous")
role_var:       ContextVar[str] = ContextVar("role",       default="")
ip_var:         ContextVar[str] = ContextVar("ip",         default="")

APP_VERSION = "1.0.0"
SERVICE     = "roadseva"


class JsonFormatter(logging.Formatter):
    """Formats every log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        base = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level":      record.levelname,
            "service":    SERVICE,
            "version":    APP_VERSION,
            "logger":     record.name,
            "event":      record.getMessage(),
            "request_id": request_id_var.get() or str(uuid.uuid4())[:8],
            "staff_id":   staff_id_var.get(),
            "role":       role_var.get(),
            "ip":         ip_var.get(),
        }
        # Merge any extra fields passed via extra={...}
        if hasattr(record, "extra_fields"):
            base.update(record.extra_fields)
        if record.exc_info:
            base["exception"] = self.formatException(record.exc_info)
        return json.dumps(base, ensure_ascii=False)


class DevFormatter(logging.Formatter):
    """Human-readable formatter for local development."""
    COLORS = {
        "DEBUG":    "\033[36m",
        "INFO":     "\033[32m",
        "WARNING":  "\033[33m",
        "ERROR":    "\033[31m",
        "CRITICAL": "\033[35m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        ts = datetime.now().strftime("%H:%M:%S")
        rid = request_id_var.get()
        rid_str = f" [{rid}]" if rid else ""
        msg = record.getMessage()
        extra = ""
        if hasattr(record, "extra_fields") and record.extra_fields:
            extra = "  " + "  ".join(f"{k}={v}" for k, v in record.extra_fields.items())
        return f"{color}{ts} {record.levelname:<8}{self.RESET} {record.name}{rid_str}: {msg}{extra}"


def configure_logging(log_level: str = "INFO", json_logs: bool = True) -> None:
    """
    Call once at app startup (inside lifespan in main.py).
    json_logs=True in production (Render), False for local dev.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_logs else DevFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Silence noisy third-party loggers
    for noisy in ("uvicorn.access", "multipart", "python_multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ── Logger factory ────────────────────────────────────────────────────────────

class RoadSevaLogger:
    """Thin wrapper that supports keyword extra fields on every call."""

    def __init__(self, name: str):
        self._log = logging.getLogger(name)

    def _emit(self, level: int, event: str, **kwargs):
        record = self._log.makeRecord(
            self._log.name, level, "(unknown)", 0, event, (), None
        )
        record.extra_fields = kwargs
        self._log.handle(record)

    def debug(self, event: str, **kw):    self._emit(logging.DEBUG,    event, **kw)
    def info(self, event: str, **kw):     self._emit(logging.INFO,     event, **kw)
    def warning(self, event: str, **kw):  self._emit(logging.WARNING,  event, **kw)
    def error(self, event: str, **kw):    self._emit(logging.ERROR,    event, **kw)
    def critical(self, event: str, **kw): self._emit(logging.CRITICAL, event, **kw)


def get_logger(name: str) -> RoadSevaLogger:
    return RoadSevaLogger(name)


# ── Security event helpers ────────────────────────────────────────────────────
# Emit structured events that alerting rules / grep can match on.

_sec = get_logger("security")

def log_login_success(staff_id: str, role: str) -> None:
    _sec.info("login_success", staff_id=staff_id, role=role)

def log_login_failure(staff_id: str, reason: str, ip: str = "") -> None:
    _sec.warning("login_failure", staff_id=staff_id, reason=reason, ip=ip or ip_var.get())

def log_permission_denied(staff_id: str, endpoint: str, required_role: str) -> None:
    _sec.warning("permission_denied", staff_id=staff_id, endpoint=endpoint, required_role=required_role)

def log_photo_rejected(filename: str, reason: str) -> None:
    _sec.warning("photo_rejected", filename=filename, reason=reason)

def log_rate_limit_hit(ip: str, endpoint: str, count: int) -> None:
    _sec.warning("rate_limit_hit", ip=ip, endpoint=endpoint, count=count)