"""Tests for LinkedInProvider retry, backoff, and rate-limit behavior."""
from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import httpx
import pytest

from libs.core.models import AccountAuth
from libs.providers.linkedin.provider import (
    LinkedInProvider,
    _BACKOFF_MAX_S,
    _BACKOFF_START_S,
    _MAX_NETWORK_RETRIES,
    _NETWORK_RETRY_DELAY_S,
    _RATE_LIMIT_MAX_ATTEMPTS,
    _RETRY_BASE_DELAY_S,
    _RETRY_MAX_ATTEMPTS,
)


@pytest.fixture
def auth():
    return AccountAuth(li_at="test-li-at", jsessionid="ajax:csrf123")


@pytest.fixture
def provider(auth):
    return LinkedInProvider(auth=auth, proxy=None, account_id=42)


def _mock_response(status_code: int, headers: dict | None = None) -> httpx.Response:
    """Build a minimal httpx.Response with the given status code."""
    request = httpx.Request("GET", "https://example.com/test")
    return httpx.Response(
        status_code=status_code,
        request=request,
        headers=headers or {},
    )


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


def test_get_with_retry_returns_immediately_on_200(provider):
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = _mock_response(200)

    resp = provider._get_with_retry(client, "https://example.com")
    assert resp.status_code == 200
    client.get.assert_called_once()


# ---------------------------------------------------------------------------
# HTTP 401 — fail fast
# ---------------------------------------------------------------------------


def test_get_with_retry_raises_permission_error_on_401(provider):
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = _mock_response(401)

    with pytest.raises(PermissionError, match="HTTP 401"):
        provider._get_with_retry(client, "https://example.com")

    client.get.assert_called_once()


# ---------------------------------------------------------------------------
# Rate-limit (429/999) — exponential backoff
# ---------------------------------------------------------------------------


@patch("libs.providers.linkedin.provider.time.sleep")
def test_get_with_retry_backoff_on_429(mock_sleep, provider):
    """429 triggers exponential backoff starting at _BACKOFF_START_S."""
    resp_429 = _mock_response(429)
    resp_200 = _mock_response(200)
    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = [resp_429, resp_429, resp_200]

    resp = provider._get_with_retry(client, "https://example.com")

    assert resp.status_code == 200
    assert client.get.call_count == 3
    assert provider.rate_limit_encountered is True
    assert mock_sleep.call_args_list == [
        call(_BACKOFF_START_S),          # 30s (attempt 1)
        call(_BACKOFF_START_S * 2),      # 60s (attempt 2)
    ]


@patch("libs.providers.linkedin.provider.time.sleep")
def test_get_with_retry_999_treated_as_rate_limit(mock_sleep, provider):
    """HTTP 999 (LinkedIn-specific) is treated as a rate limit, same as 429."""
    resp_999 = _mock_response(999)
    resp_200 = _mock_response(200)
    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = [resp_999, resp_200]

    resp = provider._get_with_retry(client, "https://example.com")

    assert resp.status_code == 200
    assert provider.rate_limit_encountered is True
    mock_sleep.assert_called_once_with(_BACKOFF_START_S)


@patch("libs.providers.linkedin.provider.time.sleep")
def test_get_with_retry_rate_limit_respects_retry_after(mock_sleep, provider):
    resp_429 = _mock_response(429, headers={"Retry-After": "120"})
    resp_200 = _mock_response(200)
    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = [resp_429, resp_200]

    provider._get_with_retry(client, "https://example.com")

    mock_sleep.assert_called_once_with(120.0)


@patch("libs.providers.linkedin.provider.time.sleep")
def test_get_with_retry_rate_limit_exhausts_attempts(mock_sleep, provider):
    """After _RATE_LIMIT_MAX_ATTEMPTS, raises HTTPStatusError."""
    resp_429 = _mock_response(429)
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = resp_429

    with pytest.raises(httpx.HTTPStatusError):
        provider._get_with_retry(client, "https://example.com")

    assert client.get.call_count == _RATE_LIMIT_MAX_ATTEMPTS
    assert mock_sleep.call_count == _RATE_LIMIT_MAX_ATTEMPTS - 1
    assert provider.rate_limit_encountered is True


@patch("libs.providers.linkedin.provider.time.sleep")
def test_get_with_retry_backoff_caps_at_max(mock_sleep, provider):
    """Backoff delay is capped at _BACKOFF_MAX_S (15 min)."""
    resp_429 = _mock_response(429)
    resp_200 = _mock_response(200)
    # 5 rate-limit responses then success (attempt 6)
    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = [resp_429] * 5 + [resp_200]

    provider._get_with_retry(client, "https://example.com")

    delays = [c.args[0] for c in mock_sleep.call_args_list]
    for d in delays:
        assert d <= _BACKOFF_MAX_S
    # The 5th delay: 30 * 2^4 = 480 < 900, still under cap
    assert delays[4] == min(_BACKOFF_START_S * (2 ** 4), _BACKOFF_MAX_S)


# ---------------------------------------------------------------------------
# Server errors (500, 502, etc.) — short retry
# ---------------------------------------------------------------------------


@patch("libs.providers.linkedin.provider.time.sleep")
def test_get_with_retry_retries_server_error(mock_sleep, provider):
    resp_500 = _mock_response(500)
    resp_200 = _mock_response(200)
    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = [resp_500, resp_200]

    resp = provider._get_with_retry(client, "https://example.com")

    assert resp.status_code == 200
    assert client.get.call_count == 2
    mock_sleep.assert_called_once_with(_RETRY_BASE_DELAY_S)


@patch("libs.providers.linkedin.provider.time.sleep")
def test_get_with_retry_server_error_exhausts_attempts(mock_sleep, provider):
    resp_502 = _mock_response(502)
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = resp_502

    with pytest.raises(httpx.HTTPStatusError):
        provider._get_with_retry(client, "https://example.com")

    assert client.get.call_count == _RETRY_MAX_ATTEMPTS
    assert mock_sleep.call_count == _RETRY_MAX_ATTEMPTS - 1


@patch("libs.providers.linkedin.provider.time.sleep")
def test_get_with_retry_server_and_rate_limit_have_separate_budgets(mock_sleep, provider):
    """Mixed errors use independent retry counters."""
    resp_500 = _mock_response(500)
    resp_429 = _mock_response(429)
    resp_200 = _mock_response(200)
    client = MagicMock(spec=httpx.Client)
    # 2 server errors (under limit of 3) then 1 rate limit then success
    client.get.side_effect = [resp_500, resp_500, resp_429, resp_200]

    resp = provider._get_with_retry(client, "https://example.com")

    assert resp.status_code == 200
    assert client.get.call_count == 4
    assert provider.rate_limit_encountered is True


# ---------------------------------------------------------------------------
# Network errors — retry with fixed delay
# ---------------------------------------------------------------------------


@patch("libs.providers.linkedin.provider.time.sleep")
def test_get_with_retry_retries_on_network_error(mock_sleep, provider):
    resp_200 = _mock_response(200)
    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = [httpx.ConnectError("fail"), resp_200]

    resp = provider._get_with_retry(client, "https://example.com")

    assert resp.status_code == 200
    assert client.get.call_count == 2
    mock_sleep.assert_called_once_with(_NETWORK_RETRY_DELAY_S)


@patch("libs.providers.linkedin.provider.time.sleep")
def test_get_with_retry_retries_on_timeout(mock_sleep, provider):
    resp_200 = _mock_response(200)
    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = [httpx.ReadTimeout("timeout"), resp_200]

    resp = provider._get_with_retry(client, "https://example.com")

    assert resp.status_code == 200
    mock_sleep.assert_called_once_with(_NETWORK_RETRY_DELAY_S)


@patch("libs.providers.linkedin.provider.time.sleep")
def test_get_with_retry_raises_connection_error_after_max_network_retries(
    mock_sleep, provider
):
    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = httpx.ConnectError("down")

    with pytest.raises(ConnectionError, match="network retries"):
        provider._get_with_retry(client, "https://example.com")

    assert client.get.call_count == _MAX_NETWORK_RETRIES
    assert mock_sleep.call_count == _MAX_NETWORK_RETRIES - 1


# ---------------------------------------------------------------------------
# account_id in provider
# ---------------------------------------------------------------------------


def test_provider_stores_account_id():
    auth = AccountAuth(li_at="x", jsessionid="y")
    p = LinkedInProvider(auth=auth, proxy=None, account_id=7)
    assert p._account_id == 7


def test_provider_account_id_defaults_to_none():
    auth = AccountAuth(li_at="x", jsessionid="y")
    p = LinkedInProvider(auth=auth, proxy=None)
    assert p._account_id is None
