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
        return "ProxyConfig(url=[REDACTED])"


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

    def __repr__(self) -> str:
        return "AccountAuth(li_at=[REDACTED], jsessionid=[REDACTED])"


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
