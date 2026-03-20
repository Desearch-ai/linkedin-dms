"""Job runner: sync and send orchestration for LinkedIn DMs.

Reusable by the API and future CLI. Aligned to provider and storage stubs.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from libs.core.storage import Storage
from libs.providers.linkedin.provider import LinkedInProvider

_DELAY_BETWEEN_PAGES_S = 1.5


def _normalize_sent_at(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass(frozen=True)
class SyncResult:
    synced_threads: int
    messages_inserted: int
    messages_skipped_duplicate: int
    pages_fetched: int


def run_sync(
    account_id: int,
    storage: Storage,
    provider: LinkedInProvider,
    limit_per_thread: int = 50,
    max_pages_per_thread: int | None = 1,
) -> SyncResult:
    """Sync threads and messages from provider into storage.

    Args:
        account_id: Account to sync.
        storage: Storage instance.
        provider: LinkedIn provider (list_threads, fetch_messages).
        limit_per_thread: Max messages per fetch_messages call.
        max_pages_per_thread: Max pages per thread (1 = MVP one page). None = exhaust cursor.

    Returns:
        SyncResult with counts. Duplicates are skipped and counted separately.
    """
    threads = provider.list_threads()
    synced_threads = 0
    messages_inserted = 0
    messages_skipped = 0
    pages_fetched = 0
    for t in threads:
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
            msgs, next_cursor = provider.fetch_messages(
                platform_thread_id=t.platform_thread_id,
                cursor=cursor,
                limit=limit_per_thread,
            )
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
            time.sleep(_DELAY_BETWEEN_PAGES_S)
        synced_threads += 1
    return SyncResult(
        synced_threads=synced_threads,
        messages_inserted=messages_inserted,
        messages_skipped_duplicate=messages_skipped,
        pages_fetched=pages_fetched,
    )


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
