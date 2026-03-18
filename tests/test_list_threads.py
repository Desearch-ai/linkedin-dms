"""Tests for LinkedInProvider.list_threads() — HTTP calls, pagination, parsing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from libs.core.models import AccountAuth, ProxyConfig
from libs.providers.linkedin.provider import (
    _CONVERSATIONS_QID_RE,
    _DEFAULT_COUNT,
    _DELAY_BETWEEN_PAGES_S,
    _FALLBACK_CONVERSATIONS_QUERY_ID,
    _FALLBACK_MESSAGES_QUERY_ID,
    _MAX_PAGES,
    _MESSAGES_QID_RE,
    _RETRY_MAX_ATTEMPTS,
    _RETRYABLE_STATUS_CODES,
    _SCRIPT_SRC_RE,
    LinkedInProvider,
    LinkedInThread,
    _discover_query_ids,
    _extract_thread_title,
    _get_oldest_timestamp,
    _parse_graphql_threads,
    _parse_threads,
    _reset_query_id_cache,
)

# -- Fixtures ----------------------------------------------------------------


@pytest.fixture
def auth():
    return AccountAuth(li_at="fake-li-at-cookie-value", jsessionid="ajax:9999999999")


@pytest.fixture
def provider(auth):
    return LinkedInProvider(auth=auth)


@pytest.fixture
def provider_with_proxy(auth):
    return LinkedInProvider(auth=auth, proxy=ProxyConfig(url="http://proxy:8080"))


@pytest.fixture
def mock_client():
    """A mock httpx.Client for injection into provider._client."""
    client = MagicMock(spec=httpx.Client)
    client.is_closed = False
    return client


@pytest.fixture(autouse=True)
def _prefill_query_id_cache():
    """Pre-fill the queryId cache so tests don't trigger discovery HTTP calls."""
    from libs.providers.linkedin.provider import _query_id_cache, _query_id_lock

    with _query_id_lock:
        _query_id_cache["conversations"] = _FALLBACK_CONVERSATIONS_QUERY_ID
        _query_id_cache["messages"] = _FALLBACK_MESSAGES_QUERY_ID
    yield
    _reset_query_id_cache()


def _voyager_response(
    elements, total=None, start=0, count=_DEFAULT_COUNT, included=None
):
    """Build a realistic Voyager conversations response."""
    body = {
        "elements": elements,
        "paging": {"start": start, "count": count},
    }
    if total is not None:
        body["paging"]["total"] = total
    if included is not None:
        body["included"] = included
    return body


def _make_element(urn, participants=None):
    """Shorthand for a conversation element with an entityUrn."""
    elem = {"entityUrn": urn}
    if participants is not None:
        elem["participants"] = participants
    return elem


def _ok_response(json_data):
    """Build a MagicMock that passes LinkedInProvider._check_response."""
    mock = MagicMock(spec=httpx.Response)
    mock.status_code = 200
    mock.is_redirect = False
    mock.headers = {}
    mock.text = ""
    mock.json.return_value = json_data
    mock.raise_for_status = MagicMock()
    return mock


def _me_response(profile_urn="urn:li:fsd_profile:ACoAATest123"):
    """Build a /me response mock."""
    return _ok_response({"entityUrn": profile_urn})


def _graphql_conversations(elements):
    """Build a GraphQL messengerConversations response."""
    return {
        "data": {
            "messengerConversationsByCriteria": {
                "elements": elements,
            }
        }
    }


def _graphql_conv_element(urn, last_activity_at=None, participant_names=None):
    """Build a single GraphQL conversation element."""
    elem = {
        "entityUrn": urn,
        "lastActivityAt": last_activity_at or 1700000000000,
    }
    if participant_names:
        elem["conversationParticipants"] = [
            {
                "participantProfile": {
                    "firstName": n.split()[0],
                    "lastName": n.split()[-1] if len(n.split()) > 1 else "",
                }
            }
            for n in participant_names
        ]
    return elem


# -- Unit tests: response parsing --------------------------------------------


class TestParseThreads:
    def test_empty_elements(self):
        data = _voyager_response(elements=[])
        assert _parse_threads(data) == []

    def test_single_thread_with_urn(self):
        elem = _make_element("urn:li:fs_conversation:2-abc123")
        data = _voyager_response(elements=[elem])
        threads = _parse_threads(data)
        assert len(threads) == 1
        assert threads[0].platform_thread_id == "urn:li:fs_conversation:2-abc123"
        assert threads[0].raw == elem

    def test_multiple_threads(self):
        elems = [_make_element(f"urn:li:fs_conversation:{i}") for i in range(5)]
        data = _voyager_response(elements=elems)
        threads = _parse_threads(data)
        assert len(threads) == 5
        assert threads[2].platform_thread_id == "urn:li:fs_conversation:2"

    def test_skips_elements_without_entity_urn(self):
        elems = [{"someField": "value"}, _make_element("urn:li:fs_conversation:good")]
        data = _voyager_response(elements=elems)
        threads = _parse_threads(data)
        assert len(threads) == 1
        assert threads[0].platform_thread_id == "urn:li:fs_conversation:good"

    def test_title_from_included_mini_profiles(self):
        participant_urn = "urn:li:fs_miniProfile:alice123"
        elem = _make_element(
            "urn:li:fs_conversation:2-abc",
            participants=[{"participantUrn": participant_urn}],
        )
        included = [
            {"entityUrn": participant_urn, "firstName": "Alice", "lastName": "Smith"}
        ]
        data = _voyager_response(elements=[elem], included=included)
        threads = _parse_threads(data)
        assert threads[0].title == "Alice Smith"

    def test_title_multiple_participants(self):
        urn_a = "urn:li:fs_miniProfile:alice"
        urn_b = "urn:li:fs_miniProfile:bob"
        elem = _make_element(
            "urn:li:fs_conversation:group",
            participants=[{"participantUrn": urn_a}, {"participantUrn": urn_b}],
        )
        included = [
            {"entityUrn": urn_a, "firstName": "Alice", "lastName": "A"},
            {"entityUrn": urn_b, "firstName": "Bob", "lastName": "B"},
        ]
        data = _voyager_response(elements=[elem], included=included)
        threads = _parse_threads(data)
        assert threads[0].title == "Alice A, Bob B"

    def test_title_none_when_no_participants(self):
        elem = _make_element("urn:li:fs_conversation:solo")
        data = _voyager_response(elements=[elem])
        threads = _parse_threads(data)
        assert threads[0].title is None

    def test_title_none_when_participant_not_in_included(self):
        elem = _make_element(
            "urn:li:fs_conversation:orphan",
            participants=[{"participantUrn": "urn:li:fs_miniProfile:unknown"}],
        )
        data = _voyager_response(elements=[elem], included=[])
        threads = _parse_threads(data)
        assert threads[0].title is None


# -- Unit tests: GraphQL response parsing ------------------------------------


class TestParseGraphqlThreads:
    def test_empty_elements(self):
        data = _graphql_conversations(elements=[])
        assert _parse_graphql_threads(data) == []

    def test_single_conversation(self):
        elems = [_graphql_conv_element("urn:li:msg_conversation:abc123")]
        data = _graphql_conversations(elems)
        threads = _parse_graphql_threads(data)
        assert len(threads) == 1
        assert threads[0].platform_thread_id == "urn:li:msg_conversation:abc123"

    def test_multiple_conversations(self):
        elems = [
            _graphql_conv_element(f"urn:li:msg_conversation:{i}") for i in range(5)
        ]
        data = _graphql_conversations(elems)
        threads = _parse_graphql_threads(data)
        assert len(threads) == 5

    def test_title_from_participants(self):
        elems = [
            _graphql_conv_element(
                "urn:li:msg_conversation:1", participant_names=["Alice Smith"]
            )
        ]
        data = _graphql_conversations(elems)
        threads = _parse_graphql_threads(data)
        assert threads[0].title == "Alice Smith"

    def test_title_multiple_participants(self):
        elems = [
            _graphql_conv_element(
                "urn:li:msg_conversation:1",
                participant_names=["Alice A", "Bob B"],
            )
        ]
        data = _graphql_conversations(elems)
        threads = _parse_graphql_threads(data)
        assert threads[0].title == "Alice A, Bob B"


class TestGetOldestTimestamp:
    def test_returns_oldest(self):
        elems = [
            _graphql_conv_element("urn:1", last_activity_at=3000),
            _graphql_conv_element("urn:2", last_activity_at=1000),
            _graphql_conv_element("urn:3", last_activity_at=2000),
        ]
        data = _graphql_conversations(elems)
        assert _get_oldest_timestamp(data) == 1000

    def test_returns_none_for_empty(self):
        data = _graphql_conversations([])
        assert _get_oldest_timestamp(data) is None


# -- Unit tests: header and cookie building ----------------------------------


class TestBuildHeaders:
    def test_includes_csrf_token(self, provider):
        headers = provider._build_headers()
        assert headers["csrf-token"] == "ajax:9999999999"
        assert "user-agent" in headers
        assert "x-restli-protocol-version" in headers

    def test_csrf_token_strips_quotes(self):
        """JSESSIONID cookie value has surrounding quotes; csrf-token must not."""
        auth = AccountAuth(li_at="fake", jsessionid='"ajax:1234567890"')
        p = LinkedInProvider(auth=auth)
        headers = p._build_headers()
        assert headers["csrf-token"] == "ajax:1234567890"

    def test_no_csrf_without_jsessionid(self):
        auth = AccountAuth(li_at="fake", jsessionid=None)
        p = LinkedInProvider(auth=auth)
        headers = p._build_headers()
        assert "csrf-token" not in headers

    def test_build_cookies(self, provider):
        cookies = provider._build_cookies()
        assert cookies["li_at"] == "fake-li-at-cookie-value"
        assert cookies["JSESSIONID"] == "ajax:9999999999"

    def test_build_cookies_without_jsessionid(self):
        auth = AccountAuth(li_at="fake", jsessionid=None)
        p = LinkedInProvider(auth=auth)
        cookies = p._build_cookies()
        assert "JSESSIONID" not in cookies

    def test_proxy_url(self, provider_with_proxy):
        assert provider_with_proxy._get_proxy_url() == "http://proxy:8080"

    def test_proxy_url_none(self, provider):
        assert provider._get_proxy_url() is None


# -- Integration-style tests: list_threads with mocked HTTP ------------------


class TestListThreads:
    def test_single_page(self, provider, mock_client):
        """When LinkedIn returns fewer than DEFAULT_COUNT, no second request."""
        provider._client = mock_client
        elems = [
            _graphql_conv_element(f"urn:li:msg_conversation:{i}") for i in range(3)
        ]
        me_resp = _me_response()
        graphql_resp = _ok_response(_graphql_conversations(elems))
        mock_client.get.side_effect = [me_resp, graphql_resp]

        threads = provider.list_threads()

        assert len(threads) == 3
        assert all(isinstance(t, LinkedInThread) for t in threads)
        assert mock_client.get.call_count == 2  # /me + 1 graphql page

    def test_pagination_two_pages(self, provider, mock_client):
        """When first page is full, fetches second page."""
        provider._client = mock_client
        page1_elems = [
            _graphql_conv_element(f"urn:conv:{i}", last_activity_at=2000 - i)
            for i in range(_DEFAULT_COUNT)
        ]
        page2_elems = [
            _graphql_conv_element(
                f"urn:conv:{_DEFAULT_COUNT + i}", last_activity_at=1000 - i
            )
            for i in range(5)
        ]

        me_resp = _me_response()
        resp1 = _ok_response(_graphql_conversations(page1_elems))
        resp2 = _ok_response(_graphql_conversations(page2_elems))
        mock_client.get.side_effect = [me_resp, resp1, resp2]

        with patch("libs.providers.linkedin.provider.time.sleep"):
            threads = provider.list_threads()

        assert len(threads) == _DEFAULT_COUNT + 5
        assert mock_client.get.call_count == 3  # /me + 2 graphql pages

    def test_empty_inbox(self, provider, mock_client):
        provider._client = mock_client
        me_resp = _me_response()
        graphql_resp = _ok_response(_graphql_conversations([]))
        mock_client.get.side_effect = [me_resp, graphql_resp]

        threads = provider.list_threads()

        assert threads == []

    def test_http_error_propagates(self, provider, mock_client):
        """HTTP errors from LinkedIn should bubble up, not be swallowed."""
        provider._client = mock_client
        error_resp = MagicMock(spec=httpx.Response)
        error_resp.status_code = 403
        error_resp.is_redirect = False
        error_resp.headers = {}
        error_resp.text = "Forbidden"
        error_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403 Forbidden",
            request=MagicMock(),
            response=error_resp,
        )
        # /me fails with 403
        mock_client.get.return_value = error_resp

        with pytest.raises(httpx.HTTPStatusError):
            provider.list_threads()

    def test_requires_jsessionid(self):
        """list_threads raises ValueError when JSESSIONID is missing."""
        auth = AccountAuth(li_at="fake-cookie", jsessionid=None)
        p = LinkedInProvider(auth=auth)
        with pytest.raises(ValueError, match="JSESSIONID"):
            p.list_threads()

    def test_uses_proxy(self, provider_with_proxy):
        """Proxy URL is passed through to httpx.Client."""
        me_resp = _me_response()
        graphql_resp = _ok_response(_graphql_conversations([]))

        with patch("libs.providers.linkedin.provider.httpx.Client") as MockClient:
            client_instance = MockClient.return_value
            client_instance.is_closed = False
            client_instance.get.side_effect = [me_resp, graphql_resp]

            provider_with_proxy.list_threads()

        MockClient.assert_called_once_with(
            proxy="http://proxy:8080",
            timeout=30.0,
            follow_redirects=False,
        )

    def test_cookies_not_in_headers(self, provider, mock_client):
        """Cookies are passed via the cookies param, not leaked into headers."""
        provider._client = mock_client
        me_resp = _me_response()
        graphql_resp = _ok_response(_graphql_conversations([]))
        mock_client.get.side_effect = [me_resp, graphql_resp]

        provider.list_threads()

        # Check the last call (graphql) — cookies should be in the cookies param
        call_kwargs = mock_client.get.call_args.kwargs
        headers = call_kwargs["headers"]
        assert "li_at" not in str(headers.get("cookie", ""))
        assert "fake-li-at-cookie-value" not in str(headers)
        assert call_kwargs["cookies"]["li_at"] == "fake-li-at-cookie-value"

    def test_max_pages_safety_limit(self, provider, mock_client):
        """Pagination stops after _MAX_PAGES even if more data exists."""
        provider._client = mock_client

        def make_full_page(base_ts):
            return [
                _graphql_conv_element(
                    f"urn:conv:{base_ts - i}", last_activity_at=base_ts - i
                )
                for i in range(_DEFAULT_COUNT)
            ]

        me_resp = _me_response()
        pages = []
        base = 100000
        for p in range(_MAX_PAGES + 1):
            ts = base - (p * _DEFAULT_COUNT)
            pages.append(_ok_response(_graphql_conversations(make_full_page(ts))))

        mock_client.get.side_effect = [me_resp] + pages

        with patch("libs.providers.linkedin.provider.time.sleep"):
            threads = provider.list_threads()

        # /me + _MAX_PAGES graphql calls
        assert mock_client.get.call_count == 1 + _MAX_PAGES
        assert len(threads) == _MAX_PAGES * _DEFAULT_COUNT

    def test_deduplicates_across_pages(self, provider, mock_client):
        """Same entityUrn on two pages is returned only once."""
        provider._client = mock_client
        page1_elems = [
            _graphql_conv_element("urn:conv:dup", last_activity_at=2000),
            _graphql_conv_element("urn:conv:1", last_activity_at=1999),
        ] + [
            _graphql_conv_element(f"urn:conv:p1-{i}", last_activity_at=1998 - i)
            for i in range(_DEFAULT_COUNT - 2)
        ]
        page2_elems = [
            _graphql_conv_element("urn:conv:dup", last_activity_at=2000),
            _graphql_conv_element("urn:conv:2", last_activity_at=900),
        ]

        me_resp = _me_response()
        resp1 = _ok_response(_graphql_conversations(page1_elems))
        resp2 = _ok_response(_graphql_conversations(page2_elems))
        mock_client.get.side_effect = [me_resp, resp1, resp2]

        with patch("libs.providers.linkedin.provider.time.sleep"):
            threads = provider.list_threads()

        urns = [t.platform_thread_id for t in threads]
        assert urns.count("urn:conv:dup") == 1
        assert "urn:conv:1" in urns
        assert "urn:conv:2" in urns

    def test_sleeps_between_pages(self, provider, mock_client):
        """Rate limiting: sleeps between pagination requests."""
        provider._client = mock_client
        page1_elems = [
            _graphql_conv_element(f"urn:conv:{i}", last_activity_at=2000 - i)
            for i in range(_DEFAULT_COUNT)
        ]
        page2_elems = [_graphql_conv_element("urn:conv:last", last_activity_at=500)]

        me_resp = _me_response()
        resp1 = _ok_response(_graphql_conversations(page1_elems))
        resp2 = _ok_response(_graphql_conversations(page2_elems))
        mock_client.get.side_effect = [me_resp, resp1, resp2]

        with patch("libs.providers.linkedin.provider.time.sleep") as mock_sleep:
            provider.list_threads()

        mock_sleep.assert_called_once_with(_DELAY_BETWEEN_PAGES_S)

    def test_no_sleep_on_single_page(self, provider, mock_client):
        """No rate-limit sleep when only one page is fetched."""
        provider._client = mock_client
        me_resp = _me_response()
        graphql_resp = _ok_response(
            _graphql_conversations([_graphql_conv_element("urn:conv:1")])
        )
        mock_client.get.side_effect = [me_resp, graphql_resp]

        with patch("libs.providers.linkedin.provider.time.sleep") as mock_sleep:
            provider.list_threads()

        mock_sleep.assert_not_called()

    def test_retries_on_429_then_succeeds(self, provider, mock_client):
        """Retries transient 429 and succeeds on next attempt."""
        provider._client = mock_client
        me_resp = _me_response()

        resp_429 = MagicMock(spec=httpx.Response)
        resp_429.status_code = 429
        resp_429.headers = {}
        resp_429.request = MagicMock()

        graphql_resp = _ok_response(
            _graphql_conversations([_graphql_conv_element("urn:conv:1")])
        )
        mock_client.get.side_effect = [me_resp, resp_429, graphql_resp]

        with patch("libs.providers.linkedin.provider.time.sleep"):
            threads = provider.list_threads()

        assert len(threads) == 1

    def test_retry_honours_retry_after_header(self, provider, mock_client):
        """Retry delay respects Retry-After header from LinkedIn."""
        provider._client = mock_client
        me_resp = _me_response()

        resp_429 = MagicMock(spec=httpx.Response)
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "10"}
        resp_429.request = MagicMock()

        graphql_resp = _ok_response(_graphql_conversations([]))
        mock_client.get.side_effect = [me_resp, resp_429, graphql_resp]

        with patch("libs.providers.linkedin.provider.time.sleep") as mock_sleep:
            provider.list_threads()

        # The retry sleep should be at least 10s (from Retry-After)
        assert mock_sleep.call_args[0][0] >= 10.0

    def test_exhausts_retries_on_503(self, provider, mock_client):
        """Raises after exhausting retry attempts on persistent 5xx."""
        provider._client = mock_client
        me_resp = _me_response()

        resp_503 = MagicMock(spec=httpx.Response)
        resp_503.status_code = 503
        resp_503.headers = {}
        resp_503.request = MagicMock()

        mock_client.get.side_effect = [me_resp] + [resp_503] * _RETRY_MAX_ATTEMPTS

        with patch("libs.providers.linkedin.provider.time.sleep"):
            with pytest.raises(httpx.HTTPStatusError):
                provider.list_threads()

    def test_no_retry_on_403(self, provider, mock_client):
        """Non-retryable errors (403) are not retried."""
        provider._client = mock_client
        me_resp = _me_response()

        resp_403 = MagicMock(spec=httpx.Response)
        resp_403.status_code = 403
        resp_403.is_redirect = False
        resp_403.headers = {}
        resp_403.text = "Forbidden"
        resp_403.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403", request=MagicMock(), response=resp_403
        )
        mock_client.get.side_effect = [me_resp, resp_403]

        with patch("libs.providers.linkedin.provider.time.sleep") as mock_sleep:
            with pytest.raises(httpx.HTTPStatusError):
                provider.list_threads()

        # time.sleep should NOT be called for retry (only _DELAY_BETWEEN_PAGES_S is possible)
        mock_sleep.assert_not_called()

    def test_context_manager_closes_client(self, auth):
        """Provider used as context manager closes the httpx client."""
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.is_closed = False

        with LinkedInProvider(auth=auth) as p:
            p._client = mock_client

        mock_client.close.assert_called_once()


# -- Edge case: _extract_thread_title variations -----------------------------


class TestExtractThreadTitle:
    def test_voyager_messaging_member_key(self):
        """Handles the alternate participant key LinkedIn sometimes uses."""
        urn = "urn:li:fs_miniProfile:alice"
        element = {
            "entityUrn": "urn:li:fs_conversation:1",
            "participants": [{"*com.linkedin.voyager.messaging.MessagingMember": urn}],
        }
        included = [{"entityUrn": urn, "firstName": "Alice", "lastName": "W"}]
        assert _extract_thread_title(element, included) == "Alice W"

    def test_entity_urn_fallback(self):
        """Falls back to entityUrn on participant when other keys are missing."""
        urn = "urn:li:fs_miniProfile:bob"
        element = {
            "entityUrn": "urn:li:fs_conversation:1",
            "participants": [{"entityUrn": urn}],
        }
        included = [{"entityUrn": urn, "firstName": "Bob", "lastName": ""}]
        assert _extract_thread_title(element, included) == "Bob"

    def test_empty_name_fields(self):
        urn = "urn:li:fs_miniProfile:empty"
        element = {
            "entityUrn": "urn:li:fs_conversation:1",
            "participants": [{"participantUrn": urn}],
        }
        included = [{"entityUrn": urn, "firstName": "", "lastName": ""}]
        # Empty name → no title
        assert _extract_thread_title(element, included) is None


# -- Unit tests: queryId regex patterns ----------------------------------------


class TestQueryIdRegex:
    def test_conversations_pattern_double_quotes(self):
        js = 'queryId:"messengerConversations.9501074288a12f3ae9e3c7ea243bccbf"'
        match = _CONVERSATIONS_QID_RE.search(js)
        assert match is not None
        assert (
            match.group(1) == "messengerConversations.9501074288a12f3ae9e3c7ea243bccbf"
        )

    def test_conversations_pattern_single_quotes(self):
        js = "queryId:'messengerConversations.abcdef1234567890abcdef'"
        match = _CONVERSATIONS_QID_RE.search(js)
        assert match is not None
        assert match.group(1) == "messengerConversations.abcdef1234567890abcdef"

    def test_messages_pattern(self):
        js = 'queryId:"messengerMessages.5846eeb71c981f11e0134cb6626cc314"'
        match = _MESSAGES_QID_RE.search(js)
        assert match is not None
        assert match.group(1) == "messengerMessages.5846eeb71c981f11e0134cb6626cc314"

    def test_no_match_on_unrelated(self):
        js = 'queryId:"somethingElse.abc123"'
        assert _CONVERSATIONS_QID_RE.search(js) is None
        assert _MESSAGES_QID_RE.search(js) is None

    def test_pattern_with_spaces(self):
        js = 'queryId: "messengerConversations.aaaa1111bbbb2222cccc3333"'
        match = _CONVERSATIONS_QID_RE.search(js)
        assert match is not None

    def test_script_src_extraction(self):
        html = '<script src="https://static.licdn.com/aero-v1/sc/h/abc123"></script>'
        urls = _SCRIPT_SRC_RE.findall(html)
        assert len(urls) == 1
        assert urls[0] == "https://static.licdn.com/aero-v1/sc/h/abc123"


# -- Integration-style tests: queryId discovery --------------------------------


class TestDiscoverQueryIds:
    def setup_method(self):
        _reset_query_id_cache()

    def test_discovers_both_ids_from_bundle(self):
        """Discovery finds both queryIds from JS bundle content."""
        html = (
            "<html><head>"
            '<script src="https://static.licdn.com/bundle.js"></script>'
            "</head></html>"
        )
        js_content = (
            'var x=1;queryId:"messengerConversations.aaaa1111bbbb2222cccc3333dddd4444";'
            'queryId:"messengerMessages.eeee5555ffff6666aaaa7777bbbb8888";'
        )

        html_resp = _ok_response(None)
        html_resp.status_code = 200
        html_resp.text = html

        js_resp = _ok_response(None)
        js_resp.status_code = 200
        js_resp.text = js_content

        with patch("libs.providers.linkedin.provider.httpx.Client") as MockClient:
            client_instance = MockClient.return_value.__enter__.return_value
            client_instance.get.side_effect = [html_resp, js_resp]

            conv_id, msg_id = _discover_query_ids(
                {"li_at": "fake", "JSESSIONID": "ajax:123"}, {}, None
            )

        assert conv_id == "messengerConversations.aaaa1111bbbb2222cccc3333dddd4444"
        assert msg_id == "messengerMessages.eeee5555ffff6666aaaa7777bbbb8888"

    def test_falls_back_when_page_returns_error(self):
        """Falls back to hardcoded IDs when /messaging returns non-200."""
        html_resp = _ok_response(None)
        html_resp.status_code = 403
        html_resp.text = "Forbidden"

        with patch("libs.providers.linkedin.provider.httpx.Client") as MockClient:
            client_instance = MockClient.return_value.__enter__.return_value
            client_instance.get.return_value = html_resp

            conv_id, msg_id = _discover_query_ids({"li_at": "fake"}, {}, None)

        assert conv_id == _FALLBACK_CONVERSATIONS_QUERY_ID
        assert msg_id == _FALLBACK_MESSAGES_QUERY_ID

    def test_falls_back_when_no_ids_in_bundles(self):
        """Falls back when JS bundles don't contain queryId patterns."""
        html = '<html><script src="/bundle.js"></script></html>'
        js_content = "var x = 1; // no queryIds here"

        html_resp = _ok_response(None)
        html_resp.status_code = 200
        html_resp.text = html

        js_resp = _ok_response(None)
        js_resp.status_code = 200
        js_resp.text = js_content

        with patch("libs.providers.linkedin.provider.httpx.Client") as MockClient:
            client_instance = MockClient.return_value.__enter__.return_value
            client_instance.get.side_effect = [html_resp, js_resp]

            conv_id, msg_id = _discover_query_ids({"li_at": "fake"}, {}, None)

        assert conv_id == _FALLBACK_CONVERSATIONS_QUERY_ID
        assert msg_id == _FALLBACK_MESSAGES_QUERY_ID

    def test_caches_results(self):
        """Second call returns cached result without HTTP requests."""
        html = '<html><script src="/bundle.js"></script></html>'
        js_content = (
            'queryId:"messengerConversations.aabb11223344556677889900aabb1122";'
            'queryId:"messengerMessages.ccdd33445566778899001122ccdd3344";'
        )

        html_resp = _ok_response(None)
        html_resp.status_code = 200
        html_resp.text = html

        js_resp = _ok_response(None)
        js_resp.status_code = 200
        js_resp.text = js_content

        with patch("libs.providers.linkedin.provider.httpx.Client") as MockClient:
            client_instance = MockClient.return_value.__enter__.return_value
            client_instance.get.side_effect = [html_resp, js_resp]

            conv1, msg1 = _discover_query_ids({"li_at": "fake"}, {}, None)

        # Second call — should use cache, no HTTP
        with patch("libs.providers.linkedin.provider.httpx.Client") as MockClient:
            conv2, msg2 = _discover_query_ids({"li_at": "fake"}, {}, None)
            MockClient.assert_not_called()

        assert conv1 == conv2
        assert msg1 == msg2

    def test_falls_back_on_http_error(self):
        """Falls back gracefully when httpx raises an error."""
        with patch("libs.providers.linkedin.provider.httpx.Client") as MockClient:
            client_instance = MockClient.return_value.__enter__.return_value
            client_instance.get.side_effect = httpx.ConnectError("Connection refused")

            conv_id, msg_id = _discover_query_ids({"li_at": "fake"}, {}, None)

        assert conv_id == _FALLBACK_CONVERSATIONS_QUERY_ID
        assert msg_id == _FALLBACK_MESSAGES_QUERY_ID

    def test_partial_discovery_uses_fallback_for_missing(self):
        """If only one queryId is found, the other falls back."""
        html = '<html><script src="/bundle.js"></script></html>'
        js_content = (
            'queryId:"messengerConversations.aabb11cc22dd33ee44ff5566aabb1122";'
        )

        html_resp = _ok_response(None)
        html_resp.status_code = 200
        html_resp.text = html

        js_resp = _ok_response(None)
        js_resp.status_code = 200
        js_resp.text = js_content

        with patch("libs.providers.linkedin.provider.httpx.Client") as MockClient:
            client_instance = MockClient.return_value.__enter__.return_value
            client_instance.get.side_effect = [html_resp, js_resp]

            conv_id, msg_id = _discover_query_ids({"li_at": "fake"}, {}, None)

        assert conv_id == "messengerConversations.aabb11cc22dd33ee44ff5566aabb1122"
        assert msg_id == _FALLBACK_MESSAGES_QUERY_ID
