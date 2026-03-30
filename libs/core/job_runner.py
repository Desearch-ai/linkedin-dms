"""Job runner: sync and send orchestration for LinkedIn DMs.

Reusable by the API and future CLI. Aligned to provider and storage stubs.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from libs.core.storage import Storage
from libs.providers.linkedin.provider import LinkedInProvider

logger = logging.getLogger(__name__)


def _normalize_sent_at(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass(frozen=True)
class SyncConfig:
    delay_between_threads_s: float = 2.0  # pause between threads
    delay_between_pages_s: float = 1.5    # pause between fetch_messages pages
    delay_between_accounts_s: float = 5.0 # if running multiple accounts


@dataclass(frozen=True)
class SyncResult:
    synced_threads: int
    messages_inserted: int
    messages_skipped_duplicate: int
    pages_fetched: int
    rate_limited: bool


def run_sync(
    account_id: int,
    storage: Storage,
    provider: LinkedInProvider,
    limit_per_thread: int = 50,
    max_pages_per_thread: int | None = 1,
    sync_config: SyncConfig | None = None,
) -> SyncResult:
    """Sync threads and messages from provider into storage.

    Args:
        account_id: Account to sync.
        storage: Storage instance.
        provider: LinkedIn provider (list_threads, fetch_messages).
        limit_per_thread: Max messages per fetch_messages call.
        max_pages_per_thread: Max pages per thread (1 = MVP one page). None = exhaust cursor.
        sync_config: Optional delay configuration. Uses defaults if None.

    Returns:
        SyncResult with counts. Duplicates are skipped and counted separately.
    """
    cfg = sync_config or SyncConfig()

    # Use cached profile_id from storage to avoid hitting /me every sync.
    # Only fetch from LinkedIn if not yet cached, then persist.
    try:
        cached_profile_id = storage.get_profile_id(account_id)
    except KeyError:
        cached_profile_id = None
    if cached_profile_id:
        provider._profile_id = cached_profile_id
        provider._profile_id_fetched = True

    threads = provider.list_threads()

    # Cache profile_id after first successful fetch
    if not cached_profile_id and provider._profile_id:
        storage.set_profile_id(account_id, provider._profile_id)

    synced_threads = 0
    messages_inserted = 0
    messages_skipped = 0
    pages_fetched = 0
    rate_limited = False
    for idx, t in enumerate(threads):
        if idx > 0:
            logger.debug(
                "account_id=%d: sleeping %.1fs between threads",
                account_id, cfg.delay_between_threads_s,
            )
            time.sleep(cfg.delay_between_threads_s)
        thread_id = storage.upsert_thread(
            account_id=account_id,
            platform_thread_id=t.platform_thread_id,
            title=t.title,
        )
        pages_this_thread = 0
        cursor = storage.get_cursor(account_id=account_id, thread_id=thread_id)
        while True:
            if max_pages_per_thread is not None and pages_this_thread >= max_pages_per_thread:
                break
            try:
                msgs, next_cursor = provider.fetch_messages(
                    platform_thread_id=t.platform_thread_id,
                    cursor=cursor,
                    limit=limit_per_thread,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (429, 999):
                    logger.warning(
                        "account_id=%d: rate-limited (HTTP %d) during fetch_messages for thread %s",
                        account_id, exc.response.status_code, t.platform_thread_id,
                    )
                    rate_limited = True
                    break
                raise
            except RuntimeError as exc:
                if "Rate-limited" in str(exc) or "429" in str(exc):
                    logger.warning(
                        "account_id=%d: rate-limited during fetch_messages for thread %s",
                        account_id, t.platform_thread_id,
                    )
                    rate_limited = True
                    break
                raise
            pages_fetched += 1
            pages_this_thread += 1
            for m in msgs:
                inserted = storage.insert_message(
                    account_id=account_id,
                    thread_id=thread_id,
                    platform_message_id=m.platform_message_id,
                    direction=m.direction,
                    sender=m.sender,
                    text=m.text,
                    sent_at=_normalize_sent_at(m.sent_at),
                    raw=m.raw,
                )
                if inserted:
                    messages_inserted += 1
                else:
                    messages_skipped += 1
            storage.set_cursor(account_id=account_id, thread_id=thread_id, cursor=next_cursor)
            if next_cursor is None:
                break
            cursor = next_cursor
            time.sleep(cfg.delay_between_pages_s)
        synced_threads += 1
    # Detect rate limiting from both exception catches and provider-internal retries
    if provider.rate_limit_encountered:
        rate_limited = True
        logger.warning(
            "account_id=%d: rate-limit encountered during sync", account_id,
        )
    return SyncResult(
        synced_threads=synced_threads,
        messages_inserted=messages_inserted,
        messages_skipped_duplicate=messages_skipped,
        pages_fetched=pages_fetched,
        rate_limited=rate_limited,
    )


def run_sync_multi(
    accounts: list[tuple[int, LinkedInProvider]],
    storage: Storage,
    limit_per_thread: int = 50,
    max_pages_per_thread: int | None = 1,
    sync_config: SyncConfig | None = None,
) -> list[SyncResult]:
    """Sync multiple accounts with configurable delay between them.

    Args:
        accounts: List of (account_id, provider) pairs.
        storage: Storage instance.
        limit_per_thread: Max messages per fetch_messages call.
        max_pages_per_thread: Max pages per thread.
        sync_config: Delay configuration.

    Returns:
        List of SyncResult, one per account.
    """
    cfg = sync_config or SyncConfig()
    results: list[SyncResult] = []
    for idx, (account_id, provider) in enumerate(accounts):
        if idx > 0:
            logger.debug(
                "sleeping %.1fs between accounts (after account_id=%d)",
                cfg.delay_between_accounts_s, accounts[idx - 1][0],
            )
            time.sleep(cfg.delay_between_accounts_s)
        result = run_sync(
            account_id=account_id,
            storage=storage,
            provider=provider,
            limit_per_thread=limit_per_thread,
            max_pages_per_thread=max_pages_per_thread,
            sync_config=cfg,
        )
        results.append(result)
    return results


_MAX_DAILY_SENDS = 10  # safe limit per issue #7: 5-10/day, >20 = spam flag


def run_send(
    account_id: int,
    storage: Storage,
    provider: LinkedInProvider,
    recipient: str,
    text: str,
    idempotency_key: str | None,
) -> str:
    """Send one message via provider. Returns platform_message_id.

    Persists the outbound message in storage for local archive (thread keyed by recipient).
    """
    daily_count = storage.get_daily_send_count(account_id=account_id)
    if daily_count >= _MAX_DAILY_SENDS:
        raise RuntimeError(
            f"Daily send limit reached ({_MAX_DAILY_SENDS} messages/day) "
            f"for account {account_id}. Retry tomorrow to avoid spam flags."
        )

    platform_message_id = provider.send_message(
        recipient=recipient,
        text=text,
        idempotency_key=idempotency_key,
    )
    thread_id = storage.upsert_thread(
        account_id=account_id,
        platform_thread_id=recipient,
        title=None,
    )
    storage.insert_message(
        account_id=account_id,
        thread_id=thread_id,
        platform_message_id=platform_message_id,
        direction="out",
        sender=None,
        text=text,
        sent_at=datetime.now(timezone.utc),
        raw=None,
    )
    return platform_message_id
