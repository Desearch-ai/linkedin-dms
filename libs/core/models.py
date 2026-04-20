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


@dataclass(frozen=True)
class AccountAuth:
    """LinkedIn auth material.

    MVP: accept raw cookie values.

    - li_at is usually the primary session cookie.
    - JSESSIONID is sometimes required for CSRF headers.

    IMPORTANT: treat these as secrets; never log.
    """

    li_at: str
    jsessionid: Optional[str] = None

    def __post_init__(self) -> None:
        # Normalize quoted JSESSIONID regardless of which input path created this instance.
        object.__setattr__(self, "jsessionid", _normalize_jsessionid(self.jsessionid))

    def __repr__(self) -> str:
        return "AccountAuth(li_at='[REDACTED]', jsessionid='[REDACTED]')"

    def __str__(self) -> str:
        return self.__repr__()


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
