from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import httpx

from libs.core.models import AccountAuth, ProxyConfig

logger = logging.getLogger(__name__)

# LinkedIn internal API base
_VOYAGER_BASE = "https://www.linkedin.com/voyager/api"

# Headers that LinkedIn requires on every Voyager request.
# Without these you get 403 or status 999.
_STATIC_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "accept": "application/vnd.linkedin.normalized+json+2.1",
    "x-restli-protocol-version": "2.0.0",
    "x-li-track": json.dumps({
        "clientVersion": "1.13.8953",
        "osName": "web",
        "timezoneOffset": 4,
        "deviceFormFactor": "DESKTOP",
    }),
    "x-li-page-instance": "urn:li:page:d_flagship3_messaging",
}

# Default page size for conversation listing
_DEFAULT_COUNT = 20

# Safety limit to prevent runaway pagination on very large inboxes
_MAX_PAGES = 50  # 50 * 20 = 1000 threads max


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


def _extract_thread_title(element: dict[str, Any], included: list[dict[str, Any]]) -> Optional[str]:
    """Try to build a human-readable title from conversation participants.

    LinkedIn stores participant references in the conversation element and
    resolves them in the top-level 'included' array. We look for miniProfile
    entities that match this conversation's participants.
    """
    # Collect participant URNs from the conversation element
    participant_urns: list[str] = []
    for p in element.get("participants", []):
        urn = (
            p.get("*com.linkedin.voyager.messaging.MessagingMember")
            or p.get("participantUrn")
            or p.get("entityUrn", "")
        )
        if urn:
            participant_urns.append(urn)

    if not participant_urns:
        return None

    # Match against included miniProfile entities
    names: list[str] = []
    for inc in included:
        entity_urn = inc.get("entityUrn", "")
        if entity_urn not in participant_urns:
            continue
        first = inc.get("firstName", "")
        last = inc.get("lastName", "")
        full = f"{first} {last}".strip()
        if full:
            names.append(full)

    return ", ".join(names) if names else None


def _parse_threads(data: dict[str, Any]) -> list[LinkedInThread]:
    """Parse the Voyager conversations response into LinkedInThread objects."""
    elements = data.get("elements", [])
    included = data.get("included", [])
    threads: list[LinkedInThread] = []
    for elem in elements:
        entity_urn = elem.get("entityUrn", "")
        if not entity_urn:
            continue
        title = _extract_thread_title(elem, included)
        threads.append(LinkedInThread(
            platform_thread_id=entity_urn,
            title=title,
            raw=elem,
        ))
    return threads


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

    def _build_headers(self) -> dict[str, str]:
        """Build request headers including the CSRF token from JSESSIONID."""
        headers = dict(_STATIC_HEADERS)
        if self.auth.jsessionid:
            headers["csrf-token"] = self.auth.jsessionid
        return headers

    def _build_cookies(self) -> dict[str, str]:
        """Build the cookie dict for requests. Never log the return value."""
        cookies: dict[str, str] = {"li_at": self.auth.li_at}
        if self.auth.jsessionid:
            cookies["JSESSIONID"] = self.auth.jsessionid
        return cookies

    def _get_proxy_url(self) -> Optional[str]:
        return self.proxy.url if self.proxy else None

    def list_threads(self) -> list[LinkedInThread]:
        """Fetch all DM conversation threads with pagination.

        Calls the Voyager conversations endpoint, page by page, until
        LinkedIn returns fewer results than the requested count.
        """
        if not self.auth.jsessionid:
            raise ValueError(
                "JSESSIONID is required for LinkedIn API requests (used as CSRF token). "
                "Re-create the account with both li_at and JSESSIONID cookies."
            )

        headers = self._build_headers()
        cookies = self._build_cookies()
        proxy_url = self._get_proxy_url()

        all_threads: list[LinkedInThread] = []
        start = 0
        page = 0

        with httpx.Client(proxy=proxy_url, timeout=30.0) as client:
            while page < _MAX_PAGES:
                resp = client.get(
                    f"{_VOYAGER_BASE}/messaging/conversations",
                    params={
                        "keyVersion": "LEGACY_INBOX",
                        "q": "participants",
                        "start": start,
                        "count": _DEFAULT_COUNT,
                    },
                    headers=headers,
                    cookies=cookies,
                )
                resp.raise_for_status()
                data = resp.json()
                page_threads = _parse_threads(data)
                all_threads.extend(page_threads)

                # Stop when we got fewer than a full page (last page)
                paging = data.get("paging", {})
                returned = len(data.get("elements", []))
                total = paging.get("total")

                if returned < _DEFAULT_COUNT:
                    break
                # If LinkedIn tells us the total, stop when we've seen them all
                if total is not None and start + returned >= total:
                    break

                start += _DEFAULT_COUNT
                page += 1

        if page >= _MAX_PAGES:
            logger.warning(
                "Reached max page limit (%d); %d threads fetched — some threads may be missing",
                _MAX_PAGES, len(all_threads),
            )
        logger.info("Fetched %d threads across %d pages", len(all_threads), page)
        return all_threads

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
