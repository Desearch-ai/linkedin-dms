from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Optional


@dataclass(frozen=True)
class ProxyConfig:
    """Per-account proxy configuration.

    Keep it minimal for MVP. Contributors can extend it to support auth, rotation, etc.
    """

    url: str  # e.g. http://user:pass@host:port or socks5://host:port

    def __repr__(self) -> str:
        return "ProxyConfig(url='[REDACTED]')"

    def __str__(self) -> str:
        return self.__repr__()


def _normalize_jsessionid(value: Optional[str]) -> Optional[str]:
    """Strip surrounding quotes that LinkedIn adds to JSESSIONID cookie values."""
    if value is None:
        return None
    stripped = value.strip().strip('"')
    return stripped if stripped else None


def _normalize_header(value: Optional[str]) -> Optional[str]:
    """Trim whitespace from a captured request-header value; empty -> None."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


@dataclass(frozen=True)
class AccountAuth:
    """LinkedIn auth material.

    MVP: accept raw cookie values.

    - li_at is usually the primary session cookie.
    - JSESSIONID is sometimes required for CSRF headers.
    - x_li_track / csrf_token are browser-captured request-header values
      forwarded by the Chrome extension so backend requests match the live
      browser fingerprint (see issue #54). Both are optional; when absent,
      the provider falls back to its built-in defaults.

    IMPORTANT: treat these as secrets; never log.
    """

    li_at: str
    jsessionid: Optional[str] = None
    x_li_track: Optional[str] = None
    csrf_token: Optional[str] = None

    def __post_init__(self) -> None:
        # Normalize quoted JSESSIONID regardless of which input path created this instance.
        object.__setattr__(self, "jsessionid", _normalize_jsessionid(self.jsessionid))
        object.__setattr__(self, "x_li_track", _normalize_header(self.x_li_track))
        object.__setattr__(self, "csrf_token", _normalize_header(self.csrf_token))

    def __repr__(self) -> str:
        return (
            "AccountAuth(li_at='[REDACTED]', jsessionid='[REDACTED]', "
            "x_li_track='[REDACTED]', csrf_token='[REDACTED]')"
        )

    def __str__(self) -> str:
        return self.__repr__()


@dataclass(frozen=True)
class BrowserContext:
    """Extension-captured browser request metadata for a LinkedIn session.

    These values are captured from real browser requests by the Chrome extension
    and are more authentic than server-side reconstructions.

    IMPORTANT: csrf_token is security-sensitive; treat like auth material, never log.
    """

    x_li_track: Optional[str] = None
    csrf_token: Optional[str] = None

    def __repr__(self) -> str:
        return "BrowserContext(x_li_track='[REDACTED]', csrf_token='[REDACTED]')"

    def __str__(self) -> str:
        return self.__repr__()

    def is_empty(self) -> bool:
        return not self.x_li_track and not self.csrf_token


@dataclass(frozen=True)
class Account:
    id: int
    label: str
    created_at: datetime


@dataclass(frozen=True)
class Thread:
    id: int
    account_id: int
    platform_thread_id: str
    title: Optional[str]
    created_at: datetime


@dataclass(frozen=True)
class Message:
    id: int
    account_id: int
    thread_id: int
    platform_message_id: str
    direction: Literal["in", "out"]
    sender: Optional[str]
    text: Optional[str]
    sent_at: datetime
    raw: Optional[dict[str, Any]] = None
