"""Tests for LinkedInProvider.list_threads() — GraphQL messaging API."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from libs.core.models import AccountAuth, ProxyConfig
from libs.providers.linkedin.provider import (
    LinkedInProvider,
    LinkedInThread,
    _extract_thread_title,
    _extract_conversation_urn,
    _MAX_PAGES,
    _GRAPHQL_BASE,
    _CONVERSATIONS_QUERY_ID,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def auth():
    return AccountAuth(li_at="test-li-at", jsessionid="ajax:csrf123")


@pytest.fixture
def provider(auth):
    """Provider with pre-set browser cookies and profile ID (no real Playwright)."""
    p = LinkedInProvider(auth=auth, proxy=None)
    p._browser_cookies = {"li_at": "test-li-at", "JSESSIONID": "ajax:csrf123", "__cf_bm": "fake"}
    p._profile_id = "urn:li:fsd_profile:ABC123"
    p._profile_id_fetched = True
    return p


# ---------------------------------------------------------------------------
# GraphQL response builders
# ---------------------------------------------------------------------------

def _graphql_conversations_response(
    elements: list[dict],
    *,
    sync_token: str | None = "token-1",
) -> dict:
    """Build a realistic GraphQL messengerConversations response."""
    metadata: dict = {}
    if sync_token is not None:
        metadata["newSyncToken"] = sync_token
        metadata["_recipeType"] = "com.linkedin.6614bca623fc9720b29edae666a9bdb4"
    return {
        "data": {
            "messengerConversationsBySyncToken": {
                "elements": elements,
                "metadata": metadata,
            },
        },
    }


def _make_conversation(
    urn: str,
    *,
    name: str | None = None,
    participants: list[dict] | None = None,
) -> dict:
    """Build a conversation element for GraphQL responses."""
    elem: dict = {"entityUrn": urn}
    if name is not None:
        elem["conversationName"] = name
    if participants is not None:
        elem["conversationParticipants"] = participants
    return elem


def _mock_resp(data: dict, status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.content = b"ok"
    r.raise_for_status = MagicMock()
    r.json.return_value = data
    return r


def _patch_client(mock_client: MagicMock):
    """Patch httpx.Client so _get_client() returns mock_client."""
    return patch("libs.providers.linkedin.provider.httpx.Client", return_value=mock_client)


# ---------------------------------------------------------------------------
# Unit tests: _extract_thread_title
# ---------------------------------------------------------------------------

class TestExtractThreadTitle:
    def test_title_from_conversation_name(self):
        elem = {"conversationName": "Project Chat"}
        assert _extract_thread_title(elem) == "Project Chat"

    def test_title_from_participants(self):
        elem = {
            "conversationParticipants": [
                {"participantProfile": {"firstName": "Alice", "lastName": "Smith"}},
            ],
        }
        assert _extract_thread_title(elem) == "Alice Smith"

    def test_title_multiple_participants(self):
        elem = {
            "conversationParticipants": [
                {"participantProfile": {"firstName": "Alice", "lastName": "A"}},
                {"participantProfile": {"firstName": "Bob", "lastName": "B"}},
            ],
        }
        assert _extract_thread_title(elem) == "Alice A, Bob B"

    def test_title_prefers_conversation_name(self):
        elem = {
            "conversationName": "Group Chat",
            "conversationParticipants": [
                {"participantProfile": {"firstName": "Alice", "lastName": "A"}},
            ],
        }
        assert _extract_thread_title(elem) == "Group Chat"

    def test_none_when_no_info(self):
        assert _extract_thread_title({}) is None

    def test_none_when_participants_empty(self):
        assert _extract_thread_title({"conversationParticipants": []}) is None

    def test_none_when_name_blank(self):
        assert _extract_thread_title({"conversationName": "   "}) is None

    def test_profile_fallback_key(self):
        elem = {
            "conversationParticipants": [
                {"profile": {"firstName": "Carol", "lastName": "D"}},
            ],
        }
        assert _extract_thread_title(elem) == "Carol D"

    def test_skips_non_dict_participants(self):
        elem = {
            "conversationParticipants": [
                "not-a-dict",
                {"participantProfile": {"firstName": "Valid", "lastName": "User"}},
            ],
        }
        assert _extract_thread_title(elem) == "Valid User"


# ---------------------------------------------------------------------------
# Unit tests: _extract_conversation_urn
# ---------------------------------------------------------------------------

class TestExtractConversationUrn:
    def test_entity_urn(self):
        assert _extract_conversation_urn({"entityUrn": "urn:li:conv:1"}) == "urn:li:conv:1"

    def test_conversation_urn_fallback(self):
        assert _extract_conversation_urn({"conversationUrn": "urn:conv:2"}) == "urn:conv:2"

    def test_backend_urn_fallback(self):
        assert _extract_conversation_urn({"backendConversationUrn": "urn:b:3"}) == "urn:b:3"

    def test_none_when_empty(self):
        assert _extract_conversation_urn({}) is None

    def test_prefers_entity_urn(self):
        elem = {"entityUrn": "urn:a", "conversationUrn": "urn:b"}
        assert _extract_conversation_urn(elem) == "urn:a"


# ---------------------------------------------------------------------------
# Integration: list_threads with mocked HTTP + mocked Playwright
# ---------------------------------------------------------------------------

class TestListThreads:
    def test_single_page(self, provider):
        """A few conversations → single page, no second request."""
        elems = [_make_conversation(f"urn:conv:{i}") for i in range(3)]
        data = _graphql_conversations_response(elems, sync_token=None)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            threads = provider.list_threads()
        assert len(threads) == 3
        assert all(isinstance(t, LinkedInThread) for t in threads)

    def test_empty_inbox(self, provider):
        data = _graphql_conversations_response([], sync_token=None)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            threads = provider.list_threads()
        assert threads == []

    def test_pagination_two_pages(self, provider):
        """Two pages with different sync tokens."""
        page1_elems = [_make_conversation(f"urn:conv:{i}") for i in range(5)]
        page2_elems = [_make_conversation(f"urn:conv:{5 + i}") for i in range(3)]
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.side_effect = [
            _mock_resp(_graphql_conversations_response(page1_elems, sync_token="tok-1")),
            _mock_resp(_graphql_conversations_response(page2_elems, sync_token=None)),
        ]
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep"):
            threads = provider.list_threads()
        assert len(threads) == 8
        assert mock_client.get.call_count == 2

    def test_pagination_stops_when_sync_token_unchanged(self, provider):
        """Stops when new sync token equals previous sync token."""
        page1 = [_make_conversation(f"urn:conv:{i}") for i in range(5)]
        page2 = [_make_conversation(f"urn:conv:{5 + i}") for i in range(3)]
        mock_client = MagicMock()
        mock_client.is_closed = False
        # Both pages return the same sync token → second page stops pagination
        mock_client.get.side_effect = [
            _mock_resp(_graphql_conversations_response(page1, sync_token="same-token")),
            _mock_resp(_graphql_conversations_response(page2, sync_token="same-token")),
        ]
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep"):
            threads = provider.list_threads()
        assert len(threads) == 8
        assert mock_client.get.call_count == 2

    def test_max_pages_safety_limit(self, provider):
        """Stops after _MAX_PAGES even if more data exists."""
        call_count = {"n": 0}

        def _make_page_resp(*args, **kwargs):
            page = call_count["n"]
            call_count["n"] += 1
            elems = [_make_conversation(f"urn:conv:p{page}_{i}") for i in range(5)]
            return _mock_resp(
                _graphql_conversations_response(elems, sync_token=f"tok-{page + 1}")
            )

        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.side_effect = _make_page_resp
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep"):
            threads = provider.list_threads()
        assert mock_client.get.call_count == _MAX_PAGES
        assert len(threads) == _MAX_PAGES * 5

    def test_deduplicates_across_pages(self, provider):
        """Same entityUrn on two pages → returned only once."""
        dup = _make_conversation("urn:conv:dup")
        page1 = [dup, _make_conversation("urn:conv:1")]
        page2 = [dup, _make_conversation("urn:conv:2")]
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.side_effect = [
            _mock_resp(_graphql_conversations_response(page1, sync_token="tok-1")),
            _mock_resp(_graphql_conversations_response(page2, sync_token=None)),
        ]
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep"):
            threads = provider.list_threads()
        urns = [t.platform_thread_id for t in threads]
        assert urns.count("urn:conv:dup") == 1
        assert "urn:conv:2" in urns

    def test_skips_elements_without_urn(self, provider):
        elems = [{"someField": "value"}, _make_conversation("urn:conv:good")]
        data = _graphql_conversations_response(elems, sync_token=None)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            threads = provider.list_threads()
        assert len(threads) == 1
        assert threads[0].platform_thread_id == "urn:conv:good"

    def test_title_from_conversation_name(self, provider):
        elem = _make_conversation("urn:conv:1", name="Team Chat")
        data = _graphql_conversations_response([elem], sync_token=None)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            threads = provider.list_threads()
        assert threads[0].title == "Team Chat"

    def test_title_from_participants(self, provider):
        elem = _make_conversation("urn:conv:1", participants=[
            {"participantProfile": {"firstName": "Alice", "lastName": "Smith"}},
        ])
        data = _graphql_conversations_response([elem], sync_token=None)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            threads = provider.list_threads()
        assert threads[0].title == "Alice Smith"

    def test_http_error_propagates(self, provider):
        resp_403 = MagicMock()
        resp_403.status_code = 403
        resp_403.content = b"forbidden"
        resp_403.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403", request=MagicMock(), response=resp_403,
        )
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = resp_403
        with _patch_client(mock_client):
            with pytest.raises(httpx.HTTPStatusError):
                provider.list_threads()

    def test_requires_jsessionid(self):
        auth = AccountAuth(li_at="li", jsessionid=None)
        p = LinkedInProvider(auth=auth)
        with pytest.raises(ValueError, match="JSESSIONID"):
            p.list_threads()

    def test_requires_jsessionid_not_blank(self):
        auth = AccountAuth(li_at="li", jsessionid="   ")
        p = LinkedInProvider(auth=auth)
        with pytest.raises(ValueError, match="JSESSIONID"):
            p.list_threads()

    def test_uses_proxy(self):
        auth = AccountAuth(li_at="test-li-at", jsessionid="ajax:csrf123")
        proxy = ProxyConfig(url="http://proxy:8080")
        p = LinkedInProvider(auth=auth, proxy=proxy)
        p._browser_cookies = {"li_at": "x"}
        p._profile_id = "urn:li:fsd_profile:ABC"
        p._profile_id_fetched = True
        data = _graphql_conversations_response([], sync_token=None)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client) as mock_cls:
            p.list_threads()
        mock_cls.assert_called_once()
        assert mock_cls.call_args.kwargs.get("proxy") == "http://proxy:8080"

    def test_cookies_not_leaked_into_headers(self, provider):
        data = _graphql_conversations_response([], sync_token=None)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            provider.list_threads()
        call_kwargs = mock_client.get.call_args.kwargs
        headers_str = str(call_kwargs["headers"])
        assert "test-li-at" not in headers_str

    def test_sleeps_between_pages(self, provider):
        page1 = [_make_conversation(f"urn:conv:{i}") for i in range(3)]
        page2 = [_make_conversation("urn:conv:last")]
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.side_effect = [
            _mock_resp(_graphql_conversations_response(page1, sync_token="tok-1")),
            _mock_resp(_graphql_conversations_response(page2, sync_token=None)),
        ]
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep") as mock_sleep:
            provider.list_threads()
        from libs.providers.linkedin.provider import _DELAY_BETWEEN_PAGES_S
        mock_sleep.assert_called_once_with(_DELAY_BETWEEN_PAGES_S)

    def test_no_sleep_on_single_page(self, provider):
        data = _graphql_conversations_response(
            [_make_conversation("urn:conv:1")], sync_token=None,
        )
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep") as mock_sleep:
            provider.list_threads()
        mock_sleep.assert_not_called()

    def test_retries_on_429_then_succeeds(self, provider):
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {}
        resp_429.request = MagicMock()
        data = _graphql_conversations_response(
            [_make_conversation("urn:conv:1")], sync_token=None,
        )
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.side_effect = [resp_429, _mock_resp(data)]
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep"):
            threads = provider.list_threads()
        assert len(threads) == 1

    def test_exhausts_retries_on_503(self, provider):
        resp_503 = MagicMock()
        resp_503.status_code = 503
        resp_503.headers = {}
        resp_503.request = MagicMock()
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = resp_503
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep"):
            with pytest.raises(httpx.HTTPStatusError):
                provider.list_threads()

    def test_retry_honours_retry_after_header(self, provider):
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "10"}
        resp_429.request = MagicMock()
        data = _graphql_conversations_response([], sync_token=None)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.side_effect = [resp_429, _mock_resp(data)]
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep") as mock_sleep:
            provider.list_threads()
        assert mock_sleep.call_args[0][0] >= 10.0

    def test_no_retry_on_403(self, provider):
        resp_403 = MagicMock()
        resp_403.status_code = 403
        resp_403.content = b"forbidden"
        resp_403.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403", request=MagicMock(), response=resp_403,
        )
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = resp_403
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep") as mock_sleep:
            with pytest.raises(httpx.HTTPStatusError):
                provider.list_threads()
        mock_sleep.assert_not_called()

    def test_client_reused_across_calls(self, provider):
        data = _graphql_conversations_response([], sync_token=None)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client) as mock_cls:
            provider.list_threads()
            provider.list_threads()
        mock_cls.assert_called_once()

    def test_context_manager_closes_client(self, auth):
        mock_client = MagicMock()
        mock_client.is_closed = False
        with _patch_client(mock_client):
            p = LinkedInProvider(auth=auth)
            with p:
                p._get_client()
            mock_client.close.assert_called_once()

    def test_handles_non_dict_response(self, provider):
        """Non-dict JSON response treated as empty."""
        r = MagicMock()
        r.status_code = 200
        r.content = b"[]"
        r.raise_for_status = MagicMock()
        r.json.return_value = []
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = r
        with _patch_client(mock_client):
            threads = provider.list_threads()
        assert threads == []

    def test_handles_empty_response_body(self, provider):
        """Empty response body treated as empty."""
        r = MagicMock()
        r.status_code = 200
        r.content = b""
        r.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = r
        with _patch_client(mock_client):
            threads = provider.list_threads()
        assert threads == []

    def test_graphql_url_contains_query_id(self, provider):
        """Verify the GraphQL URL uses the correct queryId."""
        data = _graphql_conversations_response([], sync_token=None)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            provider.list_threads()
        url = mock_client.get.call_args[0][0]
        assert _CONVERSATIONS_QUERY_ID in url
        assert _GRAPHQL_BASE in url

    def test_graphql_url_contains_mailbox_urn(self, provider):
        """Verify the mailboxUrn variable is included in the URL."""
        data = _graphql_conversations_response([], sync_token=None)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            provider.list_threads()
        url = mock_client.get.call_args[0][0]
        assert "mailboxUrn:urn:li:fsd_profile:ABC123" in url

    def test_raises_if_profile_id_unavailable(self, auth):
        """Raises RuntimeError if profile ID cannot be determined."""
        p = LinkedInProvider(auth=auth)
        p._browser_cookies = {"li_at": "x"}
        p._profile_id = None
        mock_client = MagicMock()
        mock_client.is_closed = False
        # Mock the /me call to return non-200
        me_resp = MagicMock()
        me_resp.status_code = 302
        mock_client.get.return_value = me_resp
        with _patch_client(mock_client):
            with pytest.raises(RuntimeError, match="profile ID"):
                p.list_threads()


# ---------------------------------------------------------------------------
# Header / cookie construction
# ---------------------------------------------------------------------------

class TestBuildHeaders:
    def test_includes_csrf_and_required_headers(self, provider):
        headers = provider._build_graphql_headers()
        assert headers["csrf-token"] == "ajax:csrf123"
        assert "User-Agent" in headers
        assert headers["Accept"] == "application/graphql"
        assert headers["x-restli-protocol-version"] == "2.0.0"
        assert "x-li-track" in headers
        assert "x-li-page-instance" in headers
        assert headers["x-li-lang"] == "en_US"

    def test_build_cookies_includes_li_at_and_jsessionid(self, provider):
        cookies = provider._build_basic_cookies()
        assert cookies["li_at"] == "test-li-at"
        assert cookies["JSESSIONID"] == "ajax:csrf123"

    def test_build_cookies_without_jsessionid(self):
        auth = AccountAuth(li_at="li", jsessionid=None)
        p = LinkedInProvider(auth=auth)
        cookies = p._build_basic_cookies()
        assert "li_at" in cookies
        assert "JSESSIONID" not in cookies


# ---------------------------------------------------------------------------
# Playwright cookie harvesting
# ---------------------------------------------------------------------------

class TestHarvestCookies:
    def test_get_browser_cookies_returns_basic_by_default(self, auth):
        """Without prior Playwright harvest, returns basic cookies."""
        p = LinkedInProvider(auth=auth)
        cookies = p._get_browser_cookies()
        assert cookies["li_at"] == "test-li-at"
        assert cookies["JSESSIONID"] == "ajax:csrf123"
        assert "__cf_bm" not in cookies

    def test_get_browser_cookies_returns_cached_after_harvest(self, auth):
        p = LinkedInProvider(auth=auth)
        fake_cookies = {"li_at": "x", "__cf_bm": "y"}
        with patch(
            "libs.providers.linkedin.provider._harvest_cookies_playwright",
            return_value=fake_cookies,
        ):
            p._harvest_and_cache_cookies()
            result = p._get_browser_cookies()
        assert result == fake_cookies
        assert result["__cf_bm"] == "y"

    def test_harvest_and_cache_calls_playwright(self, auth):
        p = LinkedInProvider(auth=auth)
        fake_cookies = {"li_at": "x", "__cf_bm": "y"}
        with patch(
            "libs.providers.linkedin.provider._harvest_cookies_playwright",
            return_value=fake_cookies,
        ) as mock_harvest:
            p._harvest_and_cache_cookies()
        mock_harvest.assert_called_once_with(
            li_at="test-li-at",
            jsessionid="ajax:csrf123",
            proxy_url=None,
        )

    def test_harvest_cookies_cached(self, auth):
        p = LinkedInProvider(auth=auth)
        fake_cookies = {"li_at": "x"}
        with patch(
            "libs.providers.linkedin.provider._harvest_cookies_playwright",
            return_value=fake_cookies,
        ) as mock_harvest:
            p._harvest_and_cache_cookies()
            p._get_browser_cookies()
            p._get_browser_cookies()
        mock_harvest.assert_called_once()

    def test_harvest_requires_jsessionid(self):
        auth = AccountAuth(li_at="li", jsessionid=None)
        p = LinkedInProvider(auth=auth)
        with pytest.raises(ValueError, match="JSESSIONID"):
            p._harvest_and_cache_cookies()

    def test_harvest_passes_proxy(self):
        auth = AccountAuth(li_at="li", jsessionid="csrf")
        proxy = ProxyConfig(url="http://proxy:8080")
        p = LinkedInProvider(auth=auth, proxy=proxy)
        with patch(
            "libs.providers.linkedin.provider._harvest_cookies_playwright",
            return_value={"li_at": "x"},
        ) as mock_harvest:
            p._harvest_and_cache_cookies()
        assert mock_harvest.call_args.kwargs.get("proxy_url") == "http://proxy:8080"

    def test_invalidate_cookies_forces_reharvest(self, auth):
        p = LinkedInProvider(auth=auth)
        fake_cookies_1 = {"li_at": "x", "__cf_bm": "old"}
        fake_cookies_2 = {"li_at": "x", "__cf_bm": "new"}
        with patch(
            "libs.providers.linkedin.provider._harvest_cookies_playwright",
            side_effect=[fake_cookies_1, fake_cookies_2],
        ) as mock_harvest:
            p._harvest_and_cache_cookies()
            assert p._get_browser_cookies()["__cf_bm"] == "old"
            p.invalidate_cookies()
            p._harvest_and_cache_cookies()
            assert p._get_browser_cookies()["__cf_bm"] == "new"
        assert mock_harvest.call_count == 2

    def test_harvest_blank_jsessionid_raises(self):
        auth = AccountAuth(li_at="li", jsessionid="   ")
        p = LinkedInProvider(auth=auth)
        with pytest.raises(ValueError, match="JSESSIONID"):
            p._harvest_and_cache_cookies()


# ---------------------------------------------------------------------------
# Profile ID caching
# ---------------------------------------------------------------------------

class TestGetProfileId:
    def test_profile_id_cached_after_first_call(self, auth):
        p = LinkedInProvider(auth=auth)
        mock_client = MagicMock()
        mock_client.is_closed = False
        me_resp = MagicMock()
        me_resp.status_code = 200
        me_resp.json.return_value = {"entityUrn": "urn:li:fsd_profile:ABC"}
        mock_client.get.return_value = me_resp
        with _patch_client(mock_client):
            first = p._get_profile_id()
            second = p._get_profile_id()
        assert first == "urn:li:fsd_profile:ABC"
        assert second == "urn:li:fsd_profile:ABC"
        # Only one HTTP call — second was cached
        assert mock_client.get.call_count == 1

    def test_profile_id_none_cached_when_api_fails(self, auth):
        """If /me fails, we cache None and don't retry every call."""
        p = LinkedInProvider(auth=auth)
        mock_client = MagicMock()
        mock_client.is_closed = False
        me_resp = MagicMock()
        me_resp.status_code = 403
        mock_client.get.return_value = me_resp
        with _patch_client(mock_client):
            first = p._get_profile_id()
            second = p._get_profile_id()
        assert first is None
        assert second is None
        assert mock_client.get.call_count == 1

    def test_profile_id_from_public_identifier(self, auth):
        p = LinkedInProvider(auth=auth)
        mock_client = MagicMock()
        mock_client.is_closed = False
        me_resp = MagicMock()
        me_resp.status_code = 200
        me_resp.json.return_value = {"publicIdentifier": "john-doe"}
        mock_client.get.return_value = me_resp
        with _patch_client(mock_client):
            pid = p._get_profile_id()
        assert pid == "john-doe"

    def test_profile_id_exception_returns_none(self, auth):
        p = LinkedInProvider(auth=auth)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.side_effect = httpx.ConnectError("network down")
        with _patch_client(mock_client):
            pid = p._get_profile_id()
        assert pid is None


# ---------------------------------------------------------------------------
# JSON decode safety
# ---------------------------------------------------------------------------

class TestJsonDecodeSafety:
    def test_list_threads_handles_html_error_page(self, provider):
        """HTML response from LinkedIn (e.g. login redirect) doesn't crash."""
        r = MagicMock()
        r.status_code = 200
        r.content = b"<html>Please log in</html>"
        r.raise_for_status = MagicMock()
        r.json.side_effect = ValueError("No JSON")
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = r
        with _patch_client(mock_client):
            threads = provider.list_threads()
        assert threads == []


# ---------------------------------------------------------------------------
# Skips non-dict elements
# ---------------------------------------------------------------------------

class TestDataFieldAsList:
    def test_list_threads_handles_data_field_as_list(self, provider):
        """Regression: data["data"] can be [] instead of dict — should not crash."""
        r = MagicMock()
        r.status_code = 200
        r.content = b'{"data":[]}'
        r.raise_for_status = MagicMock()
        r.json.return_value = {"data": []}
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = r
        with _patch_client(mock_client):
            threads = provider.list_threads()
        assert threads == []


class TestSkipNonDictElements:
    def test_list_threads_skips_non_dict_in_elements(self, provider):
        elems = ["string-element", 42, None, _make_conversation("urn:conv:ok")]
        data = _graphql_conversations_response(elems, sync_token=None)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            threads = provider.list_threads()
        assert len(threads) == 1
        assert threads[0].platform_thread_id == "urn:conv:ok"
