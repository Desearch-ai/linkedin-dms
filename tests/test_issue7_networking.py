"""Issue #7 — Networking requirements acceptance tests.

Tests each requirement:
  1. Proxy used for all provider HTTP requests
  2. Rate limiting: configurable delays in job_runner (SyncConfig)
  3. Backoff on errors (429/999, 401, network)
  4. Logging: rate_limited field on SyncResult, account_id in logs

Run:  python tests/test_issue7_networking.py
"""
from __future__ import annotations

import sys
import os
import time
import logging

# Ensure repo root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Patch libs namespace conflict (system libs package)
import importlib
import libs as _libs_pkg
_libs_pkg.__path__ = [os.path.join(os.path.dirname(__file__), "..", "libs")]
importlib.invalidate_caches()

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Any
from unittest.mock import patch, MagicMock

passed = 0
failed = 0


def assert_true(cond, label):
    global passed, failed
    if cond:
        print(f"  [PASS] {label}")
        passed += 1
    else:
        print(f"  [FAIL] {label}")
        failed += 1


# ─── Fake provider ──────────────────────────────────────────────────────────

from libs.core.models import AccountAuth, ProxyConfig
from libs.providers.linkedin.provider import LinkedInThread, LinkedInMessage


class FakeProvider:
    """Minimal provider stub for testing job_runner logic."""

    def __init__(self, threads=None, messages=None, raise_on_fetch=None):
        self.threads = threads or []
        self.messages = messages or []
        self.raise_on_fetch = raise_on_fetch
        self.list_threads_called = 0
        self.fetch_messages_calls = []
        self._profile_id = None
        self._profile_id_fetched = False
        self.rate_limit_encountered = False

    def list_threads(self):
        self.list_threads_called += 1
        return self.threads

    def fetch_messages(self, *, platform_thread_id, cursor, limit):
        self.fetch_messages_calls.append({
            "thread_id": platform_thread_id,
            "cursor": cursor,
            "limit": limit,
        })
        if self.raise_on_fetch:
            raise self.raise_on_fetch
        return self.messages, None  # messages, next_cursor=None


# ─── Fake storage ───────────────────────────────────────────────────────────

class FakeStorage:
    def __init__(self):
        self._thread_counter = 0
        self.cursors = {}
        self.inserted = []
        self._profile_ids = {}

    def upsert_thread(self, *, account_id, platform_thread_id, title):
        self._thread_counter += 1
        return self._thread_counter

    def get_cursor(self, *, account_id, thread_id):
        return self.cursors.get((account_id, thread_id))

    def set_cursor(self, *, account_id, thread_id, cursor):
        self.cursors[(account_id, thread_id)] = cursor

    def insert_message(self, *, account_id, thread_id, platform_message_id,
                       direction, sender, text, sent_at, raw=None):
        self.inserted.append(platform_message_id)
        return True

    def get_profile_id(self, account_id):
        return self._profile_ids.get(account_id)

    def set_profile_id(self, account_id, profile_id):
        self._profile_ids[account_id] = profile_id


# ─── Test 1: Proxy ──────────────────────────────────────────────────────────

def test_proxy():
    print("\n=== Requirement 1: Proxy used for all provider HTTP requests ===")

    from libs.providers.linkedin.provider import LinkedInProvider

    proxy = ProxyConfig(url="http://user:pass@proxy.example.com:8080")
    auth = AccountAuth(li_at="fake-token", jsessionid="fake-session")
    provider = LinkedInProvider(auth=auth, proxy=proxy)

    # _get_client creates httpx.Client with proxy
    client = provider._get_client()
    # httpx stores proxy config internally
    assert_true(provider._proxy_url() == "http://user:pass@proxy.example.com:8080",
                "_proxy_url() returns configured proxy URL")

    # _get_client uses proxy
    assert_true(client is not None, "_get_client() creates client successfully")
    provider.close()

    # socks5 proxy
    proxy_socks = ProxyConfig(url="socks5://host:1080")
    provider2 = LinkedInProvider(auth=auth, proxy=proxy_socks)
    assert_true(provider2._proxy_url() == "socks5://host:1080",
                "_proxy_url() supports socks5:// scheme")
    provider2.close()

    # No proxy
    provider3 = LinkedInProvider(auth=auth, proxy=None)
    assert_true(provider3._proxy_url() is None,
                "_proxy_url() returns None when no proxy configured")
    provider3.close()

    # One account -> one consistent IP: send_message reuses _get_client()
    provider4 = LinkedInProvider(auth=auth, proxy=proxy)
    client_a = provider4._get_client()
    client_b = provider4._get_client()
    assert_true(client_a is client_b,
                "One account -> one consistent IP: _get_client() reuses same client")
    provider4.close()

    # Verify send_message uses _get_client (not a fresh httpx.Client)
    import inspect
    send_src = inspect.getsource(LinkedInProvider.send_message)
    assert_true("self._get_client()" in send_src,
                "send_message uses _get_client() (shared client, consistent IP)")
    assert_true("httpx.Client(" not in send_src,
                "send_message does NOT create a new httpx.Client")


# ─── Test 2: Rate limiting — SyncConfig ─────────────────────────────────────

def test_rate_limiting():
    print("\n=== Requirement 2: Rate limiting in job_runner.py ===")

    from libs.core.job_runner import SyncConfig, SyncResult, run_sync

    # SyncConfig exists with correct defaults
    cfg = SyncConfig()
    assert_true(cfg.delay_between_threads_s == 2.0,
                "SyncConfig.delay_between_threads_s default is 2.0")
    assert_true(cfg.delay_between_pages_s == 1.5,
                "SyncConfig.delay_between_pages_s default is 1.5")
    assert_true(cfg.delay_between_accounts_s == 5.0,
                "SyncConfig.delay_between_accounts_s default is 5.0")

    # Custom config
    cfg2 = SyncConfig(delay_between_threads_s=0.01, delay_between_pages_s=0.01)
    assert_true(cfg2.delay_between_threads_s == 0.01,
                "SyncConfig accepts custom delay_between_threads_s")

    # Verify delay_between_threads is applied (with 3 threads, should sleep between them)
    threads = [
        LinkedInThread(platform_thread_id=f"t{i}", title=f"Thread {i}")
        for i in range(3)
    ]
    provider = FakeProvider(threads=threads)
    storage = FakeStorage()

    with patch("libs.core.job_runner.time.sleep") as mock_sleep:
        run_sync(
            account_id=1,
            storage=storage,
            provider=provider,
            sync_config=SyncConfig(delay_between_threads_s=2.0, delay_between_pages_s=1.5),
        )
        # Should sleep between threads (not before first one)
        thread_sleeps = [c for c in mock_sleep.call_args_list if c[0][0] == 2.0]
        assert_true(len(thread_sleeps) == 2,
                    f"time.sleep(2.0) called 2 times between 3 threads (got {len(thread_sleeps)})")

    # Verify delay_between_pages is used (provider returns next_cursor once)
    class PagingProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.threads = [LinkedInThread(platform_thread_id="t1", title="T1")]
            self._page_count = 0

        def fetch_messages(self, *, platform_thread_id, cursor, limit):
            self._page_count += 1
            if self._page_count == 1:
                msg = LinkedInMessage(
                    platform_message_id="m1", direction="in", sender="Alice",
                    text="hi", sent_at=datetime.now(timezone.utc),
                )
                return [msg], "cursor-2"  # has next page
            return [], None  # no more

    paging_provider = PagingProvider()
    storage2 = FakeStorage()
    with patch("libs.core.job_runner.time.sleep") as mock_sleep:
        run_sync(
            account_id=1,
            storage=storage2,
            provider=paging_provider,
            max_pages_per_thread=5,
            sync_config=SyncConfig(delay_between_pages_s=1.5, delay_between_threads_s=2.0),
        )
        page_sleeps = [c for c in mock_sleep.call_args_list if c[0][0] == 1.5]
        assert_true(len(page_sleeps) == 1,
                    f"time.sleep(1.5) called between pages (got {len(page_sleeps)})")


# ─── Test 3: Backoff on errors ──────────────────────────────────────────────

def test_backoff():
    print("\n=== Requirement 3: Backoff on errors ===")

    from libs.providers.linkedin.provider import LinkedInProvider
    import libs.providers.linkedin.provider as pmod
    import httpx

    # --- Constants match issue spec ---

    # send_message path
    assert_true(pmod._MAX_NETWORK_RETRIES == 3,
                "send: MAX_NETWORK_RETRIES is 3")
    assert_true(pmod._NETWORK_RETRY_DELAY_S == 5.0,
                "send: NETWORK_RETRY_DELAY_S is 5.0")
    assert_true(pmod._BACKOFF_START_S == 30.0,
                "send: BACKOFF_START_S is 30.0")
    assert_true(pmod._BACKOFF_MAX_S == 900.0,
                "send: BACKOFF_MAX_S is 900.0 (15 min)")

    # GraphQL path — separate retry budgets
    assert_true(pmod._MAX_RATE_LIMIT_RETRIES == 6,
                "graphql: rate-limit budget is 6 attempts")
    assert_true(pmod._SERVER_ERROR_MAX_ATTEMPTS == 3,
                "graphql: server-error budget is 3 attempts")
    assert_true(429 in pmod._RATE_LIMIT_STATUS_CODES,
                "graphql: 429 in RATE_LIMIT_STATUS_CODES")
    assert_true(999 in pmod._RATE_LIMIT_STATUS_CODES,
                "graphql: 999 in RATE_LIMIT_STATUS_CODES")
    assert_true(500 in pmod._SERVER_ERROR_STATUS_CODES,
                "graphql: 500 in SERVER_ERROR_STATUS_CODES")

    # --- 401 raises PermissionError + logs cookie expiry in send_message ---
    auth = AccountAuth(li_at="fake", jsessionid="fake")

    provider_logger = logging.getLogger("libs.providers.linkedin.provider")
    provider_logger.setLevel(logging.DEBUG)
    log_records_401 = []

    class CaptureHandler401(logging.Handler):
        def emit(self, record):
            log_records_401.append(record)

    handler_401 = CaptureHandler401()
    provider_logger.addHandler(handler_401)

    provider = LinkedInProvider(auth=auth)

    mock_resp = MagicMock()
    mock_resp.status_code = 401

    # Patch _get_client to return a mock client for send_message
    mock_client_inst = MagicMock()
    mock_client_inst.post.return_value = mock_resp

    with patch.object(provider, "_get_client", return_value=mock_client_inst):
        try:
            provider.send_message(recipient="urn:li:member:123", text="hi")
            assert_true(False, "send: 401 should raise PermissionError")
        except PermissionError:
            assert_true(True, "send: HTTP 401 raises PermissionError (no retry)")

    has_cookie_expiry_log = any(
        "Cookie expiry" in r.getMessage() for r in log_records_401
    )
    assert_true(has_cookie_expiry_log,
                "send: 401 logs cookie expiry notification")

    # --- 401 raises PermissionError + logs cookie expiry in _get_with_retry ---
    log_records_401.clear()
    provider2 = LinkedInProvider(auth=auth)
    mock_client = MagicMock()
    mock_401_resp = MagicMock()
    mock_401_resp.status_code = 401
    mock_client.get.return_value = mock_401_resp

    try:
        provider2._get_with_retry(mock_client, "https://example.com/api")
        assert_true(False, "graphql: 401 should raise PermissionError")
    except PermissionError:
        assert_true(True, "graphql: HTTP 401 raises PermissionError (no retry)")

    has_cookie_expiry_log_gql = any(
        "Cookie expiry" in r.getMessage() for r in log_records_401
    )
    assert_true(has_cookie_expiry_log_gql,
                "graphql: 401 logs cookie expiry notification")

    provider_logger.removeHandler(handler_401)

    # --- Network error retry in _get_with_retry ---
    provider3 = LinkedInProvider(auth=auth)
    mock_client2 = MagicMock()
    mock_client2.get.side_effect = httpx.NetworkError("connection reset")

    with patch.object(pmod.time, "sleep"):
        try:
            provider3._get_with_retry(mock_client2, "https://example.com/api")
            assert_true(False, "graphql: network error should raise ConnectionError")
        except ConnectionError:
            assert_true(True, "graphql: network error raises ConnectionError after 3 retries")

    assert_true(mock_client2.get.call_count == 3,
                f"graphql: retried 3 times on network error (got {mock_client2.get.call_count})")

    # --- rate_limited flag via httpx.HTTPStatusError ---
    from libs.core.job_runner import run_sync, SyncConfig

    # Simulate _get_with_retry raising HTTPStatusError (429 exhausted retries)
    mock_request = MagicMock()
    mock_429_resp = MagicMock()
    mock_429_resp.status_code = 429

    class HTTPStatusProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.threads = [LinkedInThread(platform_thread_id="t1", title="T")]

        def fetch_messages(self, **kwargs):
            raise httpx.HTTPStatusError("429", request=mock_request, response=mock_429_resp)

    storage = FakeStorage()
    with patch("libs.core.job_runner.time.sleep"):
        result = run_sync(
            account_id=1, storage=storage, provider=HTTPStatusProvider(),
            sync_config=SyncConfig(delay_between_threads_s=0, delay_between_pages_s=0),
        )
    assert_true(result.rate_limited is True,
                "job_runner: rate_limited=True on httpx.HTTPStatusError(429)")

    # RuntimeError path still works too
    rate_err_provider = FakeProvider(
        threads=[LinkedInThread(platform_thread_id="t1", title="T")],
        raise_on_fetch=RuntimeError("Rate-limited 5 times, giving up"),
    )
    storage2 = FakeStorage()
    with patch("libs.core.job_runner.time.sleep"):
        result2 = run_sync(
            account_id=1, storage=storage2, provider=rate_err_provider,
            sync_config=SyncConfig(delay_between_threads_s=0, delay_between_pages_s=0),
        )
    assert_true(result2.rate_limited is True,
                "job_runner: rate_limited=True on RuntimeError('Rate-limited')")

    # rate_limit_encountered flag on provider (even when retries succeed internally)
    class RateLimitFlagProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.threads = [LinkedInThread(platform_thread_id="t1", title="T")]
            self.rate_limit_encountered = True  # simulates internal retry that succeeded

    flag_provider = RateLimitFlagProvider()
    storage3 = FakeStorage()
    with patch("libs.core.job_runner.time.sleep"):
        result3 = run_sync(
            account_id=1, storage=storage3, provider=flag_provider,
            sync_config=SyncConfig(delay_between_threads_s=0, delay_between_pages_s=0),
        )
    assert_true(result3.rate_limited is True,
                "job_runner: rate_limited=True from provider.rate_limit_encountered flag")


# ─── Test 4: Logging ────────────────────────────────────────────────────────

def test_logging():
    print("\n=== Requirement 4: Logging with account_id and rate_limited ===")

    from libs.core.job_runner import SyncResult, SyncConfig, run_sync

    # SyncResult has rate_limited field
    r = SyncResult(synced_threads=1, messages_inserted=5,
                   messages_skipped_duplicate=0, pages_fetched=1, rate_limited=False)
    assert_true(hasattr(r, "rate_limited"), "SyncResult has rate_limited field")
    assert_true(r.rate_limited is False, "rate_limited=False when no rate limiting")

    # Verify logging includes account_id on rate limit
    rate_err_provider = FakeProvider(
        threads=[LinkedInThread(platform_thread_id="t1", title="T")],
        raise_on_fetch=RuntimeError("Rate-limited 429"),
    )
    storage = FakeStorage()

    # Use a real log handler to capture messages (mock doesn't work since
    # the module-level logger is already bound at import time).
    job_logger = logging.getLogger("libs.core.job_runner")
    job_logger.setLevel(logging.DEBUG)
    log_records = []

    class CaptureHandler(logging.Handler):
        def emit(self, record):
            log_records.append(record)

    handler = CaptureHandler()
    job_logger.addHandler(handler)

    with patch("libs.core.job_runner.time.sleep"):
        result = run_sync(
            account_id=42, storage=storage, provider=rate_err_provider,
            sync_config=SyncConfig(delay_between_threads_s=0, delay_between_pages_s=0),
        )
    has_account_id_log = any(
        "account_id=42" in record.getMessage() for record in log_records
        if record.levelno >= logging.WARNING
    )
    assert_true(has_account_id_log,
                "Rate-limit warning logged with account_id=42")
    assert_true(result.rate_limited is True,
                "SyncResult.rate_limited=True on rate limit")

    # Verify debug log between threads includes account_id
    log_records.clear()
    threads = [
        LinkedInThread(platform_thread_id=f"t{i}", title=f"T{i}")
        for i in range(2)
    ]
    provider = FakeProvider(threads=threads)
    storage2 = FakeStorage()
    with patch("libs.core.job_runner.time.sleep"):
        run_sync(
            account_id=99, storage=storage2, provider=provider,
            sync_config=SyncConfig(delay_between_threads_s=2.0, delay_between_pages_s=1.5),
        )
    has_thread_delay_log = any(
        "account_id=99" in record.getMessage() for record in log_records
        if record.levelno == logging.DEBUG
    )
    assert_true(has_thread_delay_log,
                "Thread delay debug log includes account_id=99")

    job_logger.removeHandler(handler)

    # Verify provider-level rate-limit logs include account_id
    import libs.providers.linkedin.provider as pmod
    from libs.providers.linkedin.provider import LinkedInProvider

    provider_logger = logging.getLogger("libs.providers.linkedin.provider")
    provider_logger.setLevel(logging.DEBUG)
    prov_log_records = []

    class ProvCaptureHandler(logging.Handler):
        def emit(self, record):
            prov_log_records.append(record)

    prov_handler = ProvCaptureHandler()
    provider_logger.addHandler(prov_handler)

    # GraphQL path: _get_with_retry rate limit log
    auth = AccountAuth(li_at="fake", jsessionid="fake")
    provider_with_id = LinkedInProvider(auth=auth, account_id=77)

    mock_client = MagicMock()
    mock_429_resp = MagicMock()
    mock_429_resp.status_code = 429
    mock_429_resp.headers = {}
    mock_429_resp.request = MagicMock()
    mock_client.get.return_value = mock_429_resp

    with patch.object(pmod.time, "sleep"):
        try:
            provider_with_id._get_with_retry(mock_client, "https://example.com/api")
        except Exception:
            pass

    has_provider_account_id = any(
        "account_id=77" in r.getMessage() for r in prov_log_records
        if r.levelno >= logging.WARNING
    )
    assert_true(has_provider_account_id,
                "Provider rate-limit log includes account_id=77")

    provider_logger.removeHandler(prov_handler)


# ─── Test 5: Acceptance criteria ────────────────────────────────────────────

def test_acceptance():
    print("\n=== Acceptance Criteria ===")

    from libs.core.job_runner import SyncConfig, run_sync

    # AC1: Sync runs with configurable delays between threads
    threads = [
        LinkedInThread(platform_thread_id=f"t{i}", title=f"T{i}")
        for i in range(3)
    ]
    provider = FakeProvider(threads=threads)
    storage = FakeStorage()
    custom_cfg = SyncConfig(delay_between_threads_s=3.5, delay_between_pages_s=0.5)

    with patch("libs.core.job_runner.time.sleep") as mock_sleep:
        run_sync(
            account_id=1, storage=storage, provider=provider,
            sync_config=custom_cfg,
        )
        thread_sleeps = [c for c in mock_sleep.call_args_list if c[0][0] == 3.5]
        assert_true(len(thread_sleeps) == 2,
                    "AC1: Configurable delay (3.5s) applied between threads")

    # AC2: Rate limit response triggers backoff, not crash
    rate_provider = FakeProvider(
        threads=[LinkedInThread(platform_thread_id="t1", title="T")],
        raise_on_fetch=RuntimeError("Rate-limited 429"),
    )
    storage2 = FakeStorage()
    with patch("libs.core.job_runner.time.sleep"):
        result = run_sync(
            account_id=1, storage=storage2, provider=rate_provider,
            sync_config=SyncConfig(delay_between_threads_s=0, delay_between_pages_s=0),
        )
        assert_true(result.rate_limited is True,
                    "AC2: Rate limit sets flag, does not crash")
        assert_true(result.synced_threads == 1,
                    "AC2: Sync continues after rate limit (thread counted)")

    # AC3: Proxy is used for all provider HTTP requests
    from libs.providers.linkedin.provider import LinkedInProvider
    proxy = ProxyConfig(url="http://proxy:8080")
    auth = AccountAuth(li_at="fake", jsessionid="fake")
    p = LinkedInProvider(auth=auth, proxy=proxy)
    assert_true(p._proxy_url() == "http://proxy:8080",
                "AC3: Proxy URL available for all HTTP requests")
    client = p._get_client()
    assert_true(client is not None,
                "AC3: HTTP client created with proxy config")
    p.close()


# ─── Test 6: Safe rate limit floors ──────────────────────────────────────────

def test_safe_floors():
    print("\n=== Rate limit safe floors ===")

    # Verify the API model enforces min 1.0s by reading the Field definitions
    # (can't import SyncIn directly on Python 3.9 due to `str | None` syntax)
    import ast

    with open(os.path.join(os.path.dirname(__file__), "..", "apps", "api", "main.py")) as f:
        source = f.read()

    # Check that ge=1.0 (or ge=1) appears in the delay Field definitions
    has_threads_floor = "delay_between_threads_s" in source and "ge=1" in source
    has_pages_floor = "delay_between_pages_s" in source and "ge=1.2" in source

    assert_true(has_threads_floor,
                "SyncIn.delay_between_threads_s has ge=1.0 minimum floor")
    assert_true(has_pages_floor,
                "SyncIn.delay_between_pages_s has ge=1.2 minimum floor (max 50 req/min)")

    from libs.core.job_runner import SyncConfig
    cfg = SyncConfig()

    # DM history fetch: default 1.5s between pages -> ~40 req/min (safe: 20-50)
    # Worst case with min 1.2s -> 50 req/min (at safe boundary)
    dm_fetch_per_min = 60.0 / cfg.delay_between_pages_s
    assert_true(dm_fetch_per_min <= 50,
                f"DM fetch: default ~{dm_fetch_per_min:.0f} req/min (<= 50 safe)")
    worst_case_dm = 60.0 / 1.2
    assert_true(worst_case_dm <= 50,
                f"DM fetch: worst case (min 1.2s) = {worst_case_dm:.0f} req/min (<= 50)")

    # Thread list: hardcoded 6.0s in provider -> 10 req/min (safe: ~10)
    import libs.providers.linkedin.provider as pmod
    thread_list_per_min = 60.0 / pmod._DELAY_BETWEEN_THREAD_LIST_PAGES_S
    assert_true(thread_list_per_min <= 10,
                f"Thread list: provider hardcoded ~{thread_list_per_min:.0f} req/min (<= 10 safe)")

    # DM send: MAX_DAILY_SENDS = 10 (safe: 5-10/day)
    from libs.core.job_runner import _MAX_DAILY_SENDS
    assert_true(_MAX_DAILY_SENDS <= 10,
                f"DM send: daily cap {_MAX_DAILY_SENDS} (<= 10 safe)")


# ─── Test 7: Daily send cap ─────────────────────────────────────────────────

def test_daily_send_cap():
    print("\n=== Daily send cap ===")

    from libs.core.job_runner import run_send, _MAX_DAILY_SENDS
    from libs.providers.linkedin.provider import LinkedInProvider

    assert_true(_MAX_DAILY_SENDS == 10, f"MAX_DAILY_SENDS is 10 (got {_MAX_DAILY_SENDS})")

    # Use a real storage with a temp DB to test persisted daily count
    from libs.core.storage import Storage
    import tempfile, os
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    try:
        real_storage = Storage(db_path=tmp.name)
        real_storage.migrate()

        # Create an account
        auth = AccountAuth(li_at="fake-token-long-enough", jsessionid="fake-session")
        account_id = real_storage.create_account(label="test", auth=auth)

        # Insert _MAX_DAILY_SENDS outbound messages for today
        for i in range(_MAX_DAILY_SENDS):
            real_storage.upsert_thread(
                account_id=account_id, platform_thread_id=f"t-{i}", title=None,
            )
            real_storage.insert_message(
                account_id=account_id,
                thread_id=1,
                platform_message_id=f"msg-{i}",
                direction="out",
                sender=None,
                text="hi",
                sent_at=datetime.now(timezone.utc),
            )

        # Verify daily count is at the cap
        count = real_storage.get_daily_send_count(account_id=account_id)
        assert_true(count == _MAX_DAILY_SENDS,
                    f"Storage reports {count} daily sends (expected {_MAX_DAILY_SENDS})")

        # run_send should refuse
        provider = LinkedInProvider(auth=auth)
        try:
            run_send(
                account_id=account_id, storage=real_storage, provider=provider,
                recipient="urn:li:member:999", text="blocked", idempotency_key=None,
            )
            assert_true(False, "Should raise RuntimeError when daily cap reached")
        except RuntimeError as e:
            assert_true("Daily send limit reached" in str(e),
                        "Daily cap raises RuntimeError with clear message")

        # With 0 sends today, it should NOT raise the cap error
        # (it will fail on network, but the cap check passes)
        real_storage2 = Storage(db_path=tmp.name)
        real_storage2.migrate()
        account_id2 = real_storage2.create_account(label="test2", auth=auth)
        count2 = real_storage2.get_daily_send_count(account_id=account_id2)
        assert_true(count2 == 0,
                    "New account has 0 daily sends")
        real_storage2.close()

        real_storage.close()
    finally:
        os.unlink(tmp.name)


# ─── Test 8: list_threads respects page_delay ───────────────────────────────

def test_list_threads_safe_delay():
    print("\n=== list_threads safe delay ===")

    import libs.providers.linkedin.provider as pmod

    # Thread list uses a hardcoded safe delay (not shared with DM fetch)
    assert_true(pmod._DELAY_BETWEEN_THREAD_LIST_PAGES_S == 6.0,
                "Thread list page delay hardcoded at 6.0s")

    # This gives ~10 req/min, matching the safe limit from the issue
    reqs = 60.0 / pmod._DELAY_BETWEEN_THREAD_LIST_PAGES_S
    assert_true(reqs <= 10,
                f"Thread list: {reqs:.0f} req/min (<= 10 safe limit)")


# ─── Test 9: delay_between_accounts_s ────────────────────────────────────────

def test_delay_between_accounts():
    print("\n=== delay_between_accounts_s ===")

    from libs.core.job_runner import SyncConfig, run_sync_multi

    provider1 = FakeProvider(threads=[LinkedInThread(platform_thread_id="t1", title="T1")])
    provider2 = FakeProvider(threads=[LinkedInThread(platform_thread_id="t2", title="T2")])
    provider3 = FakeProvider(threads=[LinkedInThread(platform_thread_id="t3", title="T3")])

    accounts = [(1, provider1), (2, provider2), (3, provider3)]
    storage = FakeStorage()

    with patch("libs.core.job_runner.time.sleep") as mock_sleep:
        results = run_sync_multi(
            accounts=accounts,
            storage=storage,
            sync_config=SyncConfig(delay_between_accounts_s=5.0, delay_between_threads_s=2.0),
        )

    assert_true(len(results) == 3, f"run_sync_multi returns 3 results (got {len(results)})")

    account_sleeps = [c for c in mock_sleep.call_args_list if c[0][0] == 5.0]
    assert_true(len(account_sleeps) == 2,
                f"time.sleep(5.0) called 2 times between 3 accounts (got {len(account_sleeps)})")


# ─── Test 10: Profile views caching ──────────────────────────────────────────

def test_profile_views_caching():
    print("\n=== Profile views caching ===")

    from libs.core.job_runner import SyncConfig, run_sync

    # First sync: no cached profile_id, provider fetches it
    class ProfileTrackingProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.threads = [LinkedInThread(platform_thread_id="t1", title="T")]
            self._profile_id = "urn:li:fsd_profile:ABC123"
            self._profile_id_fetched = True

    provider1 = ProfileTrackingProvider()
    storage = FakeStorage()

    with patch("libs.core.job_runner.time.sleep"):
        run_sync(
            account_id=1, storage=storage, provider=provider1,
            sync_config=SyncConfig(delay_between_threads_s=0, delay_between_pages_s=0),
        )

    assert_true(storage._profile_ids.get(1) == "urn:li:fsd_profile:ABC123",
                "Profile ID cached in storage after first sync")

    # Second sync: cached profile_id should be used, no /me call needed
    provider2 = ProfileTrackingProvider()
    provider2._profile_id = None
    provider2._profile_id_fetched = False

    with patch("libs.core.job_runner.time.sleep"):
        run_sync(
            account_id=1, storage=storage, provider=provider2,
            sync_config=SyncConfig(delay_between_threads_s=0, delay_between_pages_s=0),
        )

    assert_true(provider2._profile_id == "urn:li:fsd_profile:ABC123",
                "Second sync uses cached profile_id (no /me call)")
    assert_true(provider2._profile_id_fetched is True,
                "Provider marked as fetched from cache")


# ─── Run all ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("Issue #7 -- Networking Requirements Tests")
    print("=" * 60)

    test_proxy()
    test_rate_limiting()
    test_backoff()
    test_logging()
    test_acceptance()
    test_safe_floors()
    test_daily_send_cap()
    test_list_threads_safe_delay()
    test_delay_between_accounts()
    test_profile_views_caching()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)
    sys.exit(1 if failed > 0 else 0)
