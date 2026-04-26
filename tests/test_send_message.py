"""Tests for LinkedInProvider.send_message() implementation.

Covers: happy path, headers/payload, idempotency, proxy, rate limiting,
auth errors, network retries, and the _extract_message_id helper.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import httpx
import pytest

from libs.core.models import AccountAuth, ProxyConfig
from libs.providers.linkedin.provider import (
    LinkedInProvider,
    _extract_message_id,
    _MESSAGING_URL,
)

# ---------------------------------------------------------------------------
# Sample data (matches real LinkedIn response shapes)
# ---------------------------------------------------------------------------

SAMPLE_AUTH = AccountAuth(
    li_at="AQEFARIBAAAAAAefghij-SAMPLE-TOKEN-NOT-REAL",
    jsessionid="ajax:9876543210987654321",
)

SAMPLE_AUTH_NO_JSESSIONID = AccountAuth(
    li_at="AQEFARIBAAAAAAefghij-SAMPLE-TOKEN-NOT-REAL",
    jsessionid=None,
)

SAMPLE_PROXY = ProxyConfig(url="http://user:pass@residential.proxy.io:8080")

SAMPLE_RECIPIENT = "urn:li:fsd_profile:ACoAADI4RK0BxNdiSomeProfileId"
SAMPLE_TEXT = "Hi, this is a test message from Desearch!"

SAMPLE_LINKEDIN_SUCCESS_RESPONSE = {
    "value": {
        "eventUrn": "urn:li:messagingEvent:(urn:li:conv:123456789,987654321)",
        "backendUrn": "urn:li:messagingMessage:654321",
        "conversationUrn": "urn:li:messaging_conversation:123456789",
    }
}

SAMPLE_LINKEDIN_MINIMAL_RESPONSE = {
    "value": {
        "id": "msg-id-from-linkedin-api",
    }
}

SAMPLE_LINKEDIN_EMPTY_RESPONSE = {
    "value": {}
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_provider(auth=None, proxy=None):
    return LinkedInProvider(auth=auth or SAMPLE_AUTH, proxy=proxy)


def _make_mock_response(status_code=200, json_data=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or SAMPLE_LINKEDIN_SUCCESS_RESPONSE
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# 1) Happy path — message sent, ID extracted
# ---------------------------------------------------------------------------


class TestSendMessageSuccess:
    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_returns_event_urn_from_response(self, MockClient, mock_time):
        mock_time.monotonic.return_value = 1000.0
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        client.post.return_value = _make_mock_response(200, SAMPLE_LINKEDIN_SUCCESS_RESPONSE)

        provider = _make_provider()
        result = provider.send_message(recipient=SAMPLE_RECIPIENT, text=SAMPLE_TEXT)

        assert result == "urn:li:messagingEvent:(urn:li:conv:123456789,987654321)"
        client.post.assert_called_once()

    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_returns_id_field_when_no_event_urn(self, MockClient, mock_time):
        mock_time.monotonic.return_value = 1000.0
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        client.post.return_value = _make_mock_response(200, SAMPLE_LINKEDIN_MINIMAL_RESPONSE)

        provider = _make_provider()
        result = provider.send_message(recipient=SAMPLE_RECIPIENT, text=SAMPLE_TEXT)

        assert result == "msg-id-from-linkedin-api"

    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_generates_fallback_id_when_response_empty(self, MockClient, mock_time):
        mock_time.monotonic.return_value = 1000.0
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        client.post.return_value = _make_mock_response(200, SAMPLE_LINKEDIN_EMPTY_RESPONSE)

        provider = _make_provider()
        result = provider.send_message(recipient=SAMPLE_RECIPIENT, text=SAMPLE_TEXT)

        assert result.startswith("li-send-")
        assert len(result) == len("li-send-") + 16


# ---------------------------------------------------------------------------
# 2) Correct headers, cookies, payload, and URL
# ---------------------------------------------------------------------------


class TestSendMessageRequestShape:
    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_posts_to_correct_url_with_headers_and_cookies(self, MockClient, mock_time):
        mock_time.monotonic.return_value = 1000.0
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        client.post.return_value = _make_mock_response(200)

        provider = _make_provider()
        provider.send_message(recipient=SAMPLE_RECIPIENT, text=SAMPLE_TEXT)

        args, kwargs = client.post.call_args
        assert args[0] == _MESSAGING_URL

        headers = kwargs["headers"]
        assert headers["csrf-token"] == "ajax:9876543210987654321"
        assert headers["Content-Type"] == "application/json"
        assert headers["x-restli-method"] == "CREATE"
        assert headers["x-restli-protocol-version"] == "2.0.0"
        assert "Chrome" in headers["User-Agent"]

        cookies = kwargs["cookies"]
        assert cookies["li_at"] == SAMPLE_AUTH.li_at
        assert cookies["JSESSIONID"] == SAMPLE_AUTH.jsessionid

    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_payload_contains_recipient_and_text(self, MockClient, mock_time):
        mock_time.monotonic.return_value = 1000.0
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        client.post.return_value = _make_mock_response(200)

        provider = _make_provider()
        provider.send_message(recipient=SAMPLE_RECIPIENT, text="Hello LinkedIn!")

        payload = client.post.call_args.kwargs["json"]
        assert payload["keyVersion"] == "LEGACY_INBOX"
        conv = payload["conversationCreate"]
        assert conv["recipients"] == [SAMPLE_RECIPIENT]
        assert conv["subtype"] == "MEMBER_TO_MEMBER"
        msg_create = conv["eventCreate"]["value"][
            "com.linkedin.voyager.messaging.create.MessageCreate"
        ]
        assert msg_create["attributedBody"]["text"] == "Hello LinkedIn!"
        assert msg_create["attachments"] == []

    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_omits_jsessionid_cookie_when_none(self, MockClient, mock_time):
        mock_time.monotonic.return_value = 1000.0
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        client.post.return_value = _make_mock_response(200)

        provider = _make_provider(auth=SAMPLE_AUTH_NO_JSESSIONID)
        provider.send_message(recipient=SAMPLE_RECIPIENT, text="Hi")

        cookies = client.post.call_args.kwargs["cookies"]
        assert "JSESSIONID" not in cookies
        headers = client.post.call_args.kwargs["headers"]
        assert headers["csrf-token"] == ""


# ---------------------------------------------------------------------------
# 3) Proxy support
# ---------------------------------------------------------------------------


class TestSendMessageProxy:
    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_passes_proxy_url_to_httpx_client(self, MockClient, mock_time):
        mock_time.monotonic.return_value = 1000.0
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        client.post.return_value = _make_mock_response(200)

        provider = _make_provider(proxy=SAMPLE_PROXY)
        provider.send_message(recipient=SAMPLE_RECIPIENT, text="Hi")

        MockClient.assert_called_once_with(
            proxy="http://user:pass@residential.proxy.io:8080", timeout=30.0
        )

    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_proxy_is_none_when_not_configured(self, MockClient, mock_time):
        mock_time.monotonic.return_value = 1000.0
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        client.post.return_value = _make_mock_response(200)

        provider = _make_provider(proxy=None)
        provider.send_message(recipient=SAMPLE_RECIPIENT, text="Hi")

        MockClient.assert_called_once_with(proxy=None, timeout=30.0)


# ---------------------------------------------------------------------------
# 4) Idempotency — duplicate key returns cached ID, no second HTTP call
# ---------------------------------------------------------------------------


class TestSendMessageIdempotency:
    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_same_key_returns_cached_id_without_http_call(self, MockClient, mock_time):
        mock_time.monotonic.return_value = 1000.0
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        client.post.return_value = _make_mock_response(200, SAMPLE_LINKEDIN_SUCCESS_RESPONSE)

        provider = _make_provider()
        id1 = provider.send_message(
            recipient=SAMPLE_RECIPIENT, text="Hi", idempotency_key="dedup-key-001"
        )
        id2 = provider.send_message(
            recipient=SAMPLE_RECIPIENT, text="Hi", idempotency_key="dedup-key-001"
        )

        assert id1 == id2
        assert client.post.call_count == 1

    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_different_keys_make_separate_http_calls(self, MockClient, mock_time):
        mock_time.monotonic.return_value = 1000.0
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        client.post.return_value = _make_mock_response(200, SAMPLE_LINKEDIN_SUCCESS_RESPONSE)

        provider = _make_provider()
        provider.send_message(
            recipient=SAMPLE_RECIPIENT, text="Hi", idempotency_key="key-A"
        )
        provider.send_message(
            recipient=SAMPLE_RECIPIENT, text="Hi", idempotency_key="key-B"
        )

        assert client.post.call_count == 2

    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_none_key_always_sends(self, MockClient, mock_time):
        mock_time.monotonic.return_value = 1000.0
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        client.post.return_value = _make_mock_response(200, SAMPLE_LINKEDIN_SUCCESS_RESPONSE)

        provider = _make_provider()
        provider.send_message(recipient=SAMPLE_RECIPIENT, text="Hi", idempotency_key=None)
        provider.send_message(recipient=SAMPLE_RECIPIENT, text="Hi", idempotency_key=None)

        assert client.post.call_count == 2


# ---------------------------------------------------------------------------
# 5) Auth errors — 401 and 403 raise immediately, no retry
# ---------------------------------------------------------------------------


class TestSendMessageAuthErrors:
    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_401_raises_permission_error(self, MockClient, mock_time):
        mock_time.monotonic.return_value = 1000.0
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        client.post.return_value = _make_mock_response(401)

        provider = _make_provider()
        with pytest.raises(PermissionError, match="401"):
            provider.send_message(recipient=SAMPLE_RECIPIENT, text="Hi")

        assert client.post.call_count == 1

    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_403_raises_permission_error(self, MockClient, mock_time):
        mock_time.monotonic.return_value = 1000.0
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        client.post.return_value = _make_mock_response(403)

        provider = _make_provider()
        with pytest.raises(PermissionError, match="403"):
            provider.send_message(recipient=SAMPLE_RECIPIENT, text="Hi")

        assert client.post.call_count == 1


# ---------------------------------------------------------------------------
# 6) Rate limiting — 429 / 999 trigger backoff and retry
# ---------------------------------------------------------------------------


class TestSendMessageRateLimiting:
    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_429_retries_then_succeeds(self, MockClient, mock_time):
        mock_time.monotonic.return_value = 1000.0
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        rate_limit_resp = _make_mock_response(429)
        success_resp = _make_mock_response(200, SAMPLE_LINKEDIN_SUCCESS_RESPONSE)
        client.post.side_effect = [rate_limit_resp, success_resp]

        provider = _make_provider()
        result = provider.send_message(recipient=SAMPLE_RECIPIENT, text="Hi")

        assert result == "urn:li:messagingEvent:(urn:li:conv:123456789,987654321)"
        assert client.post.call_count == 2
        mock_time.sleep.assert_any_call(30.0)

    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_999_retries_then_succeeds(self, MockClient, mock_time):
        mock_time.monotonic.return_value = 1000.0
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        rate_limit_resp = _make_mock_response(999)
        success_resp = _make_mock_response(200, SAMPLE_LINKEDIN_SUCCESS_RESPONSE)
        client.post.side_effect = [rate_limit_resp, success_resp]

        provider = _make_provider()
        result = provider.send_message(recipient=SAMPLE_RECIPIENT, text="Hi")

        assert result == "urn:li:messagingEvent:(urn:li:conv:123456789,987654321)"
        assert client.post.call_count == 2

    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_backoff_is_exponential(self, MockClient, mock_time):
        mock_time.monotonic.return_value = 1000.0
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        r429 = _make_mock_response(429)
        ok = _make_mock_response(200, SAMPLE_LINKEDIN_SUCCESS_RESPONSE)
        client.post.side_effect = [r429, r429, r429, ok]

        provider = _make_provider()
        provider.send_message(recipient=SAMPLE_RECIPIENT, text="Hi")

        sleep_calls = [c.args[0] for c in mock_time.sleep.call_args_list]
        assert sleep_calls == [30.0, 60.0, 120.0]

    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_gives_up_after_max_rate_limit_retries(self, MockClient, mock_time):
        mock_time.monotonic.return_value = 1000.0
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        client.post.return_value = _make_mock_response(429)

        provider = _make_provider()
        with pytest.raises(httpx.HTTPStatusError):
            provider.send_message(recipient=SAMPLE_RECIPIENT, text="Hi")

        assert client.post.call_count == 6  # 5 retries + 1 final that triggers raise


# ---------------------------------------------------------------------------
# 7) Network errors — retries up to 3 times, then ConnectionError
# ---------------------------------------------------------------------------


class TestSendMessageNetworkRetries:
    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_network_error_retries_then_succeeds(self, MockClient, mock_time):
        mock_time.monotonic.return_value = 1000.0
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        client.post.side_effect = [
            httpx.ConnectError("connection refused"),
            _make_mock_response(200, SAMPLE_LINKEDIN_SUCCESS_RESPONSE),
        ]

        provider = _make_provider()
        result = provider.send_message(recipient=SAMPLE_RECIPIENT, text="Hi")

        assert result == "urn:li:messagingEvent:(urn:li:conv:123456789,987654321)"
        assert client.post.call_count == 2
        mock_time.sleep.assert_called_with(5.0)

    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_timeout_retries_then_succeeds(self, MockClient, mock_time):
        mock_time.monotonic.return_value = 1000.0
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        client.post.side_effect = [
            httpx.ReadTimeout("timed out"),
            _make_mock_response(200, SAMPLE_LINKEDIN_SUCCESS_RESPONSE),
        ]

        provider = _make_provider()
        result = provider.send_message(recipient=SAMPLE_RECIPIENT, text="Hi")

        assert result == "urn:li:messagingEvent:(urn:li:conv:123456789,987654321)"

    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_raises_connection_error_after_3_failures(self, MockClient, mock_time):
        mock_time.monotonic.return_value = 1000.0
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        client.post.side_effect = httpx.ConnectError("refused")

        provider = _make_provider()
        with pytest.raises(ConnectionError, match="3 network retries"):
            provider.send_message(recipient=SAMPLE_RECIPIENT, text="Hi")

        assert client.post.call_count == 3


# ---------------------------------------------------------------------------
# 8) Rate-limit interval enforced between sends
# ---------------------------------------------------------------------------


class TestSendInterval:
    @patch("libs.providers.linkedin.provider.time")
    @patch("libs.providers.linkedin.provider.httpx.Client")
    def test_sleeps_when_sends_are_too_fast(self, MockClient, mock_time):
        mock_time.monotonic.side_effect = [100.0, 100.5]
        client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        client.post.return_value = _make_mock_response(200)

        provider = _make_provider()
        provider._last_send_ts = 100.0
        provider.send_message(recipient=SAMPLE_RECIPIENT, text="Hi")

        mock_time.sleep.assert_any_call(pytest.approx(2.0, abs=0.01))


# ---------------------------------------------------------------------------
# 9) _extract_message_id helper — unit tests with sample data
# ---------------------------------------------------------------------------


class TestExtractMessageId:
    def test_extracts_event_urn(self):
        data = {"value": {"eventUrn": "urn:li:messagingEvent:123"}}
        assert _extract_message_id(data) == "urn:li:messagingEvent:123"

    def test_extracts_backend_urn(self):
        data = {"value": {"backendUrn": "urn:li:messagingMessage:456"}}
        assert _extract_message_id(data) == "urn:li:messagingMessage:456"

    def test_extracts_conversation_urn(self):
        data = {"value": {"conversationUrn": "urn:li:conv:789"}}
        assert _extract_message_id(data) == "urn:li:conv:789"

    def test_extracts_id_field(self):
        data = {"value": {"id": "simple-id"}}
        assert _extract_message_id(data) == "simple-id"

    def test_extracts_entity_urn(self):
        data = {"value": {"entityUrn": "urn:li:entity:321"}}
        assert _extract_message_id(data) == "urn:li:entity:321"

    def test_prefers_event_urn_over_others(self):
        data = {"value": {"eventUrn": "ev-1", "id": "id-1", "entityUrn": "ent-1"}}
        assert _extract_message_id(data) == "ev-1"

    def test_falls_back_to_generated_id_when_empty(self):
        data = {"value": {}}
        result = _extract_message_id(data)
        assert result.startswith("li-send-")

    def test_falls_back_when_no_value_key(self):
        data = {"something_else": True}
        result = _extract_message_id(data)
        assert result.startswith("li-send-")

    def test_full_linkedin_response(self):
        result = _extract_message_id(SAMPLE_LINKEDIN_SUCCESS_RESPONSE)
        assert result == "urn:li:messagingEvent:(urn:li:conv:123456789,987654321)"
