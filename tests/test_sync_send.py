"""Tests for sync/send orchestration (job_runner) and API behavior."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from libs.core.job_runner import SendResult, SyncConfig, SyncResult, run_send, run_sync
from libs.core.models import AccountAuth
from libs.core.storage import Storage
from libs.providers.linkedin.provider import LinkedInMessage, LinkedInProvider, LinkedInThread


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.sqlite")


@pytest.fixture
def storage(db_path):
    s = Storage(db_path=db_path)
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def account_id(storage):
    auth = AccountAuth(li_at="test-li-at", jsessionid=None)
    return storage.create_account(label="test", auth=auth, proxy=None)


def _mock_provider(**kwargs):
    """Create a MagicMock provider with rate_limit_encountered attribute."""
    provider = MagicMock(spec=LinkedInProvider, **kwargs)
    provider.rate_limit_encountered = False
    return provider


def test_run_sync_upserts_threads_and_messages(storage, account_id):
    thread = LinkedInThread(platform_thread_id="urn:li:conv:1", title="Alice", raw=None)
    msg = LinkedInMessage(
        platform_message_id="mid-1",
        direction="in",
        sender="alice",
        text="Hi",
        sent_at=datetime.now(timezone.utc),
        raw=None,
    )
    provider = _mock_provider()
    provider.list_threads.return_value = [thread]
    provider.fetch_messages.return_value = ([msg], None)

    result = run_sync(
        account_id=account_id,
        storage=storage,
        provider=provider,
        limit_per_thread=50,
    )
    assert isinstance(result, SyncResult)
    assert result.synced_threads == 1
    assert result.messages_inserted == 1
    assert result.messages_skipped_duplicate == 0
    assert result.pages_fetched == 1
    assert result.rate_limited is False
    provider.list_threads.assert_called_once()
    provider.fetch_messages.assert_called_once_with(
        platform_thread_id="urn:li:conv:1",
        cursor=None,
        limit=50,
    )
    threads = storage.list_threads(account_id=account_id)
    assert len(threads) == 1
    assert threads[0]["platform_thread_id"] == "urn:li:conv:1"
    assert threads[0]["title"] == "Alice"


def test_run_sync_empty_threads_returns_zero_counts(storage, account_id):
    provider = _mock_provider()
    provider.list_threads.return_value = []

    result = run_sync(
        account_id=account_id,
        storage=storage,
        provider=provider,
        limit_per_thread=50,
    )
    assert result.synced_threads == 0
    assert result.messages_inserted == 0
    assert result.messages_skipped_duplicate == 0
    assert result.pages_fetched == 0
    provider.list_threads.assert_called_once()
    provider.fetch_messages.assert_not_called()


def test_run_sync_multiple_threads_and_messages(storage, account_id):
    t1 = LinkedInThread(platform_thread_id="conv-1", title="A", raw=None)
    t2 = LinkedInThread(platform_thread_id="conv-2", title="B", raw=None)
    msg1 = LinkedInMessage(
        platform_message_id="m1",
        direction="out",
        sender=None,
        text="x",
        sent_at=datetime.now(timezone.utc),
        raw=None,
    )
    msg2 = LinkedInMessage(
        platform_message_id="m2",
        direction="in",
        sender="b",
        text="y",
        sent_at=datetime.now(timezone.utc),
        raw=None,
    )
    provider = _mock_provider()
    provider.list_threads.return_value = [t1, t2]
    provider.fetch_messages.side_effect = [([msg1], None), ([msg2], None)]

    result = run_sync(
        account_id=account_id,
        storage=storage,
        provider=provider,
        limit_per_thread=10,
    )
    assert result.synced_threads == 2
    assert result.messages_inserted == 2
    assert result.pages_fetched == 2
    assert len(storage.list_threads(account_id=account_id)) == 2


def test_run_sync_duplicate_messages_counted_as_skipped(storage, account_id):
    thread = LinkedInThread(platform_thread_id="t1", title=None, raw=None)
    msg = LinkedInMessage(
        platform_message_id="dup-1",
        direction="in",
        sender="x",
        text="Hi",
        sent_at=datetime.now(timezone.utc),
        raw=None,
    )
    provider = _mock_provider()
    provider.list_threads.return_value = [thread]
    provider.fetch_messages.return_value = ([msg], None)

    result1 = run_sync(
        account_id=account_id,
        storage=storage,
        provider=provider,
        limit_per_thread=50,
    )
    assert result1.messages_inserted == 1
    assert result1.messages_skipped_duplicate == 0

    result2 = run_sync(
        account_id=account_id,
        storage=storage,
        provider=provider,
        limit_per_thread=50,
    )
    assert result2.messages_inserted == 0
    assert result2.messages_skipped_duplicate == 1


def test_run_sync_uses_stored_cursor(storage, account_id):
    thread = LinkedInThread(platform_thread_id="t1", title=None, raw=None)
    provider = _mock_provider()
    provider.list_threads.return_value = [thread]
    provider.fetch_messages.return_value = ([], None)
    thread_id = storage.upsert_thread(
        account_id=account_id, platform_thread_id="t1", title=None
    )
    storage.set_cursor(account_id=account_id, thread_id=thread_id, cursor="page2")

    run_sync(
        account_id=account_id,
        storage=storage,
        provider=provider,
        limit_per_thread=10,
    )
    provider.fetch_messages.assert_called_once_with(
        platform_thread_id="t1",
        cursor="page2",
        limit=10,
    )


def test_run_sync_exhausts_cursor_when_max_pages_none(storage, account_id):
    thread = LinkedInThread(platform_thread_id="t1", title=None, raw=None)
    msg1 = LinkedInMessage(
        platform_message_id="m1",
        direction="in",
        sender=None,
        text="x",
        sent_at=datetime.now(timezone.utc),
        raw=None,
    )
    msg2 = LinkedInMessage(
        platform_message_id="m2",
        direction="out",
        sender=None,
        text="y",
        sent_at=datetime.now(timezone.utc),
        raw=None,
    )
    provider = _mock_provider()
    provider.list_threads.return_value = [thread]
    provider.fetch_messages.side_effect = [
        ([msg1], "cursor2"),
        ([msg2], None),
    ]

    result = run_sync(
        account_id=account_id,
        storage=storage,
        provider=provider,
        limit_per_thread=50,
        max_pages_per_thread=None,
    )
    assert result.pages_fetched == 2
    assert result.messages_inserted == 2
    assert provider.fetch_messages.call_count == 2


def test_run_sync_respects_max_pages_per_thread(storage, account_id):
    thread = LinkedInThread(platform_thread_id="t1", title=None, raw=None)
    provider = _mock_provider()
    provider.list_threads.return_value = [thread]
    provider.fetch_messages.return_value = ([], "next")

    result = run_sync(
        account_id=account_id,
        storage=storage,
        provider=provider,
        limit_per_thread=10,
        max_pages_per_thread=2,
    )
    assert result.pages_fetched == 2
    assert provider.fetch_messages.call_count == 2


def test_run_sync_normalizes_naive_sent_at(storage, account_id):
    thread = LinkedInThread(platform_thread_id="t1", title=None, raw=None)
    naive_dt = datetime(2025, 3, 1, 12, 0, 0)
    msg = LinkedInMessage(
        platform_message_id="m1",
        direction="out",
        sender=None,
        text="Bye",
        sent_at=naive_dt,
        raw=None,
    )
    provider = _mock_provider()
    provider.list_threads.return_value = [thread]
    provider.fetch_messages.return_value = ([msg], None)

    run_sync(
        account_id=account_id,
        storage=storage,
        provider=provider,
        limit_per_thread=50,
    )
    rows = storage._conn.execute(
        "SELECT sent_at FROM messages WHERE account_id=? AND platform_message_id=?",
        (account_id, "m1"),
    ).fetchall()
    assert len(rows) == 1
    assert "Z" in rows[0]["sent_at"] or "+00:00" in rows[0]["sent_at"]


def test_run_sync_rate_limited_flag_propagated(storage, account_id):
    thread = LinkedInThread(platform_thread_id="t1", title=None, raw=None)
    provider = _mock_provider()
    provider.list_threads.return_value = [thread]
    provider.fetch_messages.return_value = ([], None)
    provider.rate_limit_encountered = True

    result = run_sync(
        account_id=account_id,
        storage=storage,
        provider=provider,
        limit_per_thread=50,
    )
    assert result.rate_limited is True


def test_run_sync_respects_sync_config_delays(storage, account_id):
    """SyncConfig delays are forwarded to time.sleep between threads and pages."""
    from unittest.mock import patch, call

    t1 = LinkedInThread(platform_thread_id="c1", title=None, raw=None)
    t2 = LinkedInThread(platform_thread_id="c2", title=None, raw=None)
    msg = LinkedInMessage(
        platform_message_id="m1",
        direction="in",
        sender=None,
        text="x",
        sent_at=datetime.now(timezone.utc),
        raw=None,
    )
    provider = _mock_provider()
    provider.list_threads.return_value = [t1, t2]
    provider.fetch_messages.side_effect = [
        ([msg], "next"),
        ([], None),
        ([], None),
    ]
    cfg = SyncConfig(delay_between_threads_s=3.5, delay_between_pages_s=1.0)

    with patch("libs.core.job_runner.time.sleep") as mock_sleep:
        result = run_sync(
            account_id=account_id,
            storage=storage,
            provider=provider,
            limit_per_thread=50,
            max_pages_per_thread=None,
            sync_config=cfg,
        )

    assert result.synced_threads == 2
    sleep_calls = [c.args[0] for c in mock_sleep.call_args_list]
    assert 3.5 in sleep_calls
    assert 1.0 in sleep_calls


def test_run_sync_logs_rate_limit_warning(storage, account_id, caplog):
    """When provider signals rate-limit, run_sync logs a warning with account_id."""
    import logging

    provider = _mock_provider()
    provider.list_threads.return_value = []
    provider.rate_limit_encountered = True

    with caplog.at_level(logging.WARNING, logger="libs.core.job_runner"):
        result = run_sync(
            account_id=account_id,
            storage=storage,
            provider=provider,
            limit_per_thread=50,
        )
    assert result.rate_limited is True
    assert any("rate-limit" in r.message.lower() for r in caplog.records)
    assert any(str(account_id) in r.message for r in caplog.records)


def test_run_send_returns_platform_message_id(storage, account_id):
    provider = MagicMock(spec=LinkedInProvider)
    provider.send_message.return_value = "plat-msg-123"

    result = run_send(
        account_id=account_id,
        storage=storage,
        provider=provider,
        recipient="bob",
        text="Hello",
        idempotency_key="key-1",
    )
    assert isinstance(result, SendResult)
    assert result.platform_message_id == "plat-msg-123"
    assert result.status == "sent"
    assert result.was_duplicate is False
    assert result.send_id >= 1
    provider.send_message.assert_called_once_with(
        recipient="bob",
        text="Hello",
    )


def test_run_send_with_none_idempotency_key(storage, account_id):
    provider = MagicMock(spec=LinkedInProvider)
    provider.send_message.return_value = "id-1"

    result = run_send(
        account_id=account_id,
        storage=storage,
        provider=provider,
        recipient="alice",
        text="Hi",
        idempotency_key=None,
    )
    assert result.platform_message_id == "id-1"
    assert result.status == "sent"
    assert result.was_duplicate is False
    provider.send_message.assert_called_once_with(
        recipient="alice",
        text="Hi",
    )


def test_run_send_none_key_twice_creates_independent_records(storage, account_id):
    """Two calls with idempotency_key=None must both hit the provider and create separate records."""
    provider = MagicMock(spec=LinkedInProvider)
    provider.send_message.side_effect = ["msg-1", "msg-2"]

    r1 = run_send(
        account_id=account_id,
        storage=storage,
        provider=provider,
        recipient="bob",
        text="Hello",
        idempotency_key=None,
    )
    r2 = run_send(
        account_id=account_id,
        storage=storage,
        provider=provider,
        recipient="bob",
        text="Hello",
        idempotency_key=None,
    )
    assert r1.platform_message_id == "msg-1"
    assert r2.platform_message_id == "msg-2"
    assert r1.was_duplicate is False
    assert r2.was_duplicate is False
    assert r1.send_id != r2.send_id
    assert provider.send_message.call_count == 2


def test_run_send_idempotency_prevents_duplicate(storage, account_id):
    """Same idempotency key returns cached result without calling provider again."""
    provider = MagicMock(spec=LinkedInProvider)
    provider.send_message.return_value = "plat-msg-456"

    r1 = run_send(
        account_id=account_id,
        storage=storage,
        provider=provider,
        recipient="bob",
        text="Hello",
        idempotency_key="dedup-1",
    )
    r2 = run_send(
        account_id=account_id,
        storage=storage,
        provider=provider,
        recipient="bob",
        text="Hello",
        idempotency_key="dedup-1",
    )
    assert r1.status == "sent"
    assert r1.was_duplicate is False
    assert r2.status == "sent"
    assert r2.was_duplicate is True
    assert r2.platform_message_id == "plat-msg-456"
    assert provider.send_message.call_count == 1


def test_run_send_different_keys_send_separately(storage, account_id):
    provider = MagicMock(spec=LinkedInProvider)
    provider.send_message.side_effect = ["msg-a", "msg-b"]

    r1 = run_send(
        account_id=account_id,
        storage=storage,
        provider=provider,
        recipient="bob",
        text="Hello",
        idempotency_key="key-a",
    )
    r2 = run_send(
        account_id=account_id,
        storage=storage,
        provider=provider,
        recipient="bob",
        text="Hello",
        idempotency_key="key-b",
    )
    assert r1.platform_message_id == "msg-a"
    assert r2.platform_message_id == "msg-b"
    assert provider.send_message.call_count == 2


def test_run_send_retries_failed_with_same_key(storage, account_id):
    """A failed send can be retried with the same idempotency key."""
    provider = MagicMock(spec=LinkedInProvider)
    provider.send_message.side_effect = [
        ConnectionError("network down"),
        "plat-msg-retry-ok",
    ]

    with pytest.raises(ConnectionError):
        run_send(
            account_id=account_id,
            storage=storage,
            provider=provider,
            recipient="bob",
            text="Hello",
            idempotency_key="retry-key",
        )

    result = run_send(
        account_id=account_id,
        storage=storage,
        provider=provider,
        recipient="bob",
        text="Hello",
        idempotency_key="retry-key",
    )
    assert result.status == "sent"
    assert result.platform_message_id == "plat-msg-retry-ok"
    assert result.was_duplicate is False
    assert provider.send_message.call_count == 2


def test_run_send_records_outbound_send(storage, account_id):
    """Outbound send is persisted and queryable via storage."""
    provider = MagicMock(spec=LinkedInProvider)
    provider.send_message.return_value = "msg-99"

    result = run_send(
        account_id=account_id,
        storage=storage,
        provider=provider,
        recipient="alice",
        text="Hey",
        idempotency_key="track-1",
    )
    record = storage.get_outbound_send(send_id=result.send_id)
    assert record is not None
    assert record["status"] == "sent"
    assert record["platform_message_id"] == "msg-99"
    assert record["recipient"] == "alice"
    assert record["attempts"] == 1


def test_run_send_failure_records_error(storage, account_id):
    """Failed sends are recorded with error details."""
    provider = MagicMock(spec=LinkedInProvider)
    provider.send_message.side_effect = PermissionError("HTTP 401")

    with pytest.raises(PermissionError):
        run_send(
            account_id=account_id,
            storage=storage,
            provider=provider,
            recipient="bob",
            text="Hello",
            idempotency_key="fail-key",
        )

    sends = storage.list_outbound_sends(account_id=account_id, status="failed")
    assert len(sends) == 1
    assert sends[0]["last_error"] == "HTTP 401"
    assert sends[0]["attempts"] == 1


def test_run_send_rejects_pending_record(storage, account_id):
    """A pending record blocks concurrent sends instead of racing."""
    storage.create_or_get_outbound_send(
        account_id=account_id,
        idempotency_key="in-flight",
        recipient="bob",
        text="Hello",
    )

    provider = MagicMock(spec=LinkedInProvider)
    with pytest.raises(RuntimeError, match="already in progress"):
        run_send(
            account_id=account_id,
            storage=storage,
            provider=provider,
            recipient="bob",
            text="Hello",
            idempotency_key="in-flight",
        )
    provider.send_message.assert_not_called()


def test_run_send_rejects_payload_mismatch(storage, account_id):
    """Reusing a key with different recipient/text raises instead of silently deduping."""
    provider = MagicMock(spec=LinkedInProvider)
    provider.send_message.return_value = "msg-original"

    run_send(
        account_id=account_id,
        storage=storage,
        provider=provider,
        recipient="alice",
        text="First message",
        idempotency_key="reused-key",
    )
    assert provider.send_message.call_count == 1

    with pytest.raises(ValueError, match="different recipient/text"):
        run_send(
            account_id=account_id,
            storage=storage,
            provider=provider,
            recipient="bob",
            text="Different message",
            idempotency_key="reused-key",
        )
    assert provider.send_message.call_count == 1


# --- API tests (patched storage) ---


def test_sync_endpoint_404_for_unknown_account(db_path):
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from apps.api.main import app

    empty_storage = Storage(db_path=db_path)
    empty_storage.migrate()
    try:
        with patch("apps.api.main.storage", empty_storage):
            client = TestClient(app)
            resp = client.post("/sync", json={"account_id": 1, "limit_per_thread": 50})
        assert resp.status_code == 404
    finally:
        empty_storage.close()


def test_sync_endpoint_422_for_invalid_limit_per_thread(storage, account_id):
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from apps.api.main import app

    with patch("apps.api.main.storage", storage):
        client = TestClient(app)
        resp = client.post(
            "/sync",
            json={"account_id": account_id, "limit_per_thread": 0},
        )
    assert resp.status_code == 422


def test_sync_endpoint_422_when_jsessionid_missing(storage, account_id):
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from apps.api.main import app

    with patch("apps.api.main.storage", storage):
        client = TestClient(app)
        resp = client.post(
            "/sync",
            json={"account_id": account_id, "limit_per_thread": 50},
        )
    # Account has jsessionid=None → list_threads raises ValueError → 422
    assert resp.status_code == 422
    assert "jsessionid" in resp.json()["detail"].lower()

def test_sync_endpoint_422_when_me_bootstrap_returns_blocked_html(storage, account_id):
    from unittest.mock import MagicMock, patch
    from fastapi.testclient import TestClient
    from apps.api.main import app

    provider = MagicMock()
    provider.rate_limit_encountered = False
    provider.list_threads.side_effect = RuntimeError(
        "LinkedIn /voyager/api/me bootstrap returned blocked HTML. "
        "Refresh via POST /accounts/refresh and retry sync."
    )

    with patch("apps.api.main.storage", storage), patch(
        "apps.api.main.LinkedInProvider", return_value=provider
    ):
        client = TestClient(app)
        resp = client.post(
            "/sync",
            json={"account_id": account_id, "limit_per_thread": 50},
        )

    assert resp.status_code == 422
    assert "/voyager/api/me" in resp.json()["detail"]
    assert "POST /accounts/refresh" in resp.json()["detail"]


def test_sync_endpoint_returns_detailed_counts(storage, account_id):
    from unittest.mock import patch, MagicMock
    from fastapi.testclient import TestClient
    from apps.api.main import app
    from libs.providers.linkedin.provider import LinkedInThread, LinkedInMessage

    provider = MagicMock()
    provider.list_threads.return_value = [
        LinkedInThread(platform_thread_id="t1", title=None, raw=None),
    ]
    provider.fetch_messages.return_value = ([], None)
    with patch("apps.api.main.storage", storage), patch(
        "apps.api.main.LinkedInProvider", return_value=provider
    ):
        client = TestClient(app)
        resp = client.post(
            "/sync",
            json={"account_id": account_id, "limit_per_thread": 50},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "synced_threads" in data
    assert "messages_inserted" in data
    assert "messages_skipped_duplicate" in data
    assert "pages_fetched" in data
    assert "rate_limited" in data


def test_send_endpoint_404_for_unknown_account(db_path):
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from apps.api.main import app

    empty_storage = Storage(db_path=db_path)
    empty_storage.migrate()
    try:
        with patch("apps.api.main.storage", empty_storage):
            client = TestClient(app)
            resp = client.post(
                "/send",
                json={
                    "account_id": 1,
                    "recipient": "x",
                    "text": "hi",
                    "idempotency_key": None,
                },
            )
        assert resp.status_code == 404
    finally:
        empty_storage.close()


def test_send_endpoint_422_for_empty_recipient(storage, account_id):
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from apps.api.main import app

    with patch("apps.api.main.storage", storage):
        client = TestClient(app)
        resp = client.post(
            "/send",
            json={
                "account_id": account_id,
                "recipient": "",
                "text": "hi",
                "idempotency_key": None,
            },
        )
    assert resp.status_code == 422


def test_send_endpoint_422_for_empty_text(storage, account_id):
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from apps.api.main import app

    with patch("apps.api.main.storage", storage):
        client = TestClient(app)
        resp = client.post(
            "/send",
            json={
                "account_id": account_id,
                "recipient": "bob",
                "text": "",
                "idempotency_key": None,
            },
        )
    assert resp.status_code == 422


# --- GET /sends endpoint tests ---


def test_sends_endpoint_returns_records(storage, account_id):
    from unittest.mock import patch, MagicMock
    from fastapi.testclient import TestClient
    from apps.api.main import app

    provider = MagicMock(spec=LinkedInProvider)
    provider.send_message.return_value = "msg-sends-1"

    with patch("apps.api.main.storage", storage), patch(
        "apps.api.main.LinkedInProvider", return_value=provider
    ):
        client = TestClient(app)
        client.post(
            "/send",
            json={"account_id": account_id, "recipient": "alice", "text": "hi"},
        )
        resp = client.get(f"/sends?account_id={account_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["sends"]) == 1
    assert data["sends"][0]["status"] == "sent"
    assert data["sends"][0]["recipient"] == "alice"


def test_sends_endpoint_filters_by_status(storage, account_id):
    from unittest.mock import patch, MagicMock
    from fastapi.testclient import TestClient
    from apps.api.main import app

    provider = MagicMock(spec=LinkedInProvider)
    provider.send_message.return_value = "msg-filter-1"

    with patch("apps.api.main.storage", storage), patch(
        "apps.api.main.LinkedInProvider", return_value=provider
    ):
        client = TestClient(app)
        client.post(
            "/send",
            json={"account_id": account_id, "recipient": "bob", "text": "hey"},
        )
        resp_sent = client.get(f"/sends?account_id={account_id}&status=sent")
        resp_failed = client.get(f"/sends?account_id={account_id}&status=failed")
    assert resp_sent.status_code == 200
    assert len(resp_sent.json()["sends"]) == 1
    assert resp_failed.status_code == 200
    assert len(resp_failed.json()["sends"]) == 0


def test_sends_endpoint_422_on_invalid_status(storage, account_id):
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from apps.api.main import app

    with patch("apps.api.main.storage", storage):
        client = TestClient(app)
        resp = client.get(f"/sends?account_id={account_id}&status=bogus")
    assert resp.status_code == 422


# --- Cross-account idempotency key isolation ---


def test_run_send_same_key_different_accounts_are_independent(storage):
    auth = AccountAuth(li_at="test-li-at", jsessionid=None)
    acct_1 = storage.create_account(label="a1", auth=auth, proxy=None)
    acct_2 = storage.create_account(label="a2", auth=auth, proxy=None)

    provider = MagicMock(spec=LinkedInProvider)
    provider.send_message.side_effect = ["msg-acct1", "msg-acct2"]

    r1 = run_send(
        account_id=acct_1,
        storage=storage,
        provider=provider,
        recipient="bob",
        text="Hello",
        idempotency_key="shared-key",
    )
    r2 = run_send(
        account_id=acct_2,
        storage=storage,
        provider=provider,
        recipient="bob",
        text="Hello",
        idempotency_key="shared-key",
    )
    assert r1.platform_message_id == "msg-acct1"
    assert r2.platform_message_id == "msg-acct2"
    assert r1.was_duplicate is False
    assert r2.was_duplicate is False
    assert provider.send_message.call_count == 2


# --- Browser context threading tests ---


def test_sync_endpoint_stores_and_threads_browser_context(storage, account_id):
    """When /sync receives x_li_track and csrf_token, they are stored and passed to the provider."""
    from unittest.mock import patch, MagicMock, call
    from fastapi.testclient import TestClient
    from apps.api.main import app
    from libs.core.models import BrowserContext

    provider = MagicMock()
    provider.rate_limit_encountered = False
    provider.list_threads.return_value = []

    captured_context = {}

    def fake_provider(**kwargs):
        captured_context.update(kwargs)
        return provider

    with patch("apps.api.main.storage", storage), patch("apps.api.main.LinkedInProvider", side_effect=fake_provider):
        client = TestClient(app)
        resp = client.post(
            "/sync",
            json={
                "account_id": account_id,
                "x_li_track": '{"clientVersion":"1.13.42912"}',
                "csrf_token": "ajax:test-csrf",
            },
        )
    assert resp.status_code == 200
    ctx = captured_context.get("browser_context")
    assert ctx is not None
    assert ctx.x_li_track == '{"clientVersion":"1.13.42912"}'
    assert ctx.csrf_token == "ajax:test-csrf"
    stored = storage.get_browser_context(account_id)
    assert stored is not None
    assert stored.csrf_token == "ajax:test-csrf"


def test_send_endpoint_stores_and_threads_browser_context(storage, account_id):
    """When /send receives x_li_track and csrf_token, they are stored and passed to the provider."""
    from unittest.mock import patch, MagicMock
    from fastapi.testclient import TestClient
    from apps.api.main import app
    from libs.core.models import BrowserContext
    from libs.core.job_runner import SendResult

    provider = MagicMock(spec=LinkedInProvider)
    provider.send_message.return_value = "msg-ctx-1"

    captured_context = {}

    def fake_provider(**kwargs):
        captured_context.update(kwargs)
        return provider

    with patch("apps.api.main.storage", storage), patch("apps.api.main.LinkedInProvider", side_effect=fake_provider):
        client = TestClient(app)
        resp = client.post(
            "/send",
            json={
                "account_id": account_id,
                "recipient": "alice",
                "text": "hello",
                "x_li_track": '{"clientVersion":"1.99.0"}',
                "csrf_token": "ajax:send-csrf",
            },
        )
    assert resp.status_code == 200
    ctx = captured_context.get("browser_context")
    assert ctx is not None
    assert ctx.x_li_track == '{"clientVersion":"1.99.0"}'
    stored = storage.get_browser_context(account_id)
    assert stored is not None
    assert stored.csrf_token == "ajax:send-csrf"


def test_sync_endpoint_uses_stored_context_when_not_provided(storage, account_id):
    """If browser context was previously stored, /sync uses it even without new fields."""
    from unittest.mock import patch, MagicMock
    from fastapi.testclient import TestClient
    from apps.api.main import app
    from libs.core.models import BrowserContext

    storage.update_browser_context(account_id, BrowserContext(x_li_track="stored-track", csrf_token="stored-csrf"))

    provider = MagicMock()
    provider.rate_limit_encountered = False
    provider.list_threads.return_value = []
    captured_context = {}

    def fake_provider(**kwargs):
        captured_context.update(kwargs)
        return provider

    with patch("apps.api.main.storage", storage), patch("apps.api.main.LinkedInProvider", side_effect=fake_provider):
        client = TestClient(app)
        resp = client.post("/sync", json={"account_id": account_id})
    assert resp.status_code == 200
    ctx = captured_context.get("browser_context")
    assert ctx is not None
    assert ctx.x_li_track == "stored-track"
    assert ctx.csrf_token == "stored-csrf"


def test_provider_build_graphql_headers_prefers_browser_context():
    """Provider uses browser_context x_li_track and csrf_token over hardcoded values."""
    from libs.core.models import BrowserContext

    ctx = BrowserContext(x_li_track='{"clientVersion":"browser"}', csrf_token="ajax:browser-csrf")
    auth = AccountAuth(li_at="x", jsessionid="ajax:fallback")
    provider = LinkedInProvider(auth=auth, browser_context=ctx)
    headers = provider._build_graphql_headers()
    assert headers["x-li-track"] == '{"clientVersion":"browser"}'
    assert headers["csrf-token"] == "ajax:browser-csrf"


def test_provider_build_graphql_headers_falls_back_to_jsessionid():
    """Provider falls back to jsessionid when no browser_context is provided."""
    auth = AccountAuth(li_at="x", jsessionid="ajax:fallback")
    provider = LinkedInProvider(auth=auth)
    headers = provider._build_graphql_headers()
    assert headers["csrf-token"] == "ajax:fallback"


def test_provider_build_headers_prefers_browser_context():
    """_build_headers uses browser_context csrf_token and x_li_track for send."""
    from libs.core.models import BrowserContext

    ctx = BrowserContext(x_li_track='{"v":"browser"}', csrf_token="ajax:browser-csrf")
    auth = AccountAuth(li_at="x", jsessionid="ajax:fallback")
    provider = LinkedInProvider(auth=auth, browser_context=ctx)
    headers = provider._build_headers()
    assert headers["csrf-token"] == "ajax:browser-csrf"
    assert headers["x-li-track"] == '{"v":"browser"}'


def test_provider_build_headers_falls_back_to_jsessionid():
    """_build_headers falls back to jsessionid csrf when no browser_context."""
    auth = AccountAuth(li_at="x", jsessionid="ajax:session")
    provider = LinkedInProvider(auth=auth)
    headers = provider._build_headers()
    assert headers["csrf-token"] == "ajax:session"
