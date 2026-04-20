"""Cookie import format: parse multiple cookie formats into AccountAuth.

Supported formats:
  1. Header string:  "li_at=abc; JSESSIONID=xyz"
  2. JSON array:     [{"name": "li_at", "value": "abc"}, ...]
     (browser devtools "Copy all as JSON" export)
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from .models import AccountAuth

_COOKIE_PAIR = re.compile(r"([^=;\s]+)\s*=\s*([^;]*)")

_KNOWN_KEYS: dict[str, str] = {
    "li_at": "li_at",
    "jsessionid": "JSESSIONID",
}


def parse_cookie_string(cookie_string: str) -> dict[str, str]:
    """Parse a cookie header string into {normalized_name: value}.

    Accepts formats like:
      "li_at=abc; JSESSIONID=xyz"
      "li_at=abc"
    Only extracts keys relevant to LinkedIn auth (li_at, JSESSIONID).
    """
    result: dict[str, str] = {}
    for match in _COOKIE_PAIR.finditer(cookie_string):
        name = match.group(1).strip()
        value = match.group(2).strip().strip('"')
        normalized = _KNOWN_KEYS.get(name.lower())
        if normalized and value:
            result[normalized] = value
    return result


def parse_cookie_json(cookie_data: list[dict[str, Any]]) -> dict[str, str]:
    """Parse a JSON cookie array (browser devtools export) into {normalized_name: value}.

    Expects a list of objects with at least "name" and "value" keys:
      [{"name": "li_at", "value": "abc"}, {"name": "JSESSIONID", "value": "xyz"}]
    """
    result: dict[str, str] = {}
    for entry in cookie_data:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        value = str(entry.get("value", "")).strip().strip('"')
        normalized = _KNOWN_KEYS.get(name.lower())
        if normalized and value:
            result[normalized] = value
    return result


def detect_and_parse_cookies(raw: str) -> dict[str, str]:
    """Auto-detect cookie format (header string or JSON array) and parse.

    Tries JSON parse first; falls back to header-string parsing.
    """
    stripped = raw.strip()
    if stripped.startswith("["):
        try:
            data = json.loads(stripped)
            if isinstance(data, list):
                return parse_cookie_json(data)
        except (json.JSONDecodeError, TypeError):
            pass
    return parse_cookie_string(stripped)


def validate_li_at(value: str) -> str:
    """Strip whitespace and reject obviously invalid li_at values."""
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("li_at must not be empty")
    if len(cleaned) < 10:
        raise ValueError("li_at value is suspiciously short")
    if " " in cleaned:
        raise ValueError("li_at must not contain spaces")
    return cleaned


def cookies_to_account_auth(cookie_string: str) -> AccountAuth:
    """Build AccountAuth from a cookie string (header or JSON format).

    Requires li_at to be present. JSESSIONID is optional.
    """
    parsed = detect_and_parse_cookies(cookie_string)
    li_at = parsed.get("li_at")
    if not li_at:
        raise ValueError("Cookie input must contain a 'li_at' value")
    li_at = validate_li_at(li_at)
    jsessionid: Optional[str] = parsed.get("JSESSIONID")
    return AccountAuth(li_at=li_at, jsessionid=jsessionid)
