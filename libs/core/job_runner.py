"""Job runner: sync and send orchestration for LinkedIn DMs.

Reusable by the API and future CLI. Aligned to provider and storage stubs.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from libs.core.storage import Storage
from libs.providers.linkedin.provider import LinkedInProvider

logger = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class SendResult:
    """Outcome of a run_send call with durable status tracking."""
    send_id: int
    platform_message_id: Optional[str]
    status: str  # "sent" | "failed" | "pending"
    was_duplicate: bool


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
) -> SendResult:
    """Send one message via provider with durable idempotency.

    Creates (or retrieves) a persistent outbound send record *before*
    calling the provider.  If the same ``idempotency_key`` was already
    used and the send succeeded, the cached result is returned without
    contacting LinkedIn again.  Failed records are retried; pending
    records raise ``RuntimeError`` to prevent concurrent duplicate sends.
    Reusing a key with different recipient/text raises ``ValueError``.

    On success the outbound message is also archived in the ``messages``
    table (existing behavior).
    """
    send_id, existing = storage.create_or_get_outbound_send(
        account_id=account_id,
        idempotency_key=idempotency_key,
        recipient=recipient,
        text=text,
    )

    if existing is not None:
        if existing["recipient"] != recipient or existing["text"] != text:
            raise ValueError(
                f"Idempotency key {idempotency_key!r} already used with different "
                f"recipient/text. Use a new key for a different message."
            )
        if existing["status"] == "sent":
            logger.info(
                "Idempotency hit (send_id=%d, key=%s) — returning cached result",
                send_id,
                idempotency_key,
            )
            return SendResult(
                send_id=send_id,
                platform_message_id=existing["platform_message_id"],
                status="sent",
                was_duplicate=True,
            )
        if existing["status"] == "pending":
            raise RuntimeError(
                f"Send {send_id} is already in progress (status=pending). "
                f"Retry after it completes or fails."
            )
        logger.info(
            "Retrying send_id=%d (status=%s, attempts=%d)",
            send_id,
            existing["status"],
            existing["attempts"],
        )

    try:
        platform_message_id = provider.send_message(
            recipient=recipient,
            text=text,
        )
    except Exception as exc:
        storage.mark_outbound_failed(send_id=send_id, error=str(exc))
        raise

    storage.mark_outbound_sent(
        send_id=send_id,
        platform_message_id=platform_message_id,
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
    return SendResult(
        send_id=send_id,
        platform_message_id=platform_message_id,
        status="sent",
        was_duplicate=False,
    )
