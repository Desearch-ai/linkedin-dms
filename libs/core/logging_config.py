"""Enhanced logging configuration for linkedin-dms.

This module extends the redaction-aware ``configure_logging()`` helper in
``libs.core.redaction`` with file rotation, JSON structured output, and
environment-variable control.

Usage (one-time setup at application startup)::

    from libs.core.logging_config import setup_logging
    setup_logging()

Then in any module::

    import logging
    logger = logging.getLogger(__name__)
    logger.info("Application started")

Environment variables
---------------------
LOG_LEVEL
    Log level for all handlers: DEBUG, INFO, WARNING, ERROR, CRITICAL.
    Default: INFO.
LOG_FORMAT
    Set to ``json`` to emit structured JSON lines instead of human-readable text.
    Default: human-readable text.
LOG_DIR
    Directory for log files.  Default: ``logs`` (relative to working directory).
    Set to empty string or ``-`` to disable file logging.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from libs.core.redaction import SecretRedactingFilter, configure_logging

__all__ = ["setup_logging", "get_logger"]

_DEFAULT_LOG_DIR = "logs"
_LOG_FILE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_LOG_FILE_BACKUP_COUNT = 5


class _JSONFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        # Let the parent populate exc_text if needed.
        super().format(record)

        entry: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "message": record.getMessage(),
        }
        if record.exc_text:
            entry["exception"] = record.exc_text
        return json.dumps(entry, ensure_ascii=False)


def _make_text_formatter() -> logging.Formatter:
    return logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s [%(name)s] %(module)s:%(funcName)s:%(lineno)d %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def setup_logging(
    level: str | int | None = None,
    log_dir: str | None = None,
    json_format: bool | None = None,
) -> None:
    """Configure the root logger with console + optional rotating file output.

    This function is **idempotent** — subsequent calls with the same parameters
    are no-ops.  Call it once at application startup before any log messages.

    The ``SecretRedactingFilter`` from :mod:`libs.core.redaction` is always
    attached to the root logger, ensuring secrets never leak into log output
    regardless of which handler emits the record.

    Parameters
    ----------
    level:
        Log level.  Overrides the ``LOG_LEVEL`` environment variable.
        Accepts string names (``"DEBUG"``) or ``logging`` constants.
        Defaults to the ``LOG_LEVEL`` env var, or ``INFO`` if not set.
    log_dir:
        Directory for rotating log files.  Pass an empty string or ``"-"`` to
        disable file logging.  Overrides the ``LOG_DIR`` environment variable.
        Defaults to ``"logs"``.
    json_format:
        If ``True``, emit structured JSON lines.  Overrides the ``LOG_FORMAT``
        env var (set it to ``"json"`` to enable).  Defaults to ``False``.
    """
    root = logging.getLogger()

    # Idempotency: skip if we already set up (SecretRedactingFilter present
    # *and* a file handler was already attached when file logging is desired).
    if any(isinstance(f, SecretRedactingFilter) for f in root.filters):
        return

    # --- Resolve parameters from env vars / arguments ---------------------
    if level is None:
        level_str = os.getenv("LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_str, logging.INFO)
    elif isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    if json_format is None:
        json_format = os.getenv("LOG_FORMAT", "").lower() == "json"

    if log_dir is None:
        log_dir = os.getenv("LOG_DIR", _DEFAULT_LOG_DIR)

    disable_files = not log_dir or log_dir.strip() in ("", "-")

    # --- Formatter --------------------------------------------------------
    formatter: logging.Formatter = _JSONFormatter() if json_format else _make_text_formatter()

    # --- Root logger level ------------------------------------------------
    root.setLevel(level)

    # --- Console handler --------------------------------------------------
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    # --- File handlers (rotating) -----------------------------------------
    if not disable_files:
        log_path = Path(log_dir)
        try:
            log_path.mkdir(parents=True, exist_ok=True)

            # Main log: all records at or above the configured level.
            main_handler = logging.handlers.RotatingFileHandler(
                filename=log_path / "linkedin_dms.log",
                maxBytes=_LOG_FILE_MAX_BYTES,
                backupCount=_LOG_FILE_BACKUP_COUNT,
                encoding="utf-8",
            )
            main_handler.setFormatter(formatter)
            root.addHandler(main_handler)

            # Error log: WARNING and above only.
            error_handler = logging.handlers.RotatingFileHandler(
                filename=log_path / "linkedin_dms_error.log",
                maxBytes=_LOG_FILE_MAX_BYTES,
                backupCount=_LOG_FILE_BACKUP_COUNT,
                encoding="utf-8",
            )
            error_handler.setLevel(logging.WARNING)
            error_handler.setFormatter(formatter)
            root.addHandler(error_handler)

        except OSError as exc:
            # Fall back gracefully — log to console only.
            logging.getLogger(__name__).warning(
                "Could not create log directory %s: %s — file logging disabled.",
                log_dir,
                exc,
            )

    # --- Secret-redacting filter ------------------------------------------
    # Attach to the root logger so *every* handler benefits automatically.
    root.addFilter(SecretRedactingFilter())


def get_logger(name: str) -> logging.Logger:
    """Return a named logger.

    Convenience wrapper; identical to ``logging.getLogger(name)`` but signals
    intent to use the project-wide logging setup.
    """
    return logging.getLogger(name)
