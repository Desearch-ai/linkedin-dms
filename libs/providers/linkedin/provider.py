from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from libs.core.models import AccountAuth, ProxyConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — send_message (upstream)
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Constants — GraphQL list_threads / fetch_messages
# ---------------------------------------------------------------------------

_VOYAGER_BASE = "https://www.linkedin.com/voyager/api"
_GRAPHQL_BASE = f"{_VOYAGER_BASE}/voyagerMessagingGraphQL/graphql"
_VOYAGER_TIMEOUT_S = 30.0
_MAX_PAGES = 50
_DELAY_BETWEEN_PAGES_S = 1.5
_RETRY_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY_S = 2.0
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_PLAYWRIGHT_NAV_RETRIES = 2

# NOTE: These queryId hashes are extracted from LinkedIn's frontend JS bundle.
# LinkedIn may rotate them without notice. If requests start returning 400/404,
# update by inspecting XHR calls on linkedin.com/messaging/ in browser DevTools
# and extracting the new queryId values from the graphql request URLs.
_CONVERSATIONS_QUERY_ID = "messengerConversations.0d5e6781bbee71c3e51c8843c6519f48"
_MESSAGES_QUERY_ID = "messengerMessages.21eabeb3ee872254060ef21b793ea7d0"

_MESSAGING_PAGE_URL = "https://www.linkedin.com/messaging/"

_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


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


# ---------------------------------------------------------------------------
# GraphQL helper functions
# ---------------------------------------------------------------------------

def _harvest_cookies_playwright(
    li_at: str,
    jsessionid: str,
    *,
    proxy_url: Optional[str] = None,
    headless: bool = True,
    timeout_ms: int = 30_000,
) -> dict[str, str]:
    """Launch a Playwright browser, inject auth cookies, navigate to LinkedIn
    messaging to trigger Cloudflare cookie generation, and return the full
    cookie jar as a flat ``{name: value}`` dict.

    This is required because the GraphQL messaging endpoints enforce
    Cloudflare bot-management cookies that can only be obtained through a
    real browser context.

    Playwright is an **optional** dependency.  Install with:
        pip install playwright && playwright install chromium

    Raises:
        RuntimeError: If Playwright is not installed or navigation fails.
    """
    try:
        from playwright.sync_api import sync_playwright  # noqa: WPS433
    except ImportError as exc:
        raise RuntimeError(
            "Cloudflare cookies required but playwright is not installed. "
            "Install it with:  pip install playwright && playwright install chromium"
        ) from exc

    browser_cookies: dict[str, str] = {}

    with sync_playwright() as pw:
        launch_kwargs: dict[str, Any] = {"headless": headless, "args": ["--no-sandbox"]}
        if proxy_url:
            launch_kwargs["proxy"] = {"server": proxy_url}

        browser = pw.chromium.launch(**launch_kwargs)
        try:
            context = browser.new_context(
                user_agent=_BROWSER_USER_AGENT,
                viewport={"width": 1920, "height": 1080},
            )
            context.add_cookies([
                {
                    "name": "li_at",
                    "value": li_at,
                    "domain": ".linkedin.com",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                },
                {
                    "name": "JSESSIONID",
                    "value": f'"{jsessionid}"',
                    "domain": ".linkedin.com",
                    "path": "/",
                    "secure": True,
                },
            ])

            page = context.new_page()

            # Navigate with retry — Cloudflare or LinkedIn may flake.
            last_nav_error: Optional[Exception] = None
            for nav_attempt in range(_PLAYWRIGHT_NAV_RETRIES + 1):
                try:
                    page.goto(_MESSAGING_PAGE_URL, wait_until="networkidle", timeout=timeout_ms)
                    last_nav_error = None
                    break
                except Exception as nav_exc:
                    last_nav_error = nav_exc
                    if nav_attempt < _PLAYWRIGHT_NAV_RETRIES:
                        logger.debug(
                            "_harvest_cookies_playwright: nav attempt %d failed, retrying",
                            nav_attempt + 1,
                        )
                        time.sleep(2)
            if last_nav_error is not None:
                raise RuntimeError(
                    f"Failed to navigate to LinkedIn messaging after "
                    f"{_PLAYWRIGHT_NAV_RETRIES + 1} attempts. "
                    f"Ensure cookies are valid and the network is reachable."
                ) from last_nav_error

            for cookie in context.cookies():
                browser_cookies[cookie["name"]] = cookie["value"]

            logger.debug(
                "_harvest_cookies_playwright: harvested %d cookies",
                len(browser_cookies),
            )
        finally:
            browser.close()

    return browser_cookies


def _extract_thread_title(conversation: dict[str, Any]) -> Optional[str]:
    """Extract a human-readable title from a GraphQL conversation element."""
    name = conversation.get("conversationName")
    if name and isinstance(name, str) and name.strip():
        return name.strip()

    names: list[str] = []
    participants = conversation.get("conversationParticipants") or []
    for p in participants:
        if not isinstance(p, dict):
            continue
        profile = p.get("participantProfile") or p.get("profile") or {}
        if not isinstance(profile, dict):
            continue
        first = profile.get("firstName", "")
        last = profile.get("lastName", "")
        full = f"{first} {last}".strip()
        if full:
            names.append(full)
    return ", ".join(names) if names else None


def _extract_conversation_urn(conversation: dict[str, Any]) -> Optional[str]:
    """Return a stable conversation identifier from a GraphQL element."""
    return (
        conversation.get("entityUrn")
        or conversation.get("conversationUrn")
        or conversation.get("backendConversationUrn")
    )


def _parse_graphql_messages(
    events: list[dict[str, Any]],
    my_profile_id: Optional[str],
) -> list[LinkedInMessage]:
    """Parse GraphQL message event elements into LinkedInMessage objects.

    Returns messages sorted oldest-first (chronological order).
    """
    messages: list[LinkedInMessage] = []
    seen_ids: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            continue

        msg_id = (
            event.get("entityUrn")
            or event.get("backendUrn")
            or event.get("dashEntityUrn")
        )
        if not msg_id or msg_id in seen_ids:
            continue
        seen_ids.add(msg_id)

        # Extract text body.
        body = event.get("eventContent") or event.get("body") or {}
        if isinstance(body, dict):
            attr_body = body.get("attributedBody")
            text = (
                (attr_body.get("text") if isinstance(attr_body, dict) else None)
                or body.get("text")
                or body.get("body")
            )
        elif isinstance(body, str):
            text = body
        else:
            text = None

        # Sender and direction.
        sender_urn = None
        sender_name = None
        sender_info = event.get("sender") or event.get("from") or {}
        if isinstance(sender_info, dict):
            profile = sender_info.get("participantProfile") or sender_info.get("profile") or {}
            if isinstance(profile, dict):
                sender_urn = profile.get("entityUrn") or profile.get("publicIdentifier")
                first = profile.get("firstName", "")
                last = profile.get("lastName", "")
                sender_name = f"{first} {last}".strip() or sender_urn

        direction = "in"
        if my_profile_id and sender_urn:
            if sender_urn == my_profile_id or sender_urn.endswith(f":{my_profile_id}"):
                direction = "out"

        # Timestamp.
        created_at = event.get("createdAt") or event.get("deliveredAt")
        if isinstance(created_at, (int, float)):
            sent_at = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc)
        else:
            sent_at = datetime.now(timezone.utc)

        messages.append(LinkedInMessage(
            platform_message_id=msg_id,
            direction=direction,
            sender=sender_name,
            text=text,
            sent_at=sent_at,
            raw=event,
        ))

    messages.sort(key=lambda m: m.sent_at)
    return messages


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

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
        # send_message state (upstream)
        self._sent_keys: dict[str, str] = {}
        self._last_send_ts: float = 0.0
        # GraphQL state
        self._client: Optional[httpx.Client] = None
        self._browser_cookies: Optional[dict[str, str]] = None
        self._profile_id: Optional[str] = None
        self._profile_id_fetched: bool = False

    # ------------------------------------------------------------------
    # Shared helpers — send_message (upstream)
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
    # Helpers — GraphQL list_threads / fetch_messages
    # ------------------------------------------------------------------

    def _get_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            proxy = self.proxy.url if self.proxy and self.proxy.url.strip() else None
            self._client = httpx.Client(proxy=proxy, timeout=_VOYAGER_TIMEOUT_S)
        return self._client

    def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            self._client.close()
            self._client = None

    def invalidate_cookies(self) -> None:
        self._browser_cookies = None

    def __enter__(self) -> LinkedInProvider:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _build_graphql_headers(self) -> dict[str, str]:
        if not self.auth.jsessionid or not self.auth.jsessionid.strip():
            raise ValueError("JSESSIONID cookie required for Voyager API (CSRF)")
        return {
            "User-Agent": _BROWSER_USER_AGENT,
            "Accept": "application/graphql",
            "x-restli-protocol-version": "2.0.0",
            "x-li-track": json.dumps({
                "clientVersion": "1.13.42912",
                "mpVersion": "1.13.42912",
                "osName": "web",
                "timezoneOffset": 0,
                "deviceFormFactor": "DESKTOP",
                "mpName": "voyager-web",
            }),
            "x-li-page-instance": "urn:li:page:d_flagship3_messaging",
            "x-li-lang": "en_US",
            "csrf-token": self.auth.jsessionid,
            "referer": _MESSAGING_PAGE_URL,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }

    def _build_basic_cookies(self) -> dict[str, str]:
        return self._get_cookies()

    def _get_browser_cookies(self) -> dict[str, str]:
        if self._browser_cookies is not None:
            return self._browser_cookies
        return self._build_basic_cookies()

    def _harvest_and_cache_cookies(self) -> dict[str, str]:
        if not self.auth.jsessionid or not self.auth.jsessionid.strip():
            raise ValueError("JSESSIONID cookie required for Voyager API (CSRF)")
        proxy_url = self.proxy.url if self.proxy and self.proxy.url.strip() else None
        self._browser_cookies = _harvest_cookies_playwright(
            li_at=self.auth.li_at,
            jsessionid=self.auth.jsessionid,
            proxy_url=proxy_url,
        )
        return self._browser_cookies

    def _get_profile_id(self) -> Optional[str]:
        if self._profile_id_fetched:
            return self._profile_id
        client = self._get_client()
        headers = {
            "User-Agent": _BROWSER_USER_AGENT,
            "Accept": "application/vnd.linkedin.normalized+json+2.1",
            "x-restli-protocol-version": "2.0.0",
            "csrf-token": self.auth.jsessionid or "",
        }
        cookies = self._build_basic_cookies()
        try:
            resp = client.get(f"{_VOYAGER_BASE}/me", headers=headers, cookies=cookies)
            if resp.status_code == 200:
                data = resp.json()
                self._profile_id = data.get("entityUrn") or data.get("publicIdentifier")
        except Exception:
            logger.debug("_get_profile_id: failed to fetch /me", exc_info=True)
        self._profile_id_fetched = True
        return self._profile_id

    def _get_with_retry(self, client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
        last_exc: Optional[httpx.HTTPStatusError] = None
        for attempt in range(_RETRY_MAX_ATTEMPTS):
            resp = client.get(url, **kwargs)
            if resp.status_code not in _RETRYABLE_STATUS_CODES:
                return resp
            last_exc = httpx.HTTPStatusError(
                str(resp.status_code), request=resp.request, response=resp,
            )
            if attempt == _RETRY_MAX_ATTEMPTS - 1:
                break
            delay = _RETRY_BASE_DELAY_S * (2 ** attempt)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except (TypeError, ValueError):
                        pass
            logger.debug(
                "retry: %d from LinkedIn, attempt %d/%d in %.1fs",
                resp.status_code, attempt + 1, _RETRY_MAX_ATTEMPTS, delay,
            )
            time.sleep(delay)
        raise last_exc  # type: ignore[misc]

    def _is_cf_blocked(self, resp: httpx.Response) -> bool:
        if resp.status_code in (302, 303):
            return True
        if resp.status_code == 403 and "text/html" in resp.headers.get("content-type", ""):
            return True
        return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        proxy_repr = "'[REDACTED]'" if self.proxy else "None"
        return f"LinkedInProvider(auth='[REDACTED]', proxy={proxy_repr})"

    def __str__(self) -> str:
        return self.__repr__()

    def list_threads(self) -> list[LinkedInThread]:
        """Fetch all DM threads via the GraphQL messaging API.

        Tries basic cookies first; if Cloudflare blocks, harvests browser
        cookies via Playwright (optional dependency) and retries.
        """
        headers = self._build_graphql_headers()
        cookies = self._get_browser_cookies()
        client = self._get_client()

        profile_id = self._get_profile_id()
        if not profile_id:
            raise RuntimeError(
                "Could not determine LinkedIn profile ID. "
                "Ensure li_at and JSESSIONID cookies are valid."
            )

        if "fsd_profile:" in profile_id:
            mailbox_urn = profile_id
        else:
            mailbox_urn = f"urn:li:fsd_profile:{profile_id}"

        all_threads: list[LinkedInThread] = []
        seen_urns: set[str] = set()
        sync_token: Optional[str] = None

        for page_num in range(1, _MAX_PAGES + 1):
            variables = f"(mailboxUrn:{mailbox_urn}"
            if sync_token:
                variables += f",syncToken:{sync_token}"
            variables += ")"

            url = f"{_GRAPHQL_BASE}?queryId={_CONVERSATIONS_QUERY_ID}&variables={variables}"

            resp = self._get_with_retry(
                client, url, headers=headers, cookies=cookies,
            )

            # Detect CF block → harvest cookies via Playwright and retry.
            if self._is_cf_blocked(resp) and self._browser_cookies is None:
                logger.debug("list_threads: CF blocked, harvesting cookies via Playwright")
                cookies = self._harvest_and_cache_cookies()
                resp = self._get_with_retry(
                    client, url, headers=headers, cookies=cookies,
                )

            resp.raise_for_status()
            try:
                data = resp.json() if resp.content else {}
            except (json.JSONDecodeError, ValueError):
                logger.debug("list_threads: non-JSON response on page %d", page_num)
                data = {}
            if not isinstance(data, dict):
                data = {}

            inner = data.get("data")
            inner = inner if isinstance(inner, dict) else {}
            conv_data = inner.get("messengerConversationsBySyncToken", {})
            if not isinstance(conv_data, dict):
                conv_data = {}

            elements = conv_data.get("elements", [])
            if not isinstance(elements, list):
                elements = []

            for elem in elements:
                if not isinstance(elem, dict):
                    continue
                urn = _extract_conversation_urn(elem)
                if not urn or urn in seen_urns:
                    continue
                seen_urns.add(urn)
                title = _extract_thread_title(elem)
                all_threads.append(LinkedInThread(
                    platform_thread_id=urn,
                    title=title,
                    raw=elem,
                ))

            logger.debug(
                "list_threads: page %d fetched %d elements (%d threads total)",
                page_num, len(elements), len(all_threads),
            )

            metadata = conv_data.get("metadata", {})
            new_sync_token = metadata.get("newSyncToken") if isinstance(metadata, dict) else None

            if not elements:
                break
            if not new_sync_token or new_sync_token == sync_token:
                break

            sync_token = new_sync_token

            if page_num < _MAX_PAGES:
                time.sleep(_DELAY_BETWEEN_PAGES_S)
        else:
            logger.warning(
                "list_threads: reached max page limit (%d); %d threads fetched",
                _MAX_PAGES, len(all_threads),
            )

        logger.info("list_threads: %d threads across %d pages", len(all_threads), page_num)
        return all_threads

    def fetch_messages(
        self,
        *,
        platform_thread_id: str,
        cursor: Optional[str],
        limit: int = 50,
    ) -> tuple[list[LinkedInMessage], Optional[str]]:
        """Fetch messages for a thread via the GraphQL messaging API.

        Tries basic cookies first; if Cloudflare blocks, harvests browser
        cookies via Playwright (optional dependency) and retries.

        Args:
            platform_thread_id: Conversation URN.
            cursor: ``createdBefore`` timestamp in ms as string, or None.
            limit: Max messages per call (1-200).

        Returns:
            (messages, next_cursor).  next_cursor is None when exhausted.
        """
        if limit < 1 or limit > 200:
            raise ValueError(f"limit must be between 1 and 200, got {limit}")

        headers = self._build_graphql_headers()
        cookies = self._get_browser_cookies()
        client = self._get_client()

        my_profile_id = self._get_profile_id()

        variables = f"(conversationUrn:{platform_thread_id},count:{limit}"
        if cursor:
            variables += f",createdBefore:{cursor}"
        variables += ")"

        url = f"{_GRAPHQL_BASE}?queryId={_MESSAGES_QUERY_ID}&variables={variables}"

        resp = self._get_with_retry(
            client, url, headers=headers, cookies=cookies,
        )

        # Detect CF block → harvest cookies via Playwright and retry.
        if self._is_cf_blocked(resp) and self._browser_cookies is None:
            logger.debug("fetch_messages: CF blocked, harvesting cookies via Playwright")
            cookies = self._harvest_and_cache_cookies()
            resp = self._get_with_retry(
                client, url, headers=headers, cookies=cookies,
            )

        resp.raise_for_status()
        try:
            data = resp.json() if resp.content else {}
        except (json.JSONDecodeError, ValueError):
            logger.debug("fetch_messages: non-JSON response for %s", platform_thread_id)
            data = {}
        if not isinstance(data, dict):
            data = {}

        inner = data.get("data")
        inner = inner if isinstance(inner, dict) else {}
        msg_data = inner.get("messengerMessagesBySyncToken") or inner.get("messengerMessages", {})
        if not isinstance(msg_data, dict):
            msg_data = {}

        elements = msg_data.get("elements", [])
        if not isinstance(elements, list):
            elements = []

        messages = _parse_graphql_messages(elements, my_profile_id)

        next_cursor: Optional[str] = None
        if len(elements) >= limit and messages:
            oldest = messages[0]
            if oldest.raw and isinstance(oldest.raw, dict):
                created_at = oldest.raw.get("createdAt")
                if isinstance(created_at, (int, float)):
                    next_cursor = str(int(created_at))

        logger.info(
            "fetch_messages: %d messages for %s (cursor=%s, next=%s)",
            len(messages), platform_thread_id, cursor, next_cursor,
        )
        return messages, next_cursor

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
