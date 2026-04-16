from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Optional
from pydantic import BaseModel

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

class BrowserHeaders(BaseModel):
    """Real browser request headers captured by the Chrome extension.

    When provided, the LinkedIn provider uses these values in place of
    the hardcoded synthetic equivalents, so the backend request fingerprint
    matches the real browser session and LinkedIn does not challenge/invalidate it.

    All fields are optional — missing fields fall back to the provider defaults.
    """
    user_agent: str | None = None
    x_li_track: str | None = None
    csrf_token: str | None = None
    x_li_page_instance: str | None = None
    x_li_lang: str | None = None

