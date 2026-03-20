"""Tests for LinkedInProvider.fetch_messages() — GraphQL messaging API."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from libs.core.models import AccountAuth, ProxyConfig
from libs.providers.linkedin.provider import (
    _FALLBACK_CONVERSATIONS_QUERY_ID,
    _FALLBACK_MESSAGES_QUERY_ID,
    _RETRY_MAX_ATTEMPTS,
    LinkedInMessage,
    LinkedInProvider,
    _parse_graphql_messages,
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


# -- Response builders -------------------------------------------------------


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


def _graphql_messages(elements, sync_token=None):
    """Build a GraphQL messengerMessages response."""
    metadata = {}
    if sync_token:
        metadata["syncToken"] = sync_token
    return {
        "data": {
            "messengerMessagesBySyncToken": {
                "elements": elements,
                "metadata": metadata,
            }
        }
    }


def _msg_element(
    urn,
    delivered_at=1700000000000,
    sender_urn=None,
    text="Hello",
):
    """Build a single message element for GraphQL responses."""
    elem = {
        "entityUrn": urn,
        "deliveredAt": delivered_at,
        "body": {"text": text},
    }
    if sender_urn:
        elem["sender"] = {
            "participantProfile": {"entityUrn": sender_urn},
        }
    return elem


# -- Unit tests: _parse_graphql_messages -------------------------------------


class TestParseGraphqlMessages:
    def test_single_message(self):
        data = _graphql_messages([_msg_element("urn:msg:1", text="Hi")])
        msgs, cursor = _parse_graphql_messages(data, "urn:conv:1")
        assert len(msgs) == 1
        assert msgs[0].platform_message_id == "urn:msg:1"
        assert msgs[0].text == "Hi"

    def test_returns_sync_token_as_cursor(self):
        data = _graphql_messages(
            [_msg_element("urn:msg:1")], sync_token="abc-sync-123"
        )
        msgs, cursor = _parse_graphql_messages(data, "urn:conv:1")
        assert cursor == "abc-sync-123"

    def test_no_cursor_without_sync_token(self):
        data = _graphql_messages([_msg_element("urn:msg:1")])
        _, cursor = _parse_graphql_messages(data, "urn:conv:1")
        assert cursor is None

    def test_empty_elements(self):
        data = _graphql_messages([])
        msgs, cursor = _parse_graphql_messages(data, "urn:conv:1")
        assert msgs == []
        assert cursor is None

    def test_sorted_oldest_first(self):
        elems = [
            _msg_element("urn:msg:new", delivered_at=1700000003000),
            _msg_element("urn:msg:old", delivered_at=1700000001000),
            _msg_element("urn:msg:mid", delivered_at=1700000002000),
        ]
        data = _graphql_messages(elems)
        msgs, _ = _parse_graphql_messages(data, "urn:conv:1")
        assert [m.platform_message_id for m in msgs] == [
            "urn:msg:old",
            "urn:msg:mid",
            "urn:msg:new",
        ]

    def test_timestamp_parsed_correctly(self):
        ts = 1700000000000  # 2023-11-14T22:13:20Z
        data = _graphql_messages([_msg_element("urn:msg:1", delivered_at=ts)])
        msgs, _ = _parse_graphql_messages(data, "urn:conv:1")
        assert msgs[0].sent_at == datetime.fromtimestamp(ts / 1000, tz=timezone.utc)

    def test_skips_elements_without_urn(self):
        data = _graphql_messages([{"deliveredAt": 1700000000000, "body": {"text": "x"}}])
        msgs, _ = _parse_graphql_messages(data, "urn:conv:1")
        assert msgs == []

    def test_backend_urn_fallback(self):
        elem = {"backendUrn": "urn:msg:backend:1", "deliveredAt": 1700000000000}
        data = _graphql_messages([elem])
        msgs, _ = _parse_graphql_messages(data, "urn:conv:1")
        assert msgs[0].platform_message_id == "urn:msg:backend:1"

    def test_text_from_string_body(self):
        elem = {"entityUrn": "urn:msg:1", "deliveredAt": 1700000000000, "body": "string"}
        data = _graphql_messages([elem])
        msgs, _ = _parse_graphql_messages(data, "urn:conv:1")
        assert msgs[0].text == "string"

    def test_text_none_when_empty_body(self):
        elem = {"entityUrn": "urn:msg:1", "deliveredAt": 1700000000000, "body": {}}
        data = _graphql_messages([elem])
        msgs, _ = _parse_graphql_messages(data, "urn:conv:1")
        assert msgs[0].text is None

    def test_raw_preserved(self):
        elem = _msg_element("urn:msg:1")
        data = _graphql_messages([elem])
        msgs, _ = _parse_graphql_messages(data, "urn:conv:1")
        assert msgs[0].raw == elem

    def test_sender_urn_extracted(self):
        elem = _msg_element("urn:msg:1", sender_urn="urn:li:fsd_profile:SENDER")
        data = _graphql_messages([elem])
        msgs, _ = _parse_graphql_messages(data, "urn:conv:1")
        assert msgs[0].sender == "urn:li:fsd_profile:SENDER"

    def test_sender_none_without_sender_field(self):
        elem = _msg_element("urn:msg:1")
        data = _graphql_messages([elem])
        msgs, _ = _parse_graphql_messages(data, "urn:conv:1")
        assert msgs[0].sender is None

    def test_deduplicates_by_message_id(self):
        """Duplicate message IDs within a page are returned only once."""
        dup = _msg_element("urn:msg:dup", text="first")
        dup2 = _msg_element("urn:msg:dup", text="second")
        unique = _msg_element("urn:msg:unique")
        data = _graphql_messages([dup, dup2, unique])
        msgs, _ = _parse_graphql_messages(data, "urn:conv:1")
        assert len(msgs) == 2
        ids = [m.platform_message_id for m in msgs]
        assert ids.count("urn:msg:dup") == 1
        assert "urn:msg:unique" in ids


# -- Integration: fetch_messages with mocked HTTP ----------------------------


class TestFetchMessages:
    def test_basic_fetch(self, provider, mock_client):
        provider._client = mock_client
        data = _graphql_messages([_msg_element("urn:msg:1", text="Hello")])
        mock_client.get.return_value = _ok_response(data)

        msgs, cursor = provider.fetch_messages(
            platform_thread_id="urn:li:conv:1",
            cursor=None,
        )

        assert len(msgs) == 1
        assert msgs[0].text == "Hello"
        assert isinstance(msgs[0], LinkedInMessage)

    def test_empty_response(self, provider, mock_client):
        provider._client = mock_client
        data = _graphql_messages([])
        mock_client.get.return_value = _ok_response(data)

        msgs, cursor = provider.fetch_messages(
            platform_thread_id="urn:li:conv:1",
            cursor=None,
        )

        assert msgs == []
        assert cursor is None

    def test_returns_sync_token_cursor(self, provider, mock_client):
        provider._client = mock_client
        data = _graphql_messages(
            [_msg_element("urn:msg:1")], sync_token="next-sync-abc"
        )
        mock_client.get.return_value = _ok_response(data)

        msgs, cursor = provider.fetch_messages(
            platform_thread_id="urn:li:conv:1",
            cursor=None,
        )

        assert cursor == "next-sync-abc"

    def test_cursor_passed_in_url(self, provider, mock_client):
        provider._client = mock_client
        data = _graphql_messages([])
        mock_client.get.return_value = _ok_response(data)

        provider.fetch_messages(
            platform_thread_id="urn:li:conv:1",
            cursor="prev-sync-token",
        )

        url = mock_client.get.call_args[0][0]
        assert "syncToken" in url

    def test_conversation_urn_in_url(self, provider, mock_client):
        provider._client = mock_client
        data = _graphql_messages([])
        mock_client.get.return_value = _ok_response(data)

        provider.fetch_messages(
            platform_thread_id="urn:li:msg_conversation:ABC",
            cursor=None,
        )

        url = mock_client.get.call_args[0][0]
        assert "conversationUrn" in url
        assert "messengerMessages" in url

    def test_messages_sorted_chronologically(self, provider, mock_client):
        provider._client = mock_client
        data = _graphql_messages([
            _msg_element("urn:msg:new", delivered_at=1700000002000),
            _msg_element("urn:msg:old", delivered_at=1700000001000),
        ])
        mock_client.get.return_value = _ok_response(data)

        msgs, _ = provider.fetch_messages(
            platform_thread_id="urn:li:conv:1",
            cursor=None,
        )

        assert msgs[0].platform_message_id == "urn:msg:old"
        assert msgs[1].platform_message_id == "urn:msg:new"

    def test_http_error_propagates(self, provider, mock_client):
        provider._client = mock_client
        resp_403 = MagicMock(spec=httpx.Response)
        resp_403.status_code = 403
        resp_403.is_redirect = False
        resp_403.headers = {}
        resp_403.text = "Forbidden"
        resp_403.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403", request=MagicMock(), response=resp_403
        )
        mock_client.get.return_value = resp_403

        with pytest.raises(httpx.HTTPStatusError):
            provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
            )

    def test_requires_jsessionid(self):
        auth = AccountAuth(li_at="fake", jsessionid=None)
        p = LinkedInProvider(auth=auth)
        with pytest.raises(ValueError, match="JSESSIONID"):
            p.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
            )

    def test_invalid_limit_too_low(self, provider):
        with pytest.raises(ValueError, match="limit"):
            provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
                limit=0,
            )

    def test_invalid_limit_too_high(self, provider):
        with pytest.raises(ValueError, match="limit"):
            provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
                limit=201,
            )

    def test_valid_limit_boundaries(self, provider, mock_client):
        """Limits 1 and 200 should be accepted without error."""
        provider._client = mock_client
        data = _graphql_messages([])
        mock_client.get.return_value = _ok_response(data)

        # Should not raise
        provider.fetch_messages(
            platform_thread_id="urn:li:conv:1", cursor=None, limit=1
        )
        provider.fetch_messages(
            platform_thread_id="urn:li:conv:1", cursor=None, limit=200
        )

    def test_retries_on_429(self, provider, mock_client):
        provider._client = mock_client
        resp_429 = MagicMock(spec=httpx.Response)
        resp_429.status_code = 429
        resp_429.headers = {}
        resp_429.request = MagicMock()

        data = _graphql_messages([_msg_element("urn:msg:1")])
        mock_client.get.side_effect = [resp_429, _ok_response(data)]

        with patch("libs.providers.linkedin.provider.time.sleep"):
            msgs, _ = provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
            )

        assert len(msgs) == 1

    def test_exhausts_retries_on_502(self, provider, mock_client):
        provider._client = mock_client
        resp_502 = MagicMock(spec=httpx.Response)
        resp_502.status_code = 502
        resp_502.headers = {}
        resp_502.request = MagicMock()

        mock_client.get.return_value = resp_502

        with patch("libs.providers.linkedin.provider.time.sleep"):
            with pytest.raises(httpx.HTTPStatusError):
                provider.fetch_messages(
                    platform_thread_id="urn:li:conv:1",
                    cursor=None,
                )

        # Should have attempted _RETRY_MAX_ATTEMPTS times
        assert mock_client.get.call_count == _RETRY_MAX_ATTEMPTS

    def test_uses_proxy(self):
        """Proxy URL is passed through to httpx.Client."""
        auth = AccountAuth(li_at="fake-li-at", jsessionid="ajax:123")
        proxy = ProxyConfig(url="http://proxy:8080")
        p = LinkedInProvider(auth=auth, proxy=proxy)

        data = _graphql_messages([])
        with patch("libs.providers.linkedin.provider.httpx.Client") as MockClient:
            client_instance = MockClient.return_value
            client_instance.is_closed = False
            client_instance.get.return_value = _ok_response(data)

            p.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
            )

        MockClient.assert_called_once_with(
            proxy="http://proxy:8080",
            timeout=30.0,
            follow_redirects=False,
        )

    def test_cookies_not_leaked_into_headers(self, provider, mock_client):
        provider._client = mock_client
        data = _graphql_messages([])
        mock_client.get.return_value = _ok_response(data)

        provider.fetch_messages(
            platform_thread_id="urn:li:conv:1",
            cursor=None,
        )

        call_kwargs = mock_client.get.call_args.kwargs
        headers_str = str(call_kwargs["headers"])
        assert "fake-li-at-cookie-value" not in headers_str
        assert call_kwargs["cookies"]["li_at"] == "fake-li-at-cookie-value"

    def test_client_reused_across_calls(self, provider, mock_client):
        """Multiple fetch_messages calls reuse the same httpx client."""
        provider._client = mock_client
        data = _graphql_messages([])
        mock_client.get.return_value = _ok_response(data)

        provider.fetch_messages(
            platform_thread_id="urn:li:conv:1", cursor=None
        )
        provider.fetch_messages(
            platform_thread_id="urn:li:conv:2", cursor=None
        )

        # No new httpx.Client was created — both calls used the injected mock
        assert provider._client is mock_client

    def test_handles_non_json_response(self, provider, mock_client):
        """HTML error page (non-JSON) doesn't crash — returns empty."""
        provider._client = mock_client

        html_resp = MagicMock(spec=httpx.Response)
        html_resp.status_code = 200
        html_resp.is_redirect = False
        html_resp.headers = {}
        html_resp.text = "<html>Cloudflare error</html>"
        html_resp.json.side_effect = ValueError("No JSON")
        html_resp.raise_for_status = MagicMock()

        mock_client.get.return_value = html_resp

        msgs, cursor = provider.fetch_messages(
            platform_thread_id="urn:li:conv:1",
            cursor=None,
        )

        assert msgs == []
        assert cursor is None
