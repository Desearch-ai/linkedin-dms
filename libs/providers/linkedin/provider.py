from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import httpx

from libs.core.models import AccountAuth, ProxyConfig

logger = logging.getLogger(__name__)

# LinkedIn internal API base
_VOYAGER_BASE = "https://www.linkedin.com/voyager/api"

# GraphQL messaging endpoint (separate from the REST-li /graphql path)
_GRAPHQL_MSG_URL = f"{_VOYAGER_BASE}/voyagerMessagingGraphQL/graphql"

# Fallback queryIds — used when auto-detection from LinkedIn's JS bundles fails.
# These are tied to a specific LinkedIn frontend build and will eventually go stale.
_FALLBACK_CONVERSATIONS_QUERY_ID = (
    "messengerConversations.9501074288a12f3ae9e3c7ea243bccbf"
)
_FALLBACK_MESSAGES_QUERY_ID = "messengerMessages.5846eeb71c981f11e0134cb6626cc314"
# Regex patterns to extract queryId hashes from LinkedIn's compiled JS bundles.
# The bundles contain entries like: queryId:"messengerConversations.<32-hex-hash>"
_CONVERSATIONS_QID_RE = re.compile(
    r'queryId:\s*["\']?(messengerConversations\.[a-f0-9]{20,})["\']?'
)
_MESSAGES_QID_RE = re.compile(
    r'queryId:\s*["\']?(messengerMessages\.[a-f0-9]{20,})["\']?'
)

# Pattern to find JS bundle URLs in LinkedIn's HTML page source
_SCRIPT_SRC_RE = re.compile(r'<script[^>]+src="([^"]+)"', re.IGNORECASE)

# Cache for discovered queryIds (thread-safe)
_query_id_cache: dict[str, str] = {}
_query_id_lock = threading.Lock()

_STATIC_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    ),
    "accept-language": "en-US,en;q=0.9",
    "x-restli-protocol-version": "2.0.0",
    "x-li-lang": "en_US",
    "x-li-track": json.dumps(
        {
            "clientVersion": "1.13.42849",
            "mpVersion": "1.13.42849",
            "osName": "web",
            "timezoneOffset": -4,
            "timezone": "America/New_York",
            "deviceFormFactor": "DESKTOP",
            "mpName": "voyager-web",
            "displayDensity": 1.25,
            "displayWidth": 1920,
            "displayHeight": 1080,
        }
    ),
    "x-li-page-instance": "urn:li:page:d_flagship3_messaging",
    "sec-ch-ua": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

# Default page size for conversation listing
_DEFAULT_COUNT = 20

# Safety limit to prevent runaway pagination on very large inboxes
_MAX_PAGES = 50  # 50 * 20 = 1000 threads max


def _discover_query_ids(
    cookies: dict[str, str],
    headers: dict[str, str],
    proxy_url: Optional[str] = None,
) -> tuple[str, str]:
    """Auto-detect current GraphQL queryIds from LinkedIn's JS bundles.

    LinkedIn embeds queryId strings (e.g. ``messengerConversations.<hash>``)
    in its compiled JavaScript bundles. This function:

    1. Fetches the ``/messaging`` page HTML.
    2. Extracts ``<script src="...">`` URLs pointing to JS bundles.
    3. Fetches each bundle and scans for the queryId patterns.
    4. Returns ``(conversations_query_id, messages_query_id)``.

    Falls back to the hardcoded ``_FALLBACK_*`` values if discovery fails.
    Results are cached in ``_query_id_cache`` for the process lifetime.
    """
    with _query_id_lock:
        cached_conv = _query_id_cache.get("conversations")
        cached_msg = _query_id_cache.get("messages")
        if cached_conv and cached_msg:
            return cached_conv, cached_msg

    conv_qid: Optional[str] = None
    msg_qid: Optional[str] = None

    try:
        with httpx.Client(
            proxy=proxy_url,
            timeout=20.0,
            follow_redirects=True,
        ) as client:
            page_headers = {
                "user-agent": headers.get("user-agent", _STATIC_HEADERS["user-agent"]),
                "accept": "text/html,application/xhtml+xml",
                "accept-language": "en-US,en;q=0.9",
            }
            resp = client.get(
                "https://www.linkedin.com/messaging/",
                headers=page_headers,
                cookies=cookies,
            )
            if resp.status_code != 200:
                logger.debug(
                    "queryId discovery: /messaging returned %d, using fallbacks",
                    resp.status_code,
                )
                return _use_fallback_query_ids()

            html = resp.text
            script_urls = _SCRIPT_SRC_RE.findall(html)
            logger.debug("queryId discovery: found %d script tags", len(script_urls))

            # Filter to likely messaging-related bundles; fall back to all if none match
            messaging_urls = [
                u
                for u in script_urls
                if "messaging" in u.lower() or "voyager" in u.lower()
            ]
            candidate_urls = messaging_urls or script_urls

            for url in candidate_urls:
                if url.startswith("//"):
                    url = "https:" + url
                elif url.startswith("/"):
                    url = "https://www.linkedin.com" + url

                try:
                    js_resp = client.get(url, headers=page_headers, timeout=15.0)
                    if js_resp.status_code != 200:
                        continue
                    js_text = js_resp.text
                except httpx.HTTPError:
                    continue

                if not conv_qid:
                    match = _CONVERSATIONS_QID_RE.search(js_text)
                    if match:
                        conv_qid = match.group(1)
                        logger.info("Discovered conversations queryId: %s", conv_qid)

                if not msg_qid:
                    match = _MESSAGES_QID_RE.search(js_text)
                    if match:
                        msg_qid = match.group(1)
                        logger.info("Discovered messages queryId: %s", msg_qid)

                if conv_qid and msg_qid:
                    break

    except httpx.HTTPError as exc:
        logger.debug("queryId discovery failed with HTTP error: %s", exc)
    except Exception:
        logger.debug("queryId discovery failed unexpectedly", exc_info=True)

    conv_qid = conv_qid or _FALLBACK_CONVERSATIONS_QUERY_ID
    msg_qid = msg_qid or _FALLBACK_MESSAGES_QUERY_ID

    with _query_id_lock:
        _query_id_cache["conversations"] = conv_qid
        _query_id_cache["messages"] = msg_qid

    return conv_qid, msg_qid


def _use_fallback_query_ids() -> tuple[str, str]:
    """Return hardcoded fallback queryIds and cache them."""
    with _query_id_lock:
        _query_id_cache["conversations"] = _FALLBACK_CONVERSATIONS_QUERY_ID
        _query_id_cache["messages"] = _FALLBACK_MESSAGES_QUERY_ID
    logger.debug(
        "Using fallback queryIds: conversations=%s, messages=%s",
        _FALLBACK_CONVERSATIONS_QUERY_ID,
        _FALLBACK_MESSAGES_QUERY_ID,
    )
    return _FALLBACK_CONVERSATIONS_QUERY_ID, _FALLBACK_MESSAGES_QUERY_ID


def _reset_query_id_cache() -> None:
    """Clear the cached queryIds. Useful for testing or when IDs go stale."""
    with _query_id_lock:
        _query_id_cache.clear()


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


def _extract_thread_title(
    element: dict[str, Any], included: list[dict[str, Any]]
) -> Optional[str]:
    """Try to build a human-readable title from conversation participants.

    LinkedIn stores participant references in the conversation element and
    resolves them in the top-level 'included' array. We look for miniProfile
    entities that match this conversation's participants.
    """
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
    """Parse the legacy Voyager conversations response into LinkedInThread objects."""
    elements = data.get("elements", [])
    included = data.get("included", [])
    threads: list[LinkedInThread] = []
    for elem in elements:
        entity_urn = elem.get("entityUrn", "")
        if not entity_urn:
            continue
        title = _extract_thread_title(elem, included)
        threads.append(
            LinkedInThread(
                platform_thread_id=entity_urn,
                title=title,
                raw=elem,
            )
        )
    return threads


_CONVERSATIONS_RESPONSE_KEYS = (
    "messengerConversationsByCategoryQuery",
    "messengerConversationsByCriteria",
    "messengerConversationsByLastActivity",
    "messengerConversations",
)

_MESSAGES_RESPONSE_KEYS = (
    "messengerMessagesBySyncToken",
    "messengerMessages",
    "messengerMessagesByConversation",
)


def _find_elements(data: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    """Locate the elements list and metadata from a GraphQL conversations response."""
    result = data
    if "data" in result and isinstance(result["data"], dict):
        result = result["data"]

    for key in _CONVERSATIONS_RESPONSE_KEYS:
        if key in result and isinstance(result[key], dict):
            container = result[key]
            return container.get("elements", []), container.get("metadata", {})

    return result.get("elements", []), {}


def _parse_graphql_threads(data: dict[str, Any]) -> list[LinkedInThread]:
    """Parse the GraphQL messengerConversations response.

    The response shape:
      data.messengerConversationsByCategoryQuery.elements[]
    Each element contains conversationParticipants, entityUrn, lastActivityAt, etc.
    """
    threads: list[LinkedInThread] = []
    elements, _ = _find_elements(data)

    for elem in elements:
        conv = elem if "entityUrn" in elem else elem.get("conversation", elem)
        entity_urn = conv.get("entityUrn", conv.get("conversationUrn", ""))
        if not entity_urn:
            continue

        title = _extract_graphql_title(conv)
        threads.append(
            LinkedInThread(
                platform_thread_id=entity_urn,
                title=title,
                raw=elem,
            )
        )
    return threads


def _extract_graphql_title(conv: dict[str, Any]) -> Optional[str]:
    """Extract participant names from a GraphQL conversation object."""
    names: list[str] = []

    participants = conv.get("conversationParticipants", conv.get("participants", []))
    for p in participants:
        profile = p.get("participantProfile", p.get("profile", p))
        first = profile.get("firstName", "")
        last = profile.get("lastName", "")
        full = f"{first} {last}".strip()
        if full:
            names.append(full)

    if not names:
        title = conv.get("title", None)
        if title:
            return title

    return ", ".join(names) if names else None


def _get_oldest_timestamp(data: dict[str, Any]) -> Optional[int]:
    """Extract the oldest lastActivityAt timestamp for cursor-based pagination."""
    elements, _ = _find_elements(data)
    if not elements:
        return None

    oldest = None
    for elem in elements:
        conv = elem if "lastActivityAt" in elem else elem.get("conversation", elem)
        ts = conv.get("lastActivityAt")
        if ts is not None:
            if oldest is None or ts < oldest:
                oldest = ts
    return oldest


def _parse_graphql_messages(
    data: dict[str, Any], conversation_urn: str
) -> tuple[list[LinkedInMessage], Optional[str]]:
    """Parse the GraphQL messengerMessages response into LinkedInMessage objects.

    Returns (messages_list, next_sync_token).
    """
    result = data
    if "data" in result and isinstance(result["data"], dict):
        result = result["data"]

    container: dict[str, Any] = {}
    for key in _MESSAGES_RESPONSE_KEYS:
        if key in result and isinstance(result[key], dict):
            container = result[key]
            break
    if not container:
        container = result

    elements = container.get("elements", [])

    next_cursor: Optional[str] = None
    metadata = container.get("metadata", {})
    sync_token = metadata.get("syncToken")
    if sync_token:
        next_cursor = sync_token

    messages: list[LinkedInMessage] = []
    for elem in elements:
        msg_urn = elem.get("entityUrn", elem.get("backendUrn", ""))
        if not msg_urn:
            continue

        body = elem.get("body", {})
        text = (
            body.get("text", "")
            if isinstance(body, dict)
            else str(body)
            if body
            else ""
        )

        sender_profile = elem.get("sender", {})
        sender_urn = ""
        if isinstance(sender_profile, dict):
            sender_urn = sender_profile.get("entityUrn", "")
            if not sender_urn:
                member = sender_profile.get(
                    "member", sender_profile.get("participantProfile", {})
                )
                if isinstance(member, dict):
                    sender_urn = member.get("entityUrn", "")
                elif isinstance(member, str):
                    sender_urn = member

        delivered_at = elem.get("deliveredAt", 0)
        if delivered_at:
            sent_at = datetime.fromtimestamp(delivered_at / 1000, tz=timezone.utc)
        else:
            sent_at = datetime.now(timezone.utc)

        direction = "in"
        if sender_urn and conversation_urn:
            conv_profile = ""
            if "fsd_profile:" in conversation_urn:
                parts = conversation_urn.split("fsd_profile:")
                if len(parts) >= 2:
                    conv_profile = parts[1].split(",")[0].rstrip(")")
            sender_id = sender_urn.split(":")[-1] if ":" in sender_urn else sender_urn
            if conv_profile and sender_id == conv_profile:
                direction = "out"

        messages.append(
            LinkedInMessage(
                platform_message_id=msg_urn,
                direction=direction,
                sender=sender_urn or None,
                text=text or None,
                sent_at=sent_at,
                raw=elem,
            )
        )

    messages.sort(key=lambda m: m.sent_at)
    return messages, next_cursor


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

    def _build_headers(
        self, *, accept: str = "application/vnd.linkedin.normalized+json+2.1"
    ) -> dict[str, str]:
        """Build request headers including the CSRF token from JSESSIONID."""
        headers = dict(_STATIC_HEADERS)
        headers["accept"] = accept
        if self.auth.jsessionid:
            headers["csrf-token"] = self.auth.jsessionid.strip('"')
        return headers

    def _build_cookies(self) -> dict[str, str]:
        """Build the cookie dict for requests. Never log the return value."""
        cookies: dict[str, str] = {"li_at": self.auth.li_at}
        if self.auth.jsessionid:
            cookies["JSESSIONID"] = self.auth.jsessionid
        return cookies

    def _get_proxy_url(self) -> Optional[str]:
        return self.proxy.url if self.proxy else None

    @staticmethod
    def _check_response(resp: httpx.Response) -> None:
        """Inspect a Voyager response and raise a clear error on failure.

        LinkedIn returns different status codes depending on the failure mode:
          302 — session expired / cookies invalid (redirect to login)
          401/403 — auth rejected
          500 — can indicate downstream auth-verification failure, or
                a genuine server bug.  Log the body so we can diagnose.
        """
        if resp.is_redirect:
            location = resp.headers.get("location", "")
            raise httpx.HTTPStatusError(
                f"LinkedIn returned {resp.status_code} redirect to {location!r}. "
                "Session cookies are expired or invalid — log in again and "
                "re-create the account with fresh li_at / JSESSIONID values.",
                request=resp.request,
                response=resp,
            )
        if resp.status_code >= 400:
            body_preview = resp.text[:500] if resp.text else "<empty>"
            logger.error(
                "LinkedIn API error %d on %s — body: %s",
                resp.status_code,
                resp.request.url,
                body_preview,
            )
            resp.raise_for_status()

    def _resolve_profile_urn(
        self, client: httpx.Client, cookies: dict[str, str]
    ) -> str:
        """Call /me to get the logged-in user's fsd_profile URN (mailboxUrn).

        The /me endpoint returns a normalized JSON response with shape:
            {"data": {"*miniProfile": "urn:li:fs_miniProfile:...", ...}, "included": [...]}
        The included array contains the full miniProfile object with entityUrn
        and dashEntityUrn fields.
        """
        headers = self._build_headers()
        resp = client.get(
            f"{_VOYAGER_BASE}/me",
            headers=headers,
            cookies=cookies,
        )
        self._check_response(resp)
        body = resp.json()

        urn = ""

        top_data = body if "entityUrn" in body else body.get("data", {})
        if isinstance(top_data, dict):
            urn = top_data.get("entityUrn", "")
            if not urn:
                mini_ref = top_data.get("*miniProfile", "")
                if mini_ref:
                    urn = mini_ref

        if not urn:
            for inc in body.get("included", []):
                candidate = inc.get("dashEntityUrn", "") or inc.get("entityUrn", "")
                if "fsd_profile" in candidate:
                    urn = candidate
                    break
                if "fs_miniProfile" in candidate or "member" in candidate:
                    urn = candidate

        for prefix in (
            "urn:li:fs_miniProfile:",
            "urn:li:fsd_profile:",
            "urn:li:member:",
        ):
            if urn.startswith(prefix):
                member_id = urn[len(prefix) :]
                return f"urn:li:fsd_profile:{member_id}"

        if urn:
            logger.warning("Unexpected URN format from /me: %r", urn)
            return urn

        raise ValueError(
            "Could not extract profile URN from /me response. "
            f"Top-level keys: {list(body.keys())}"
        )

    def list_threads(self) -> list[LinkedInThread]:
        """Fetch all DM conversation threads via LinkedIn's GraphQL messaging API.

        Uses the voyagerMessagingGraphQL endpoint with cursor-based pagination
        (lastUpdatedBefore timestamp).
        """
        if not self.auth.jsessionid:
            raise ValueError(
                "JSESSIONID is required for LinkedIn API requests (used as CSRF token). "
                "Re-create the account with both li_at and JSESSIONID cookies."
            )

        headers = self._build_headers(accept="application/graphql")
        cookies = self._build_cookies()
        proxy_url = self._get_proxy_url()

        conv_query_id, _ = _discover_query_ids(cookies, headers, proxy_url)

        all_threads: list[LinkedInThread] = []
        page = 0

        with httpx.Client(
            proxy=proxy_url,
            timeout=30.0,
            follow_redirects=False,
        ) as client:
            mailbox_urn = self._resolve_profile_urn(client, cookies)
            logger.info("Resolved mailbox URN: %s", mailbox_urn)

            encoded_urn = quote(mailbox_urn, safe="")
            last_activity_before = int(time.time() * 1000)

            while page < _MAX_PAGES:
                variables = (
                    f"(query:(predicateUnions:List("
                    f"(conversationCategoryPredicate:(category:PRIMARY_INBOX)))),"
                    f"count:{_DEFAULT_COUNT},"
                    f"mailboxUrn:{encoded_urn},"
                    f"lastUpdatedBefore:{last_activity_before})"
                )
                full_url = (
                    f"{_GRAPHQL_MSG_URL}?queryId={conv_query_id}&variables={variables}"
                )
                resp = client.get(
                    full_url,
                    headers=headers,
                    cookies=cookies,
                )
                self._check_response(resp)
                data = resp.json()
                page_threads = _parse_graphql_threads(data)
                if not page_threads:
                    break
                all_threads.extend(page_threads)

                if len(page_threads) < _DEFAULT_COUNT:
                    break

                oldest_ts = _get_oldest_timestamp(data)
                if oldest_ts is None or oldest_ts >= last_activity_before:
                    break
                last_activity_before = oldest_ts

                page += 1

        if page >= _MAX_PAGES:
            logger.warning(
                "Reached max page limit (%d); %d threads fetched — some threads may be missing",
                _MAX_PAGES,
                len(all_threads),
            )
        logger.info("Fetched %d threads across %d pages", len(all_threads), page + 1)
        return all_threads

    def fetch_messages(
        self,
        *,
        platform_thread_id: str,
        cursor: Optional[str],
        limit: int = 50,
    ) -> tuple[list[LinkedInMessage], Optional[str]]:
        """Fetch messages for a conversation via the GraphQL messengerMessages endpoint.

        Args:
          platform_thread_id: conversation URN from list_threads
          cursor: opaque syncToken from a previous call (None = initial fetch)
          limit: unused for this endpoint (LinkedIn controls page size)

        Returns:
          (messages, next_cursor) where next_cursor is a syncToken or None.
        """
        if not self.auth.jsessionid:
            raise ValueError("JSESSIONID is required for LinkedIn API requests.")

        headers = self._build_headers(accept="application/graphql")
        cookies = self._build_cookies()
        proxy_url = self._get_proxy_url()

        _, msg_query_id = _discover_query_ids(cookies, headers, proxy_url)

        encoded_conv = quote(platform_thread_id, safe="")

        if cursor:
            encoded_cursor = quote(cursor, safe="")
            variables = f"(conversationUrn:{encoded_conv},syncToken:{encoded_cursor})"
        else:
            variables = f"(conversationUrn:{encoded_conv})"

        full_url = f"{_GRAPHQL_MSG_URL}?queryId={msg_query_id}&variables={variables}"

        with httpx.Client(
            proxy=proxy_url,
            timeout=30.0,
            follow_redirects=False,
        ) as client:
            resp = client.get(full_url, headers=headers, cookies=cookies)
            self._check_response(resp)
            data = resp.json()

        messages, next_cursor = _parse_graphql_messages(data, platform_thread_id)
        return messages, next_cursor

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
