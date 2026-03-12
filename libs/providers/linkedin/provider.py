from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from libs.core.models import AccountAuth, ProxyConfig


@dataclass(frozen=True)
class LinkedInThread:
    platform_thread_id: str
    title: Optional[str]
    raw: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class LinkedInMessage:
    platform_message_id: str
    direction: str  # "in" | "out"
    sender: Optional[str]
    text: Optional[str]
    sent_at: datetime
    raw: Optional[dict[str, Any]] = None

@dataclass(frozen=True)
class AuthCheckResult:
    ok: bool
    error: Optional[str] = None

class LinkedInProvider:
    """LinkedIn DM provider.

    This file is the main contribution point.

    Contributors can implement this using:
    - Playwright (recommended): login via cookies and drive LinkedIn messaging UI
    - HTTP scraping: call internal endpoints using cookies + CSRF headers

    IMPORTANT:
    - Do NOT log cookies or auth headers.
    - Do NOT implement CAPTCHA/2FA bypass.
    """

    def __init__(self, *, auth: AccountAuth, proxy: Optional[ProxyConfig] = None):
        self.auth = auth
        self.proxy = proxy

    def list_threads(self) -> list[LinkedInThread]:
        """Return list of DM threads for this account.

        TODO (contributors):
        - Fetch threads from LinkedIn messaging
        - Provide stable `platform_thread_id`
        - Optional: thread title (participant names)

        Return examples:
        - platform_thread_id could be a LinkedIn conversation URN
        """
        raise NotImplementedError

    def fetch_messages(
        self,
        *,
        platform_thread_id: str,
        cursor: Optional[str],
        limit: int = 50,
    ) -> tuple[list[LinkedInMessage], Optional[str]]:
        """Fetch messages for a thread incrementally.

        Args:
          platform_thread_id: stable thread id
          cursor: opaque provider cursor (None = start)
          limit: max messages per call

        TODO (contributors):
        - Decide cursor semantics (e.g. newest timestamp, message id, pagination token)
        - Return messages in chronological order (oldest -> newest) if possible
        - Return next_cursor to continue, or None if fully synced
        """
        raise NotImplementedError

    def send_message(
        self,
        *,
        recipient: str,
        text: str,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Send a DM.

        Args:
          recipient: profile public id / URN / conversation id (define in implementation)
          text: message body
          idempotency_key: optional. If provided, use it to avoid duplicate sends on retries.

        Returns:
          platform_message_id (or provider generated id)

        TODO (contributors):
        - Implement send via UI automation or internal endpoint
        - Add retry/backoff outside provider or inside implementation
        """
        raise NotImplementedError

    def check_auth(self) -> AuthCheckResult:
        """Perform a lightweight auth sanity check.

        MVP behavior:
        - verify required cookie presence
        - optionally verify optional cookie format
        - placeholder for future lightweight LinkedIn request

        IMPORTANT:
        - do not leak cookie values in errors
        """
        if not self.auth.li_at or not self.auth.li_at.strip():
            return AuthCheckResult(ok=False, error="missing li_at cookie")

        # Optional light validation only; do not expose cookie values
        if self.auth.jsessionid is not None and not self.auth.jsessionid.strip():
            return AuthCheckResult(ok=False, error="invalid JSESSIONID cookie")

        # Placeholder success until real provider/network validation is implemented.
        return AuthCheckResult(ok=True, error=None)
