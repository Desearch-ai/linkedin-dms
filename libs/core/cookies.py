"""Cookie import format: parse cookie strings into AccountAuth."""

from __future__ import annotations

import re
from .models import AccountAuth


# Cookie name -> value; we care about li_at and JSESSIONID (case-insensitive per RFC)
COOKIE_PAIR = re.compile(r"\s*([^=;]+)\s*=\s*([^;]*)\s*")


def parse_cookie_string(cookie_string: str) -> dict[str, str]:
    """Parse a cookie header-style string into name -> value.

    Accepts: "li_at=abc; JSESSIONID=xyz" or "li_at=abc"
    Names are normalized to the case used in our model (li_at, JSESSIONID).
    """
    out: dict[str, str] = {}
    for m in COOKIE_PAIR.finditer(cookie_string.strip()):
        name = m.group(1).strip()
        value = m.group(2).strip().strip('"')
        if not name:
            continue
        key = name.lower()
        if key == "li_at":
            out["li_at"] = value
        elif key == "jsessionid":
            out["JSESSIONID"] = value
    return out


def cookies_to_account_auth(cookie_string: str) -> AccountAuth:
    """Parse a cookie string and build AccountAuth.

    Requires li_at. JSESSIONID is optional.
    Raises ValueError if li_at is missing.
    """
    parsed = parse_cookie_string(cookie_string)
    li_at = parsed.get("li_at")
    if not li_at:
        raise ValueError("Cookie string must include li_at")
    jsessionid = parsed.get("JSESSIONID")
    return AccountAuth(li_at=li_at, jsessionid=jsessionid or None)
