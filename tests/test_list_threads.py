"""Tests for LinkedInProvider.list_threads() — HTTP calls, pagination, parsing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from libs.core.models import AccountAuth, ProxyConfig
from libs.providers.linkedin.provider import (
    _DEFAULT_COUNT,
    _MAX_PAGES,
    LinkedInProvider,
    LinkedInThread,
    _extract_thread_title,
    _get_oldest_timestamp,
    _parse_graphql_threads,
    _parse_threads,
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


def _graphql_conversations(elements, has_more=False):
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
    def test_single_page(self, provider):
        """When LinkedIn returns fewer than DEFAULT_COUNT, no second request."""
        elems = [
            _graphql_conv_element(f"urn:li:msg_conversation:{i}") for i in range(3)
        ]
        me_resp = _me_response()
        graphql_resp = _ok_response(_graphql_conversations(elems))

        with patch("libs.providers.linkedin.provider.httpx.Client") as MockClient:
            client_instance = MockClient.return_value.__enter__.return_value
            client_instance.get.side_effect = [me_resp, graphql_resp]

            threads = provider.list_threads()

        assert len(threads) == 3
        assert all(isinstance(t, LinkedInThread) for t in threads)
        assert client_instance.get.call_count == 2  # /me + 1 graphql page

    def test_pagination_two_pages(self, provider):
        """When first page is full, fetches second page."""
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

        with patch("libs.providers.linkedin.provider.httpx.Client") as MockClient:
            client_instance = MockClient.return_value.__enter__.return_value
            client_instance.get.side_effect = [me_resp, resp1, resp2]

            threads = provider.list_threads()

        assert len(threads) == _DEFAULT_COUNT + 5
        assert client_instance.get.call_count == 3  # /me + 2 graphql pages

    def test_empty_inbox(self, provider):
        me_resp = _me_response()
        graphql_resp = _ok_response(_graphql_conversations([]))

        with patch("libs.providers.linkedin.provider.httpx.Client") as MockClient:
            client_instance = MockClient.return_value.__enter__.return_value
            client_instance.get.side_effect = [me_resp, graphql_resp]

            threads = provider.list_threads()

        assert threads == []

    def test_http_error_propagates(self, provider):
        """HTTP errors from LinkedIn should bubble up, not be swallowed."""
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 403
        mock_resp.is_redirect = False
        mock_resp.headers = {}
        mock_resp.text = "Forbidden"
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403 Forbidden",
            request=MagicMock(),
            response=mock_resp,
        )

        with patch("libs.providers.linkedin.provider.httpx.Client") as MockClient:
            client_instance = MockClient.return_value.__enter__.return_value
            # /me fails with 403
            client_instance.get.return_value = mock_resp

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
            client_instance = MockClient.return_value.__enter__.return_value
            client_instance.get.side_effect = [me_resp, graphql_resp]

            provider_with_proxy.list_threads()

        MockClient.assert_called_once_with(
            proxy="http://proxy:8080",
            timeout=30.0,
            follow_redirects=False,
        )

    def test_cookies_not_in_headers(self, provider):
        """Cookies are passed via the cookies param, not leaked into headers."""
        me_resp = _me_response()
        graphql_resp = _ok_response(_graphql_conversations([]))

        with patch("libs.providers.linkedin.provider.httpx.Client") as MockClient:
            client_instance = MockClient.return_value.__enter__.return_value
            client_instance.get.side_effect = [me_resp, graphql_resp]

            provider.list_threads()

        # Check the last call (graphql) — cookies should be in the cookies param
        call_kwargs = client_instance.get.call_args.kwargs
        headers = call_kwargs["headers"]
        assert "li_at" not in str(headers.get("cookie", ""))
        assert "fake-li-at-cookie-value" not in str(headers)
        assert call_kwargs["cookies"]["li_at"] == "fake-li-at-cookie-value"

    def test_max_pages_safety_limit(self, provider):
        """Pagination stops after _MAX_PAGES even if more data exists."""

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

        with patch("libs.providers.linkedin.provider.httpx.Client") as MockClient:
            client_instance = MockClient.return_value.__enter__.return_value
            client_instance.get.side_effect = [me_resp] + pages

            threads = provider.list_threads()

        # /me + _MAX_PAGES graphql calls
        assert client_instance.get.call_count == 1 + _MAX_PAGES
        assert len(threads) == _MAX_PAGES * _DEFAULT_COUNT


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
