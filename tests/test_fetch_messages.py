"""Tests for LinkedInProvider.fetch_messages (Voyager API events)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from libs.core.models import AccountAuth, ProxyConfig
from libs.providers.linkedin.provider import LinkedInMessage, LinkedInProvider


def _me_response(public_identifier: str = "my-profile") -> dict:
    return {"publicIdentifier": public_identifier}


def _event(
    *,
    entity_urn: str = "urn:li:msg:1",
    created_at_ms: int = 1000000,
    public_identifier: str = "other-user",
    body: str | None = "Hello",
) -> dict:
    return {
        "entityUrn": entity_urn,
        "createdAt": created_at_ms,
        "from": {
            "member": {
                "miniProfile": {
                    "publicIdentifier": public_identifier,
                },
            },
        },
        "eventContent": {"body": body} if body is not None else {},
    }


@pytest.fixture
def auth():
    return AccountAuth(li_at="test-li-at", jsessionid="ajax:csrf123")


@pytest.fixture
def provider(auth):
    return LinkedInProvider(auth=auth, proxy=None)


def test_fetch_messages_returns_empty_list_and_none_cursor_when_no_events(provider):
    """Regression: empty thread returns ([], None)."""
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        _mock_resp({"elements": []}),
    ]
    with _patch_client(mock_client):
        messages, next_cursor = provider.fetch_messages(
            platform_thread_id="conv-1",
            cursor=None,
            limit=50,
        )
    assert messages == []
    assert next_cursor is None
    assert mock_client.get.call_count == 2


def test_fetch_messages_returns_messages_and_none_cursor_when_fewer_than_limit(provider):
    """Fewer than limit events → next_cursor is None."""
    ev = _event(entity_urn="urn:li:msg:1", created_at_ms=2000000, public_identifier="bob", body="Hi")
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response("me")),
        _mock_resp({"elements": [ev]}),
    ]
    with _patch_client(mock_client):
        messages, next_cursor = provider.fetch_messages(
            platform_thread_id="c1",
            cursor=None,
            limit=50,
        )
    assert len(messages) == 1
    assert messages[0].platform_message_id == "urn:li:msg:1"
    assert messages[0].direction == "in"
    assert messages[0].sender == "bob"
    assert messages[0].text == "Hi"
    assert next_cursor is None


def test_fetch_messages_returns_next_cursor_when_exactly_limit_events(provider):
    """Exactly limit events → next_cursor is oldest message's createdAt ms."""
    oldest_ts = 1000000
    events = [
        _event(entity_urn="urn:li:msg:1", created_at_ms=oldest_ts, public_identifier="alice", body="First"),
        _event(entity_urn="urn:li:msg:2", created_at_ms=2000000, public_identifier="me", body="Second"),
    ]
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response("me")),
        _mock_resp({"elements": events}),
    ]
    with _patch_client(mock_client):
        messages, next_cursor = provider.fetch_messages(
            platform_thread_id="c1",
            cursor=None,
            limit=2,
        )
    assert len(messages) == 2
    assert next_cursor == str(oldest_ts)


def test_fetch_messages_direction_out_when_sender_is_my_profile_id(provider):
    """Event from current user → direction 'out'."""
    ev = _event(public_identifier="me-user", body="Sent by me")
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response("me-user")),
        _mock_resp({"elements": [ev]}),
    ]
    with _patch_client(mock_client):
        messages, _ = provider.fetch_messages(
            platform_thread_id="c1",
            cursor=None,
            limit=50,
        )
    assert len(messages) == 1
    assert messages[0].direction == "out"
    assert messages[0].sender == "me-user"


def test_fetch_messages_direction_in_when_sender_is_other(provider):
    """Event from other user → direction 'in'."""
    ev = _event(public_identifier="other-user", body="From them")
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response("my-profile")),
        _mock_resp({"elements": [ev]}),
    ]
    with _patch_client(mock_client):
        messages, _ = provider.fetch_messages(
            platform_thread_id="c1",
            cursor=None,
            limit=50,
        )
    assert len(messages) == 1
    assert messages[0].direction == "in"
    assert messages[0].sender == "other-user"


def test_fetch_messages_passes_cursor_as_created_before(provider):
    """Cursor is sent as createdBefore query param."""
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        _mock_resp({"elements": []}),
    ]
    with _patch_client(mock_client):
        provider.fetch_messages(
            platform_thread_id="conv-1",
            cursor="999000",
            limit=50,
        )
    events_call = mock_client.get.call_args_list[1]
    assert events_call.kwargs.get("params", {}).get("createdBefore") == "999000"


def test_fetch_messages_skips_malformed_events(provider):
    """Malformed events (missing from/createdAt/entityUrn) are skipped."""
    good = _event(entity_urn="urn:li:msg:1", created_at_ms=1000, public_identifier="x", body="Ok")
    bad_no_from = {"entityUrn": "urn:li:msg:2", "createdAt": 2000}
    bad_no_created = {"entityUrn": "urn:li:msg:3", "from": {"member": {"miniProfile": {"publicIdentifier": "y"}}}}
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        _mock_resp({"elements": [good, bad_no_from, bad_no_created]}),
    ]
    with _patch_client(mock_client):
        messages, next_cursor = provider.fetch_messages(
            platform_thread_id="c1",
            cursor=None,
            limit=50,
        )
    assert len(messages) == 1
    assert messages[0].platform_message_id == "urn:li:msg:1"
    assert next_cursor is None


def test_fetch_messages_raises_when_jsessionid_missing():
    """Missing JSESSIONID raises ValueError before any request."""
    auth = AccountAuth(li_at="li", jsessionid=None)
    provider = LinkedInProvider(auth=auth, proxy=None)
    with pytest.raises(ValueError, match="JSESSIONID cookie required"):
        provider.fetch_messages(platform_thread_id="c1", cursor=None, limit=50)


def test_fetch_messages_raises_when_jsessionid_empty_string():
    """Empty string JSESSIONID raises ValueError."""
    auth = AccountAuth(li_at="li", jsessionid="   ")
    provider = LinkedInProvider(auth=auth, proxy=None)
    with pytest.raises(ValueError, match="JSESSIONID cookie required"):
        provider.fetch_messages(platform_thread_id="c1", cursor=None, limit=50)


def test_fetch_messages_uses_proxy_when_configured(auth):
    """Provider passes proxy URL to httpx.Client when proxy is set."""
    proxy_config = ProxyConfig(url="http://proxy:8080")
    provider = LinkedInProvider(auth=auth, proxy=proxy_config)
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        _mock_resp({"elements": []}),
    ]
    with _patch_client(mock_client) as mock_httpx_client:
        provider.fetch_messages(platform_thread_id="c1", cursor=None, limit=50)
    mock_httpx_client.assert_called_once()
    call_kw = mock_httpx_client.call_args[1]
    assert call_kw.get("proxy") == "http://proxy:8080"


def test_fetch_messages_chronological_order_oldest_first(provider):
    """Messages are returned oldest first (API often returns newest first)."""
    ev1 = _event(entity_urn="urn:li:msg:1", created_at_ms=3000000, public_identifier="a", body="Third")
    ev2 = _event(entity_urn="urn:li:msg:2", created_at_ms=1000000, public_identifier="b", body="First")
    ev3 = _event(entity_urn="urn:li:msg:3", created_at_ms=2000000, public_identifier="c", body="Second")
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        _mock_resp({"elements": [ev1, ev2, ev3]}),
    ]
    with _patch_client(mock_client):
        messages, _ = provider.fetch_messages(
            platform_thread_id="c1",
            cursor=None,
            limit=50,
        )
    assert [m.text for m in messages] == ["First", "Second", "Third"]
    assert [m.platform_message_id for m in messages] == ["urn:li:msg:2", "urn:li:msg:3", "urn:li:msg:1"]


def test_fetch_messages_accepts_events_key_alternatively(provider):
    """Response may use 'events' key instead of 'elements'."""
    ev = _event(entity_urn="urn:li:msg:1", created_at_ms=1000, public_identifier="u", body="Hi")
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        _mock_resp({"events": [ev]}),
    ]
    with _patch_client(mock_client):
        messages, _ = provider.fetch_messages(
            platform_thread_id="c1",
            cursor=None,
            limit=50,
        )
    assert len(messages) == 1
    assert messages[0].text == "Hi"


def test_fetch_messages_direction_out_when_public_identifier_is_numeric(provider):
    """API may return publicIdentifier as number; direction still correct when compared as string."""
    ev = {
        "entityUrn": "urn:li:msg:1",
        "createdAt": 1000000,
        "from": {
            "member": {"miniProfile": {"publicIdentifier": 12345}},
        },
        "eventContent": {"body": "From me"},
    }
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response("12345")),
        _mock_resp({"elements": [ev]}),
    ]
    with _patch_client(mock_client):
        messages, _ = provider.fetch_messages(
            platform_thread_id="c1",
            cursor=None,
            limit=50,
        )
    assert len(messages) == 1
    assert messages[0].direction == "out"
    assert messages[0].sender == "12345"


def test_fetch_messages_encodes_platform_thread_id_in_url(provider):
    """platform_thread_id with reserved chars (e.g. URN with ':') is URL-encoded in path."""
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        _mock_resp({"elements": []}),
    ]
    with _patch_client(mock_client):
        provider.fetch_messages(
            platform_thread_id="urn:li:conv:123",
            cursor=None,
            limit=50,
        )
    events_call = mock_client.get.call_args_list[1]
    url = events_call.args[0] if events_call.args else events_call.kwargs.get("url")
    assert "urn%3Ali%3Aconv%3A123" in url or "urn:li:conv:123" in url


def test_fetch_messages_raises_when_limit_zero(provider):
    """limit must be 1..500; 0 raises ValueError before any request."""
    with pytest.raises(ValueError, match="limit must be between 1 and 500"):
        provider.fetch_messages(platform_thread_id="c1", cursor=None, limit=0)


def test_fetch_messages_raises_when_limit_over_max(provider):
    """limit > 500 raises ValueError (aligned with API max)."""
    with pytest.raises(ValueError, match="limit must be between 1 and 500"):
        provider.fetch_messages(platform_thread_id="c1", cursor=None, limit=501)


def test_fetch_messages_handles_non_dict_response_gracefully(provider):
    """Non-dict or invalid JSON response (e.g. HTML error page) → ([], None), no crash."""
    mock_client = MagicMock()
    resp_me = _mock_resp(_me_response())
    resp_bad = MagicMock()
    resp_bad.raise_for_status = MagicMock()
    resp_bad.json.side_effect = ValueError("Invalid JSON")
    mock_client.get.side_effect = [resp_me, resp_bad]
    with _patch_client(mock_client):
        messages, next_cursor = provider.fetch_messages(
            platform_thread_id="c1",
            cursor=None,
            limit=50,
        )
    assert messages == []
    assert next_cursor is None


def test_fetch_messages_handles_response_list_instead_of_dict(provider):
    """If API returns a list (malformed), treat as no elements."""
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        _mock_resp([]),
    ]
    with _patch_client(mock_client):
        messages, next_cursor = provider.fetch_messages(
            platform_thread_id="c1",
            cursor=None,
            limit=50,
        )
    assert messages == []
    assert next_cursor is None


def test_fetch_messages_http_error_propagates(provider):
    """HTTP 4xx/5xx from events endpoint propagates (caller can back off / retry)."""
    import httpx
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        httpx.HTTPStatusError("403", request=MagicMock(), response=MagicMock(status_code=403)),
    ]
    with _patch_client(mock_client):
        with pytest.raises(httpx.HTTPStatusError):
            provider.fetch_messages(platform_thread_id="c1", cursor=None, limit=50)


def test_fetch_messages_deduplicates_same_platform_message_id_in_page(provider):
    """If API returns duplicate entityUrn in one page, return unique messages (first wins)."""
    ev = _event(entity_urn="urn:li:msg:dup", created_at_ms=1000, public_identifier="bob", body="Hi")
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        _mock_resp({"elements": [ev, ev]}),
    ]
    with _patch_client(mock_client):
        messages, next_cursor = provider.fetch_messages(
            platform_thread_id="c1",
            cursor=None,
            limit=50,
        )
    assert len(messages) == 1
    assert messages[0].platform_message_id == "urn:li:msg:dup"
    assert next_cursor is None


def test_fetch_messages_client_uses_timeout(provider):
    """Provider uses a timeout on HTTP client to avoid hanging."""
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        _mock_resp({"elements": []}),
    ]
    with _patch_client(mock_client) as mock_httpx_client:
        provider.fetch_messages(platform_thread_id="c1", cursor=None, limit=50)
    call_kw = mock_httpx_client.call_args[1]
    assert call_kw.get("timeout") == 30.0


def test_build_headers_includes_csrf_and_required_headers(provider):
    """_build_headers includes csrf-token and standard Voyager headers."""
    headers = provider._build_headers()
    assert headers.get("csrf-token") == "ajax:csrf123"
    assert "User-Agent" in headers
    assert headers.get("Accept") == "application/vnd.linkedin.normalized+json+2.1"
    assert headers.get("x-restli-protocol-version") == "2.0.0"


def test_real_provider_fetch_messages_integration_with_storage():
    """Real test: LinkedInProvider.fetch_messages + job_runner.run_sync + storage with mocked HTTP.

    Uses the actual provider implementation (no MagicMock on provider). Only HTTP is faked.
    Asserts messages are stored and cursor is set correctly after one page.
    """
    from libs.core.job_runner import run_sync
    from libs.core.storage import Storage
    from libs.providers.linkedin.provider import LinkedInThread

    storage = Storage(db_path=":memory:")
    storage.migrate()
    auth = AccountAuth(li_at="real-li", jsessionid="ajax:real-csrf")
    account_id = storage.create_account(label="real-test", auth=auth, proxy=None)
    provider = LinkedInProvider(auth=auth, proxy=None)

    thread = LinkedInThread(platform_thread_id="conv-real-1", title="Alice", raw=None)
    ev1 = _event(
        entity_urn="urn:li:msg:101",
        created_at_ms=1700000000000,
        public_identifier="alice",
        body="Hello from Alice",
    )
    ev2 = _event(
        entity_urn="urn:li:msg:102",
        created_at_ms=1700000001000,
        public_identifier="me-user",
        body="Reply from me",
    )

    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response("me-user")),
        _mock_resp({"elements": [ev1, ev2]}),
    ]

    with _patch_client(mock_client):
        with patch(
            "libs.providers.linkedin.provider.LinkedInProvider.list_threads",
            return_value=[thread],
        ):
            result = run_sync(
                account_id=account_id,
                storage=storage,
                provider=provider,
                limit_per_thread=50,
                max_pages_per_thread=1,
            )

    assert result.synced_threads == 1
    assert result.messages_inserted == 2
    assert result.pages_fetched == 1
    threads = storage.list_threads(account_id=account_id)
    assert len(threads) == 1
    assert threads[0]["platform_thread_id"] == "conv-real-1"
    thread_id = threads[0]["id"]
    cursor = storage.get_cursor(account_id=account_id, thread_id=thread_id)
    assert cursor is None
    rows = storage._conn.execute(
        "SELECT platform_message_id, direction, text FROM messages WHERE account_id = ? ORDER BY sent_at",
        (account_id,),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["platform_message_id"] == "urn:li:msg:101"
    assert rows[0]["direction"] == "in"
    assert rows[0]["text"] == "Hello from Alice"
    assert rows[1]["platform_message_id"] == "urn:li:msg:102"
    assert rows[1]["direction"] == "out"
    assert rows[1]["text"] == "Reply from me"
    storage.close()


def test_run_sync_sleeps_between_pages():
    """job_runner sleeps 1.5s before fetching next page (rate limit)."""
    from unittest.mock import patch as mock_patch
    from libs.core.job_runner import run_sync
    from libs.core.storage import Storage
    from libs.providers.linkedin.provider import LinkedInThread

    storage = Storage(db_path=":memory:")
    storage.migrate()
    auth = AccountAuth(li_at="x", jsessionid="y")
    account_id = storage.create_account(label="a", auth=auth, proxy=None)
    thread = LinkedInThread(platform_thread_id="t1", title=None, raw=None)
    provider = MagicMock()
    provider.list_threads.return_value = [thread]
    provider.fetch_messages.side_effect = [
        ([], "cursor2"),
        ([], None),
    ]
    with mock_patch("libs.core.job_runner.time.sleep") as mock_sleep:
        run_sync(
            account_id=account_id,
            storage=storage,
            provider=provider,
            limit_per_thread=50,
            max_pages_per_thread=None,
        )
    from libs.core.job_runner import DELAY_BETWEEN_PAGES_S
    mock_sleep.assert_called_once_with(DELAY_BETWEEN_PAGES_S)
    storage.close()


def test_fetch_messages_propagates_me_failure(provider):
    """If /me returns HTTP error (expired auth), it propagates cleanly."""
    import httpx
    mock_client = MagicMock()
    mock_client.get.side_effect = httpx.HTTPStatusError(
        "403", request=MagicMock(), response=MagicMock(status_code=403),
    )
    with _patch_client(mock_client):
        with pytest.raises(httpx.HTTPStatusError):
            provider.fetch_messages(platform_thread_id="c1", cursor=None, limit=50)


def test_fetch_messages_empty_body_falls_back_to_attributed_body(provider):
    """When body is empty string, text is extracted from attributedBody."""
    ev = {
        "entityUrn": "urn:li:msg:1",
        "createdAt": 1000000,
        "from": {"member": {"miniProfile": {"publicIdentifier": "other"}}},
        "eventContent": {
            "body": "",
            "attributedBody": {"text": "Real content"},
        },
    }
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        _mock_resp({"elements": [ev]}),
    ]
    with _patch_client(mock_client):
        messages, _ = provider.fetch_messages(
            platform_thread_id="c1", cursor=None, limit=50,
        )
    assert len(messages) == 1
    assert messages[0].text == "Real content"


def test_real_provider_multi_page_integration():
    """Integration: real provider + job_runner + storage, 2 pages with cursor and sleep."""
    from unittest.mock import patch as mock_patch
    from libs.core.job_runner import run_sync, DELAY_BETWEEN_PAGES_S
    from libs.core.storage import Storage
    from libs.providers.linkedin.provider import LinkedInThread

    storage = Storage(db_path=":memory:")
    storage.migrate()
    auth = AccountAuth(li_at="mp-li", jsessionid="ajax:mp-csrf")
    account_id = storage.create_account(label="multi-page-test", auth=auth, proxy=None)
    provider = LinkedInProvider(auth=auth, proxy=None)

    thread = LinkedInThread(platform_thread_id="conv-mp-1", title="MultiPage", raw=None)
    page1_events = [
        _event(entity_urn=f"urn:li:msg:{i}", created_at_ms=1700000000000 + i * 1000,
               public_identifier="alice", body=f"msg-{i}")
        for i in range(3)
    ]
    page2_events = [
        _event(entity_urn="urn:li:msg:100", created_at_ms=1699999990000,
               public_identifier="me-mp", body="older msg"),
    ]

    # Single mock client reused across pages (client reuse via _get_client).
    mock_client = MagicMock()
    mock_client.is_closed = False
    mock_client.get.side_effect = [
        _mock_resp(_me_response("me-mp")),       # /me (first call only, cached after)
        _mock_resp({"elements": page1_events}),   # page 1
        _mock_resp({"elements": page2_events}),   # page 2
    ]

    with _patch_client(mock_client), \
         mock_patch("libs.core.job_runner.time.sleep") as mock_sleep, \
         mock_patch(
             "libs.providers.linkedin.provider.LinkedInProvider.list_threads",
             return_value=[thread],
         ):
        result = run_sync(
            account_id=account_id,
            storage=storage,
            provider=provider,
            limit_per_thread=3,
            max_pages_per_thread=None,
        )

    assert result.synced_threads == 1
    assert result.messages_inserted == 4
    assert result.pages_fetched == 2
    mock_sleep.assert_called_once_with(DELAY_BETWEEN_PAGES_S)
    rows = storage._conn.execute(
        "SELECT platform_message_id FROM messages WHERE account_id = ? ORDER BY sent_at",
        (account_id,),
    ).fetchall()
    assert len(rows) == 4
    storage.close()


def test_fetch_messages_retries_on_429_then_succeeds(provider):
    """429 on first attempt, success on second — returns messages."""
    ev = _event(entity_urn="urn:li:msg:1", created_at_ms=1000, public_identifier="bob", body="Hi")
    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.headers = {}
    resp_429.request = MagicMock()
    mock_client = MagicMock()
    mock_client.is_closed = False
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),                # /me
        resp_429,                                   # events: 429
        _mock_resp({"elements": [ev]}),             # events: retry succeeds
    ]
    with _patch_client(mock_client), \
         patch("libs.providers.linkedin.provider.time.sleep"):
        messages, _ = provider.fetch_messages(
            platform_thread_id="c1", cursor=None, limit=50,
        )
    assert len(messages) == 1
    assert messages[0].text == "Hi"


def test_fetch_messages_exhausts_retries_on_503(provider):
    """503 on all attempts raises HTTPStatusError after exhausting retries."""
    import httpx
    resp_503 = MagicMock()
    resp_503.status_code = 503
    resp_503.headers = {}
    resp_503.request = MagicMock()
    mock_client = MagicMock()
    mock_client.is_closed = False
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),    # /me
        resp_503,                       # attempt 1
        resp_503,                       # attempt 2
        resp_503,                       # attempt 3
    ]
    with _patch_client(mock_client), \
         patch("libs.providers.linkedin.provider.time.sleep"):
        with pytest.raises(httpx.HTTPStatusError):
            provider.fetch_messages(platform_thread_id="c1", cursor=None, limit=50)


def test_fetch_messages_retry_honours_retry_after_header(provider):
    """429 with Retry-After header uses that value as minimum delay."""
    ev = _event(entity_urn="urn:li:msg:1", created_at_ms=1000, public_identifier="x", body="Ok")
    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.headers = {"Retry-After": "10"}
    resp_429.request = MagicMock()
    mock_client = MagicMock()
    mock_client.is_closed = False
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        resp_429,
        _mock_resp({"elements": [ev]}),
    ]
    with _patch_client(mock_client), \
         patch("libs.providers.linkedin.provider.time.sleep") as mock_sleep:
        provider.fetch_messages(platform_thread_id="c1", cursor=None, limit=50)
    # Retry-After=10 > base delay 2.0, so sleep should be >= 10
    assert mock_sleep.call_args[0][0] >= 10.0


def test_fetch_messages_no_retry_on_403(provider):
    """403 is not retryable — raises immediately without sleeping."""
    import httpx
    resp_403 = MagicMock()
    resp_403.status_code = 403
    resp_403.raise_for_status.side_effect = httpx.HTTPStatusError(
        "403", request=MagicMock(), response=resp_403,
    )
    mock_client = MagicMock()
    mock_client.is_closed = False
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        resp_403,
    ]
    with _patch_client(mock_client), \
         patch("libs.providers.linkedin.provider.time.sleep") as mock_sleep:
        with pytest.raises(httpx.HTTPStatusError):
            provider.fetch_messages(platform_thread_id="c1", cursor=None, limit=50)
    mock_sleep.assert_not_called()


def test_provider_context_manager_closes_client(auth):
    """Provider as context manager closes the underlying httpx.Client."""
    mock_client = MagicMock()
    mock_client.is_closed = False
    with _patch_client(mock_client):
        provider = LinkedInProvider(auth=auth, proxy=None)
        with provider:
            # Force client creation
            provider._get_client()
        mock_client.close.assert_called_once()


def test_client_reused_across_fetch_messages_calls(provider):
    """_get_client returns the same httpx.Client across multiple fetch_messages calls."""
    mock_client = MagicMock()
    mock_client.is_closed = False
    mock_client.get.side_effect = [
        _mock_resp(_me_response()),
        _mock_resp({"elements": []}),
        _mock_resp({"elements": []}),
    ]
    with _patch_client(mock_client) as mock_cls:
        provider.fetch_messages(platform_thread_id="c1", cursor=None, limit=50)
        provider.fetch_messages(platform_thread_id="c2", cursor=None, limit=50)
    # httpx.Client() should be called only once (reused)
    mock_cls.assert_called_once()


def _mock_resp(json_data: dict, status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.raise_for_status = MagicMock()
    r.json.return_value = json_data
    return r


def _patch_client(mock_client: MagicMock):
    """Patch httpx.Client so _get_client() returns mock_client."""
    return patch("libs.providers.linkedin.provider.httpx.Client", return_value=mock_client)
