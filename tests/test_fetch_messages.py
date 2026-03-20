"""Tests for LinkedInProvider.fetch_messages() — GraphQL messaging API."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from libs.core.models import AccountAuth, ProxyConfig
from libs.providers.linkedin.provider import (
    LinkedInMessage,
    LinkedInProvider,
    _parse_graphql_messages,
    _GRAPHQL_BASE,
    _MESSAGES_QUERY_ID,
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

def _graphql_messages_response(elements: list[dict]) -> dict:
    """Build a realistic GraphQL messengerMessages response."""
    return {
        "data": {
            "messengerMessagesBySyncToken": {
                "elements": elements,
            },
        },
    }


def _make_message_event(
    urn: str,
    *,
    text: str = "Hello",
    created_at: int = 1700000000000,
    sender_urn: str | None = "urn:li:fsd_profile:SENDER",
    sender_first: str = "Alice",
    sender_last: str = "Smith",
) -> dict:
    """Build a message event element for GraphQL responses."""
    event: dict = {
        "entityUrn": urn,
        "createdAt": created_at,
        "eventContent": {
            "attributedBody": {"text": text},
        },
    }
    if sender_urn:
        event["sender"] = {
            "participantProfile": {
                "entityUrn": sender_urn,
                "firstName": sender_first,
                "lastName": sender_last,
            },
        }
    return event


def _mock_resp(data: dict, status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.content = b"ok"
    r.raise_for_status = MagicMock()
    r.json.return_value = data
    return r


def _patch_client(mock_client: MagicMock):
    return patch("libs.providers.linkedin.provider.httpx.Client", return_value=mock_client)


# ---------------------------------------------------------------------------
# Unit tests: _parse_graphql_messages
# ---------------------------------------------------------------------------

class TestParseGraphqlMessages:
    def test_parses_single_message(self):
        events = [_make_message_event("urn:msg:1", text="Hi")]
        msgs = _parse_graphql_messages(events, None)
        assert len(msgs) == 1
        assert msgs[0].platform_message_id == "urn:msg:1"
        assert msgs[0].text == "Hi"

    def test_direction_out_when_sender_matches_profile(self):
        events = [_make_message_event("urn:msg:1", sender_urn="urn:li:fsd_profile:ABC123")]
        msgs = _parse_graphql_messages(events, "urn:li:fsd_profile:ABC123")
        assert msgs[0].direction == "out"

    def test_direction_in_when_sender_different(self):
        events = [_make_message_event("urn:msg:1", sender_urn="urn:li:fsd_profile:OTHER")]
        msgs = _parse_graphql_messages(events, "urn:li:fsd_profile:ABC123")
        assert msgs[0].direction == "in"

    def test_direction_in_when_sender_is_substring_of_profile(self):
        """Regression: 'john' in 'johnny' must NOT match as 'out'."""
        events = [_make_message_event("urn:msg:1", sender_urn="urn:li:fsd_profile:johnny")]
        msgs = _parse_graphql_messages(events, "john")
        assert msgs[0].direction == "in"

    def test_direction_out_when_profile_is_id_suffix(self):
        """Profile ID 'ABC123' should match sender 'urn:li:fsd_profile:ABC123' via endswith."""
        events = [_make_message_event("urn:msg:1", sender_urn="urn:li:fsd_profile:ABC123")]
        msgs = _parse_graphql_messages(events, "ABC123")
        assert msgs[0].direction == "out"

    def test_direction_in_when_no_profile_id(self):
        events = [_make_message_event("urn:msg:1")]
        msgs = _parse_graphql_messages(events, None)
        assert msgs[0].direction == "in"

    def test_sender_name_extracted(self):
        events = [_make_message_event("urn:msg:1", sender_first="Bob", sender_last="Jones")]
        msgs = _parse_graphql_messages(events, None)
        assert msgs[0].sender == "Bob Jones"

    def test_timestamp_parsed(self):
        ts = 1700000000000  # 2023-11-14T22:13:20Z
        events = [_make_message_event("urn:msg:1", created_at=ts)]
        msgs = _parse_graphql_messages(events, None)
        assert msgs[0].sent_at == datetime.fromtimestamp(ts / 1000, tz=timezone.utc)

    def test_sorted_oldest_first(self):
        events = [
            _make_message_event("urn:msg:2", created_at=1700000002000),
            _make_message_event("urn:msg:1", created_at=1700000001000),
            _make_message_event("urn:msg:3", created_at=1700000003000),
        ]
        msgs = _parse_graphql_messages(events, None)
        assert [m.platform_message_id for m in msgs] == ["urn:msg:1", "urn:msg:2", "urn:msg:3"]

    def test_skips_non_dict_events(self):
        events = ["not-a-dict", _make_message_event("urn:msg:1")]
        msgs = _parse_graphql_messages(events, None)
        assert len(msgs) == 1

    def test_skips_events_without_id(self):
        events = [{"createdAt": 1700000000000, "eventContent": {"text": "hi"}}]
        msgs = _parse_graphql_messages(events, None)
        assert len(msgs) == 0

    def test_text_from_body_text_fallback(self):
        event = {
            "entityUrn": "urn:msg:1",
            "createdAt": 1700000000000,
            "eventContent": {"text": "fallback text"},
        }
        msgs = _parse_graphql_messages([event], None)
        assert msgs[0].text == "fallback text"

    def test_text_from_string_body(self):
        event = {
            "entityUrn": "urn:msg:1",
            "createdAt": 1700000000000,
            "body": "string body",
        }
        msgs = _parse_graphql_messages([event], None)
        assert msgs[0].text == "string body"

    def test_text_none_when_no_body(self):
        event = {
            "entityUrn": "urn:msg:1",
            "createdAt": 1700000000000,
        }
        msgs = _parse_graphql_messages([event], None)
        assert msgs[0].text is None

    def test_attributed_body_null_does_not_crash(self):
        """Regression: attributedBody can be explicitly null (not missing)."""
        event = {
            "entityUrn": "urn:msg:1",
            "createdAt": 1700000000000,
            "eventContent": {"attributedBody": None, "text": "fallback"},
        }
        msgs = _parse_graphql_messages([event], None)
        assert msgs[0].text == "fallback"

    def test_attributed_body_null_no_fallback(self):
        """attributedBody is null and no text fallback → None."""
        event = {
            "entityUrn": "urn:msg:1",
            "createdAt": 1700000000000,
            "eventContent": {"attributedBody": None},
        }
        msgs = _parse_graphql_messages([event], None)
        assert msgs[0].text is None

    def test_empty_events_list(self):
        assert _parse_graphql_messages([], None) == []

    def test_backend_urn_fallback(self):
        event = {
            "backendUrn": "urn:msg:backend:1",
            "createdAt": 1700000000000,
        }
        msgs = _parse_graphql_messages([event], None)
        assert msgs[0].platform_message_id == "urn:msg:backend:1"

    def test_dash_entity_urn_fallback(self):
        event = {
            "dashEntityUrn": "urn:msg:dash:1",
            "createdAt": 1700000000000,
        }
        msgs = _parse_graphql_messages([event], None)
        assert msgs[0].platform_message_id == "urn:msg:dash:1"

    def test_delivered_at_fallback(self):
        ts = 1700000005000
        event = {
            "entityUrn": "urn:msg:1",
            "deliveredAt": ts,
        }
        msgs = _parse_graphql_messages([event], None)
        assert msgs[0].sent_at == datetime.fromtimestamp(ts / 1000, tz=timezone.utc)

    def test_raw_preserved(self):
        event = _make_message_event("urn:msg:1")
        msgs = _parse_graphql_messages([event], None)
        assert msgs[0].raw == event


# ---------------------------------------------------------------------------
# Integration: fetch_messages with mocked HTTP
# ---------------------------------------------------------------------------

class TestFetchMessages:
    def test_basic_fetch(self, provider):
        events = [_make_message_event("urn:msg:1", text="Hello")]
        data = _graphql_messages_response(events)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            msgs, cursor = provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
                limit=50,
            )
        assert len(msgs) == 1
        assert msgs[0].text == "Hello"
        assert cursor is None  # 1 message < limit of 50

    def test_empty_response(self, provider):
        data = _graphql_messages_response([])
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            msgs, cursor = provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
            )
        assert msgs == []
        assert cursor is None

    def test_returns_next_cursor_when_full_page(self, provider):
        """When count matches limit, return oldest message's createdAt as cursor."""
        events = [
            _make_message_event(f"urn:msg:{i}", created_at=1700000000000 + i * 1000)
            for i in range(3)
        ]
        data = _graphql_messages_response(events)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            msgs, cursor = provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
                limit=3,  # matches len(events)
            )
        assert len(msgs) == 3
        # Oldest message (sorted first) createdAt as cursor
        assert cursor == "1700000000000"

    def test_no_cursor_when_fewer_than_limit(self, provider):
        events = [_make_message_event("urn:msg:1")]
        data = _graphql_messages_response(events)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            msgs, cursor = provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
                limit=50,
            )
        assert cursor is None

    def test_cursor_passed_in_url(self, provider):
        data = _graphql_messages_response([])
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor="1700000000000",
                limit=50,
            )
        url = mock_client.get.call_args[0][0]
        assert "createdBefore:1700000000000" in url

    def test_conversation_urn_in_url(self, provider):
        data = _graphql_messages_response([])
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            provider.fetch_messages(
                platform_thread_id="urn:li:msg_conversation:ABC",
                cursor=None,
            )
        url = mock_client.get.call_args[0][0]
        assert "conversationUrn:urn:li:msg_conversation:ABC" in url
        assert _MESSAGES_QUERY_ID in url

    def test_limit_in_url(self, provider):
        data = _graphql_messages_response([])
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
                limit=25,
            )
        url = mock_client.get.call_args[0][0]
        assert "count:25" in url

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

    def test_direction_detection(self, provider):
        """Messages from self are 'out', from others are 'in'."""
        events = [
            _make_message_event("urn:msg:1", sender_urn="urn:li:fsd_profile:ABC123"),
            _make_message_event("urn:msg:2", sender_urn="urn:li:fsd_profile:OTHER"),
        ]
        data = _graphql_messages_response(events)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            msgs, _ = provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
            )
        directions = {m.platform_message_id: m.direction for m in msgs}
        assert directions["urn:msg:1"] == "out"
        assert directions["urn:msg:2"] == "in"

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
                provider.fetch_messages(
                    platform_thread_id="urn:li:conv:1",
                    cursor=None,
                )

    def test_retries_on_429(self, provider):
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {}
        resp_429.request = MagicMock()
        data = _graphql_messages_response([_make_message_event("urn:msg:1")])
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.side_effect = [resp_429, _mock_resp(data)]
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep"):
            msgs, _ = provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
            )
        assert len(msgs) == 1

    def test_requires_jsessionid(self):
        auth = AccountAuth(li_at="li", jsessionid=None)
        p = LinkedInProvider(auth=auth)
        with pytest.raises(ValueError, match="JSESSIONID"):
            p.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
            )

    def test_uses_browser_cookies(self, provider):
        """Verify browser cookies are passed to httpx."""
        data = _graphql_messages_response([])
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
            )
        call_kwargs = mock_client.get.call_args.kwargs
        assert "__cf_bm" in call_kwargs["cookies"]

    def test_handles_non_dict_response(self, provider):
        r = MagicMock()
        r.status_code = 200
        r.content = b"[]"
        r.raise_for_status = MagicMock()
        r.json.return_value = []
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = r
        with _patch_client(mock_client):
            msgs, cursor = provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
            )
        assert msgs == []
        assert cursor is None

    def test_handles_empty_response_body(self, provider):
        r = MagicMock()
        r.status_code = 200
        r.content = b""
        r.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = r
        with _patch_client(mock_client):
            msgs, cursor = provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
            )
        assert msgs == []
        assert cursor is None

    def test_messages_sorted_chronologically(self, provider):
        events = [
            _make_message_event("urn:msg:new", created_at=1700000002000),
            _make_message_event("urn:msg:old", created_at=1700000001000),
        ]
        data = _graphql_messages_response(events)
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            msgs, _ = provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
            )
        assert msgs[0].platform_message_id == "urn:msg:old"
        assert msgs[1].platform_message_id == "urn:msg:new"

    def test_uses_proxy(self):
        auth = AccountAuth(li_at="test-li-at", jsessionid="ajax:csrf123")
        proxy = ProxyConfig(url="http://proxy:8080")
        p = LinkedInProvider(auth=auth, proxy=proxy)
        p._browser_cookies = {"li_at": "x"}
        p._profile_id = "urn:li:fsd_profile:ABC"
        p._profile_id_fetched = True
        data = _graphql_messages_response([])
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client) as mock_cls:
            p.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
            )
        mock_cls.assert_called_once()
        assert mock_cls.call_args.kwargs.get("proxy") == "http://proxy:8080"

    def test_deduplicates_messages_in_page(self, provider):
        """Duplicate message IDs in same response → returned only once."""
        dup = _make_message_event("urn:msg:dup", text="first", created_at=1700000001000)
        dup2 = _make_message_event("urn:msg:dup", text="second", created_at=1700000001000)
        unique = _make_message_event("urn:msg:unique", created_at=1700000002000)
        data = _graphql_messages_response([dup, dup2, unique])
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            msgs, _ = provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
            )
        ids = [m.platform_message_id for m in msgs]
        assert ids.count("urn:msg:dup") == 1
        assert "urn:msg:unique" in ids
        assert len(msgs) == 2

    def test_cursor_set_when_dedup_reduces_below_limit(self, provider):
        """Regression: cursor must use pre-dedup element count, not post-dedup.

        If server returns limit=2 elements but dedup reduces to 1 message,
        next_cursor must still be set because the server page was full.
        """
        dup1 = _make_message_event("urn:msg:dup", text="first", created_at=1700000001000)
        dup2 = _make_message_event("urn:msg:dup", text="second", created_at=1700000002000)
        data = _graphql_messages_response([dup1, dup2])
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            msgs, cursor = provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
                limit=2,
            )
        assert len(msgs) == 1  # dedup reduced to 1
        assert cursor is not None  # but cursor must still be set (server had full page)

    def test_handles_data_field_as_list(self, provider):
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
            msgs, cursor = provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
            )
        assert msgs == []
        assert cursor is None

    def test_handles_html_error_page(self, provider):
        """HTML response (e.g. login redirect) doesn't crash."""
        r = MagicMock()
        r.status_code = 200
        r.content = b"<html>Please log in</html>"
        r.raise_for_status = MagicMock()
        r.json.side_effect = ValueError("No JSON")
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = r
        with _patch_client(mock_client):
            msgs, cursor = provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
            )
        assert msgs == []
        assert cursor is None

    def test_no_cursor_when_oldest_lacks_created_at(self, provider):
        """Even at limit, no cursor if oldest message lacks createdAt in raw."""
        event = {"entityUrn": "urn:msg:1"}  # no createdAt
        data = _graphql_messages_response([event])
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            msgs, cursor = provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
                limit=1,
            )
        assert len(msgs) == 1
        assert cursor is None

    def test_exhausts_retries_on_500(self, provider):
        resp_500 = MagicMock()
        resp_500.status_code = 500
        resp_500.headers = {}
        resp_500.request = MagicMock()
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = resp_500
        with _patch_client(mock_client), \
             patch("libs.providers.linkedin.provider.time.sleep"):
            with pytest.raises(httpx.HTTPStatusError):
                provider.fetch_messages(
                    platform_thread_id="urn:li:conv:1",
                    cursor=None,
                )

    def test_sender_without_profile_info(self, provider):
        """Message with sender but no profile → sender is None, direction is 'in'."""
        event = {
            "entityUrn": "urn:msg:1",
            "createdAt": 1700000000000,
            "sender": {},
            "eventContent": {"text": "hello"},
        }
        data = _graphql_messages_response([event])
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            msgs, _ = provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
            )
        assert msgs[0].sender is None
        assert msgs[0].direction == "in"

    def test_limit_boundary_1(self, provider):
        """Limit=1 is valid."""
        data = _graphql_messages_response([_make_message_event("urn:msg:1")])
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            msgs, _ = provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
                limit=1,
            )
        assert len(msgs) == 1

    def test_limit_boundary_200(self, provider):
        """Limit=200 is valid."""
        data = _graphql_messages_response([])
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.return_value = _mock_resp(data)
        with _patch_client(mock_client):
            msgs, _ = provider.fetch_messages(
                platform_thread_id="urn:li:conv:1",
                cursor=None,
                limit=200,
            )
        url = mock_client.get.call_args[0][0]
        assert "count:200" in url


# ---------------------------------------------------------------------------
# Integration: list_threads → fetch_messages pipeline
# ---------------------------------------------------------------------------

class TestSyncPipeline:
    """End-to-end integration test mocking the full sync pipeline."""

    def test_list_then_fetch_pipeline(self):
        """list_threads → fetch_messages for each thread → messages collected."""
        auth = AccountAuth(li_at="li", jsessionid="csrf")
        p = LinkedInProvider(auth=auth)
        p._browser_cookies = {"li_at": "li", "__cf_bm": "cf"}
        p._profile_id = "urn:li:fsd_profile:ME"
        p._profile_id_fetched = True

        # Build responses
        conv_data = {
            "data": {
                "messengerConversationsBySyncToken": {
                    "elements": [
                        {"entityUrn": "urn:conv:1", "conversationName": "Alice"},
                        {"entityUrn": "urn:conv:2", "conversationName": "Bob"},
                    ],
                    "metadata": {},
                },
            },
        }
        msg_data_1 = {
            "data": {
                "messengerMessagesBySyncToken": {
                    "elements": [
                        _make_message_event("urn:msg:1a", text="Hi Alice", created_at=1700000001000),
                    ],
                },
            },
        }
        msg_data_2 = {
            "data": {
                "messengerMessagesBySyncToken": {
                    "elements": [
                        _make_message_event("urn:msg:2a", text="Hi Bob", created_at=1700000002000),
                    ],
                },
            },
        }

        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.get.side_effect = [
            _mock_resp(conv_data),   # list_threads
            _mock_resp(msg_data_1),  # fetch_messages for conv:1
            _mock_resp(msg_data_2),  # fetch_messages for conv:2
        ]

        with _patch_client(mock_client):
            threads = p.list_threads()
            all_msgs = []
            for t in threads:
                msgs, _ = p.fetch_messages(
                    platform_thread_id=t.platform_thread_id,
                    cursor=None,
                )
                all_msgs.extend(msgs)

        assert len(threads) == 2
        assert threads[0].title == "Alice"
        assert threads[1].title == "Bob"
        assert len(all_msgs) == 2
        assert all_msgs[0].text == "Hi Alice"
        assert all_msgs[1].text == "Hi Bob"
        assert mock_client.get.call_count == 3
