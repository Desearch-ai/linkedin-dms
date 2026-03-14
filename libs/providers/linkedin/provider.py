from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import httpx

from libs.core.models import AccountAuth, ProxyConfig

logger = logging.getLogger(__name__)

_VOYAGER_BASE = "https://www.linkedin.com/voyager/api"
_VOYAGER_TIMEOUT_S = 30.0
_EVENTS_PAGE_SIZE_MIN = 1
_EVENTS_PAGE_SIZE_MAX = 500


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
        self._my_profile_id: Optional[str] = None

    def _build_headers(self) -> dict[str, str]:
        """Build Voyager API headers. JSESSIONID must be set for CSRF."""
        if not self.auth.jsessionid or not self.auth.jsessionid.strip():
            raise ValueError("JSESSIONID cookie required for Voyager API (CSRF)")
        return {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/vnd.linkedin.normalized+json+2.1",
            "x-restli-protocol-version": "2.0.0",
            "x-li-track": '{"clientVersion":"1.13.8953","osName":"web","timezoneOffset":4,"deviceFormFactor":"DESKTOP"}',
            "x-li-page-instance": "urn:li:page:d_flagship3_messaging",
            "csrf-token": self.auth.jsessionid,
        }

    def _proxy_url(self) -> Optional[str]:
        """Return proxy URL for httpx, or None."""
        if not self.proxy or not self.proxy.url.strip():
            return None
        return self.proxy.url

    def _get_my_profile_id(self, client: httpx.Client) -> str:
        """Fetch and cache current user publicIdentifier from Voyager /me."""
        if self._my_profile_id is not None:
            return self._my_profile_id
        headers = self._build_headers()
        cookies = {"li_at": self.auth.li_at, "JSESSIONID": self.auth.jsessionid or ""}
        resp = client.get(
            f"{_VOYAGER_BASE}/me",
            headers=headers,
            cookies=cookies,
        )
        resp.raise_for_status()
        data = resp.json()
        # Voyager /me may return publicIdentifier at top level or under miniProfile
        pid = data.get("publicIdentifier")
        if not pid and isinstance(data.get("miniProfile"), dict):
            pid = data["miniProfile"].get("publicIdentifier")
        if not pid or not str(pid).strip():
            raise ValueError("Could not resolve current user publicIdentifier from /me")
        self._my_profile_id = str(pid).strip()
        return self._my_profile_id

    def _parse_event_to_message(self, event: dict[str, Any], my_profile_id: str) -> Optional[LinkedInMessage]:
        """Parse one Voyager event into LinkedInMessage. Returns None if event is malformed."""
        try:
            from_obj = event.get("from") or {}
            member = from_obj.get("member") or {}
            mini = member.get("miniProfile") or {}
            sender_identifier = mini.get("publicIdentifier")
            if sender_identifier is None:
                return None
            direction = "out" if str(sender_identifier).strip() == my_profile_id else "in"
        except (TypeError, AttributeError):
            return None

        created_at = event.get("createdAt")
        if created_at is None:
            return None
        try:
            ts_ms = int(created_at)
        except (TypeError, ValueError):
            return None
        sent_at = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)

        platform_message_id = event.get("entityUrn") or event.get("id")
        if platform_message_id is None:
            return None
        platform_message_id = str(platform_message_id)

        event_content = event.get("eventContent") or {}
        text = event_content.get("body") or None
        if not text and isinstance(event_content.get("attributedBody"), dict):
            text = event_content["attributedBody"].get("text")
        if not isinstance(text, str):
            text = None
        sender = str(sender_identifier) if sender_identifier else None

        return LinkedInMessage(
            platform_message_id=platform_message_id,
            direction=direction,
            sender=sender,
            text=text,
            sent_at=sent_at,
            raw=event,
        )

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
          platform_thread_id: conversation id (URN or id) for the thread.
          cursor: millisecond timestamp of oldest message from previous page; None for first page.
          limit: max messages per call.

        Returns:
          (messages, next_cursor). Messages in chronological order (oldest first).
          next_cursor is oldest message's createdAt ms if more pages exist, else None.

        Raises:
          ValueError: If limit is out of range (1..500) or JSESSIONID missing.
          httpx.HTTPStatusError: On 4xx/5xx from LinkedIn (caller may retry or back off).
        """
        if not (_EVENTS_PAGE_SIZE_MIN <= limit <= _EVENTS_PAGE_SIZE_MAX):
            raise ValueError(
                f"limit must be between {_EVENTS_PAGE_SIZE_MIN} and {_EVENTS_PAGE_SIZE_MAX}, got {limit}"
            )
        params: dict[str, str | int] = {
            "keyVersion": "LEGACY_INBOX",
            "q": "events",
            "count": limit,
        }
        if cursor:
            params["createdBefore"] = cursor

        headers = self._build_headers()
        cookies = {"li_at": self.auth.li_at, "JSESSIONID": self.auth.jsessionid or ""}
        path_segment = quote(platform_thread_id, safe="")
        url = f"{_VOYAGER_BASE}/messaging/conversations/{path_segment}/events"

        with httpx.Client(
            proxy=self._proxy_url(),
            timeout=_VOYAGER_TIMEOUT_S,
        ) as client:
            my_profile_id = self._get_my_profile_id(client)
            logger.debug("fetch_messages thread=%s cursor=%s limit=%d", platform_thread_id, cursor, limit)
            resp = client.get(url, params=params, headers=headers, cookies=cookies)
            resp.raise_for_status()
            try:
                data = resp.json()
            except Exception:
                logger.debug("fetch_messages: non-JSON response for thread=%s", platform_thread_id)
                data = {}
            if not isinstance(data, dict):
                data = {}

        raw_events = data.get("elements") or data.get("events") or []
        if not isinstance(raw_events, list):
            raw_events = []

        messages: list[LinkedInMessage] = []
        created_ats: list[int] = []
        seen_ids: set[str] = set()
        skipped = 0
        for ev in raw_events:
            if not isinstance(ev, dict):
                skipped += 1
                continue
            msg = self._parse_event_to_message(ev, my_profile_id)
            if msg is None:
                skipped += 1
                continue
            if msg.platform_message_id in seen_ids:
                continue
            seen_ids.add(msg.platform_message_id)
            messages.append(msg)
            try:
                ca = ev.get("createdAt")
                if isinstance(ca, (int, float)):
                    created_ats.append(int(ca))
            except (TypeError, ValueError):
                pass

        if skipped:
            logger.debug("fetch_messages: skipped %d malformed events for thread=%s", skipped, platform_thread_id)
        logger.debug("fetch_messages: %d messages parsed from %d raw events", len(messages), len(raw_events))

        # API often returns newest first; ensure chronological order (oldest first).
        messages.sort(key=lambda m: m.sent_at)
        created_ats.sort()

        # Use raw_events count (API response size) for pagination; some events may be skipped as malformed.
        if len(raw_events) < limit:
            next_cursor = None
        else:
            next_cursor = str(created_ats[0]) if created_ats else None

        return (messages, next_cursor)

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
