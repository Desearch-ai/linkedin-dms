"""Log redaction: prevent accidental leakage of cookies and auth material.

Provides:
- redact_for_log()        — dict/list deep-redaction for structured data
- redact_string()         — regex scrub for inline secrets in log messages
- SecretRedactingFilter   — logging.Filter that auto-scrubs all log records
- configure_logging()     — one-call setup: format + filter on root logger
"""

from __future__ import annotations

import dataclasses
import logging
import re
from typing import Any

_SECRET_KEYS = frozenset({
    "li_at",
    "jsessionid",
    "auth_json",
    "cookie",
    "cookies",
    "authorization",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "proxy_url",
    "url",  # proxy/config URLs (e.g. ProxyConfig.url)
})

_REDACTED = "[REDACTED]"

# Regex patterns that match inline secret values in log strings.
# Each pattern captures a prefix group we keep and a value group we redact.
# Authorization: run Bearer/Basic pattern first so the full header value is redacted.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(li_at\s*[=:]\s*)([^\s;,\"'}{]+)", re.IGNORECASE),
    re.compile(r"(jsessionid\s*[=:]\s*)([^\s;,\"'}{]+)", re.IGNORECASE),
    re.compile(r"(authorization\s*[=:]\s*)((?:Bearer|Basic|Token)\s+\S+)", re.IGNORECASE),
    re.compile(r"(authorization\s*[=:]\s*)([^\s;,\"'}{]+)", re.IGNORECASE),
    re.compile(r"(password\s*[=:]\s*)([^\s;,\"'}{]+)", re.IGNORECASE),
    re.compile(r"(api_key\s*[=:]\s*)([^\s;,\"'}{]+)", re.IGNORECASE),
    re.compile(r"(token\s*[=:]\s*)([^\s;,\"'}{]+)", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Dict-based redaction (structured data)
# ---------------------------------------------------------------------------

def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _redact_dict(value)
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


def _redact_dict(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in d.items():
        if key.lower() in _SECRET_KEYS:
            out[key] = _REDACTED
        else:
            out[key] = _redact_value(value)
    return out


def redact_for_log(obj: Any) -> Any:
    """Return a copy of obj with secret keys replaced by '[REDACTED]'.

    Works recursively on dicts and lists. Safe to call on any type.
    """
    if isinstance(obj, dict):
        return _redact_dict(obj)
    if isinstance(obj, (list, tuple)):
        return [_redact_value(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# String-based redaction (raw log messages)
# ---------------------------------------------------------------------------

def redact_string(text: str) -> str:
    """Scrub known secret patterns from a plain string.

    Replaces the value portion of patterns like 'li_at=XXXX' or
    'JSESSIONID: XXXX' with [REDACTED], preserving the key prefix.
    """
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1" + _REDACTED, text)
    return text


# ---------------------------------------------------------------------------
# logging.Filter — automatic redaction on every log record
# ---------------------------------------------------------------------------

class SecretRedactingFilter(logging.Filter):
    """Logging filter that scrubs secrets from log messages automatically.

    Attach to any handler or logger to ensure secrets never reach log output,
    even when developers forget to call redact_for_log() manually. Also redacts
    dataclass instances in log args (e.g. AccountAuth) via asdict + redact_for_log.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_string(record.msg)
        if record.args:
            record.args = self._scrub_args(record.args)
        return True

    def _scrub_args(self, args: Any) -> Any:
        if isinstance(args, dict):
            return redact_for_log(args)
        if isinstance(args, (tuple, list)):
            return tuple(self._scrub_single(a) for a in args)
        return args

    def _scrub_single(self, value: Any) -> Any:
        if isinstance(value, str):
            return redact_string(value)
        if isinstance(value, dict):
            return redact_for_log(value)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return redact_for_log(dataclasses.asdict(value))
        return value


# ---------------------------------------------------------------------------
# Logging configuration helper
# ---------------------------------------------------------------------------

def configure_logging(level: int = logging.INFO) -> None:
    """Set up root logger with a sensible format and the SecretRedactingFilter.

    Safe to call multiple times — idempotent.
    """
    root = logging.getLogger()
    if any(isinstance(f, SecretRedactingFilter) for f in root.filters):
        return
    root.setLevel(level)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    root.addHandler(handler)
    root.addFilter(SecretRedactingFilter())
