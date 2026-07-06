#!/usr/bin/env python3
"""
Structured logging for z_ai_multi_account.

Provides:
- ``get_logger(name)`` - returns a configured ``logging.Logger`` that writes
  both to the console (via rich, if available) and to a rotating JSONL file
  under ``logs/`` for machine-readable post-mortems.
- ``log_event(email, event, **fields)`` - convenience for the rotation /
  account-management events the rest of the codebase emits. Keeps backward
  compatibility with the old freeform rotation_log.txt line writer while
  also emitting structured records.

Design goals:
- Never crash the caller: a logging failure is swallowed.
- Idempotent: calling ``get_logger`` multiple times is cheap.
- Configurable via ``config`` (level, structured_logging toggle, log dir).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from rich.console import Console

import config

console = Console()

_CONFIGURED: Dict[str, bool] = {}
_DEFAULT_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


class JsonLineFormatter(logging.Formatter):
    """One JSON object per log line - easy to grep / ship."""

    KEEP_EXTRAS = (
        "email", "event", "reason", "provider", "category",
        "tokens", "request_count", "duration_sec", "status", "error_type",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in self.KEEP_EXTRAS:
            if key in record.__dict__:
                payload[key] = record.__dict__[key]
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _ensure_handlers(logger: logging.Logger, name: str) -> None:
    if _CONFIGURED.get(name):
        return

    level_name = str(config.get("log_level", "INFO")).upper()
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    logger.propagate = False

    # Console handler - keep it light so CLI stays readable.
    if not logger.handlers:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(_DEFAULT_FMT))
        logger.addHandler(stream)

    # File handlers only when structured logging is on.
    if bool(config.get("structured_logging", True)):
        try:
            log_dir = config.logs_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            # JSONL rolling file (machine-readable).
            json_handler = logging.handlers.RotatingFileHandler(
                log_dir / "events.jsonl",
                maxBytes=2_000_000, backupCount=5, encoding="utf-8",
            )
            json_handler.setFormatter(JsonLineFormatter())
            json_handler.setLevel(logging.DEBUG)
            logger.addHandler(json_handler)
        except OSError:
            # Read-only / weird FS: skip file logging silently.
            pass

    _CONFIGURED[name] = True


def get_logger(name: str = "zai") -> logging.Logger:
    logger = logging.getLogger(name)
    _ensure_handlers(logger, name)
    return logger


# ------------------------------------------------------------
# Event helpers (backward compatible with the old log_event signature)
# ------------------------------------------------------------

def log_event(
    email: str,
    event: str,
    reason: str = "",
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """
    Emit a structured event.

    Preserves the historical (email, event, reason) tuple for callers like
    ``account_router.log_event`` while adding arbitrary structured fields.
    Also writes a human-readable line to logs/rotation_log.txt for users who
    grep that file.
    """
    logger = get_logger("zai.router")

    extra: Dict[str, Any] = {"email": email, "event": event}
    if reason:
        extra["reason"] = reason
    # Filter reserved logging kwargs.
    safe_fields = {k: v for k, v in fields.items() if k not in ("msg", "levelno")}
    extra.update(safe_fields)

    try:
        logger.log(level, f"{event} | {email} | {reason}".rstrip(" |"), extra=extra)
    except Exception:
        # Logging must never raise.
        try:
            logger.log(level, f"{event} | {email} | {reason}".rstrip(" |"))
        except Exception:
            pass

    # Append to legacy plain-text log for backward compatibility.
    try:
        log_dir = config.logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {event} | {email}"
        if reason:
            line += f" | {reason}"
        with open(log_dir / "rotation_log.txt", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass

    # Echo to console (kept dim like the original implementation).
    try:
        line = f"{event} | {email}"
        if reason:
            line += f" | {reason}"
        console.print(f"[dim]{line}[/dim]")
    except Exception:
        pass


def log_exception(logger: logging.Logger, exc: BaseException, context: str = "") -> None:
    """Structured exception log with type + context. Swallows logging errors."""
    try:
        logger.error(
            f"{context or 'exception'}: {exc}",
            extra={"error_type": type(exc).__name__},
            exc_info=exc,
        )
    except Exception:
        try:
            logger.error(f"{context or 'exception'}: {exc}", exc_info=exc)
        except Exception:
            pass


__all__ = ["get_logger", "log_event", "log_exception", "JsonLineFormatter"]
