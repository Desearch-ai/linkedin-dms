from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import httpx

from libs.core.models import AccountAuth, ProxyConfig

logger = logging.getLogger(__name__)

_MESSAGING_URL = "https://www.linkedin.com/voyager/api/messaging/conversations"

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/vnd.linkedin.normalized+json+2.1",
    "x-restli-protocol-version": "2.0.0",
    "x-li-track": '{"clientVersion":"1.13.8953","osName":"web","timezoneOffset":4,"deviceFormFactor":"DESKTOP"}',
    "x-li-page-instance": "urn:li:page:d_flagship3_messaging",
}

_MIN_SEND_INTERVAL_S = 2.0
_MAX_NETWORK_RETRIES = 3
_NETWORK_RETRY_DELAY_S = 5.0
_MAX_RATE_LIMIT_RETRIES = 5
_BACKOFF_START_S = 30.0
_BACKOFF_MAX_S = 900.0  # 15 min


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


def _extract_message_id(data: dict[str, Any]) -> str:
    """Best-effort extraction of a stable message ID from LinkedIn's response."""
    value = data.get("value", data)
    for key in ("eventUrn", "backendUrn", "conversationUrn", "id", "entityUrn"):
        if key in value and value[key]:
            return str(value[key])
    return f"li-send-{uuid.uuid4().hex[:16]}"


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
        self._sent_keys: dict[str, str] = {}
        self._last_send_ts: float = 0.0

    # ------------------------------------------------------------------
    # Shared helpers (reusable by list_threads / fetch_messages later)
    # ------------------------------------------------------------------

    def _build_headers(self) -> dict[str, str]:
        csrf_token = self.auth.jsessionid or ""
        return {**_BASE_HEADERS, "csrf-token": csrf_token}

    def _get_cookies(self) -> dict[str, str]:
        cookies: dict[str, str] = {"li_at": self.auth.li_at}
        if self.auth.jsessionid:
            cookies["JSESSIONID"] = self.auth.jsessionid
        return cookies

    def _proxy_url(self) -> Optional[str]:
        return self.proxy.url if self.proxy else None

    def _enforce_send_interval(self) -> None:
        elapsed = time.monotonic() - self._last_send_ts
        remaining = _MIN_SEND_INTERVAL_S - elapsed
        if remaining > 0:
            time.sleep(remaining)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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
        """Send a DM to a LinkedIn recipient.

        Args:
          recipient: profile URN (urn:li:member:<id>) or conversation id.
          text: message body.
          idempotency_key: if provided, prevents duplicate sends within this
              provider instance's lifetime.

        Returns:
          platform_message_id extracted from the LinkedIn response (or a
          generated fallback id).

        Raises:
          PermissionError: on 401 (session expired) or 403 (forbidden).
          ConnectionError: after exhausting network retries.
          RuntimeError: after exhausting rate-limit back-off retries.
          httpx.HTTPStatusError: on unexpected HTTP errors.
        """
        if idempotency_key and idempotency_key in self._sent_keys:
            logger.info("Idempotency cache hit — returning cached message id")
            return self._sent_keys[idempotency_key]

        self._enforce_send_interval()

        headers = {
            **self._build_headers(),
            "Content-Type": "application/json",
            "x-restli-method": "CREATE",
        }
        payload = {
            "keyVersion": "LEGACY_INBOX",
            "conversationCreate": {
                "eventCreate": {
                    "value": {
                        "com.linkedin.voyager.messaging.create.MessageCreate": {
                            "attributedBody": {"text": text, "attributes": []},
                            "attachments": [],
                        }
                    }
                },
                "recipients": [recipient],
                "subtype": "MEMBER_TO_MEMBER",
            },
        }

        network_failures = 0
        rate_limit_hits = 0
        last_network_exc: Optional[Exception] = None

        while True:
            try:
                with httpx.Client(proxy=self._proxy_url(), timeout=30.0) as client:
                    resp = client.post(
                        _MESSAGING_URL,
                        json=payload,
                        headers=headers,
                        cookies=self._get_cookies(),
                    )
                self._last_send_ts = time.monotonic()
            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                network_failures += 1
                last_network_exc = exc
                if network_failures >= _MAX_NETWORK_RETRIES:
                    raise ConnectionError(
                        f"Send failed after {network_failures} network retries"
                    ) from exc
                logger.warning(
                    "Network error (attempt %d/%d), retrying in %.0fs",
                    network_failures,
                    _MAX_NETWORK_RETRIES,
                    _NETWORK_RETRY_DELAY_S,
                )
                time.sleep(_NETWORK_RETRY_DELAY_S)
                continue

            if resp.status_code in (429, 999):
                rate_limit_hits += 1
                if rate_limit_hits > _MAX_RATE_LIMIT_RETRIES:
                    raise RuntimeError(
                        f"Rate-limited {rate_limit_hits} times, giving up"
                    )
                backoff = min(
                    _BACKOFF_START_S * (2 ** (rate_limit_hits - 1)), _BACKOFF_MAX_S
                )
                logger.warning(
                    "Rate limited (HTTP %d, attempt %d/%d), backing off %.0fs",
                    resp.status_code,
                    rate_limit_hits,
                    _MAX_RATE_LIMIT_RETRIES,
                    backoff,
                )
                time.sleep(backoff)
                continue

            if resp.status_code == 401:
                raise PermissionError(
                    "LinkedIn session expired (HTTP 401). Re-authenticate."
                )

            if resp.status_code == 403:
                raise PermissionError("LinkedIn rejected the request (HTTP 403).")

            resp.raise_for_status()

            data = resp.json()
            platform_message_id = _extract_message_id(data)
            logger.info("Message sent successfully (id=%s)", platform_message_id)

            if idempotency_key:
                self._sent_keys[idempotency_key] = platform_message_id

            return platform_message_id

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

        if self.auth.jsessionid is not None and not self.auth.jsessionid.strip():
            return AuthCheckResult(ok=False, error="invalid JSESSIONID cookie")

        return AuthCheckResult(ok=True, error=None)
