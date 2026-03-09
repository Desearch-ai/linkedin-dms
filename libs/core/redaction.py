"""Log redaction: never log cookies or auth material."""

from __future__ import annotations

import copy
from typing import Any

# Keys whose values are redacted in logs (case-insensitive match)
_SECRET_KEYS = frozenset(
    k.lower()
    for k in (
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
    )
)

_REDACTED = "[REDACTED]"


def _redact_dict(d: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in d.items():
        if k.lower() in _SECRET_KEYS:
            out[k] = _REDACTED
        elif isinstance(v, dict):
            out[k] = _redact_dict(v)
        elif isinstance(v, list):
            out[k] = [_redact_value(x) for x in v]
        else:
            out[k] = v
    return out


def _redact_value(v: Any) -> Any:
    if isinstance(v, dict):
        return _redact_dict(v)
    if isinstance(v, list):
        return [_redact_value(x) for x in v]
    return v


def redact_for_log(obj: Any) -> Any:
    """Return a copy of obj safe to log: secret keys are replaced with [REDACTED].

    Use for request bodies, config, or any dict that may contain li_at, jsessionid, etc.
    """
    if isinstance(obj, dict):
        return _redact_dict(copy.deepcopy(obj))
    if isinstance(obj, (list, tuple)):
        return [_redact_value(x) for x in obj]
    return obj
