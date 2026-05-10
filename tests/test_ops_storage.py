"""Tests for the local Ops Console storage query layer."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from libs.core import crypto
from libs.core.models import AccountAuth, BrowserContext
from libs.core.storage import Storage


@pytest.fixture(autouse=True)
def _storage_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DESEARCH_DB_PATH", str(tmp_path / "ops.sqlite"))
    monkeypatch.delenv("DESEARCH_ENCRYPTION_KEY", raising=False)
    crypto._warned_no_key = False


@pytest.fixture
def storage(tmp_path):
    s = Storage(db_path=tmp_path / "ops.sqlite")
    s.migrate()
    yield s
    s.close()


def _populate(storage: Storage) -> tuple[int, int, int]:
    aid = storage.create_account(label="ops", auth=AccountAuth(li_at="AQEDsecret", jsessionid="ajax:csrf"))
    storage.update_browser_context(aid, BrowserContext(x_li_track='{"safe":true}', csrf_token="ajax:ctx"))
    t1 = storage.upsert_thread(account_id=aid, platform_thread_id="urn:thread:1", title="Ada")
    t2 = storage.upsert_thread(account_id=aid, platform_thread_id="urn:thread:2", title="Grace")
    storage.insert_message(
        account_id=aid,
        thread_id=t1,
        platform_message_id="m1",
        direction="in",
        sender="Ada",
        text="Bittensor launch details li_at=AQED_DO_NOT_LEAK csrf_token=ajax:DO_NOT_LEAK",
        sent_at=datetime(2026, 5, 8, 9, 30, tzinfo=timezone.utc),
        raw={"x": 1},
    )
    storage.insert_message(
        account_id=aid,
        thread_id=t1,
        platform_message_id="m2",
        direction="out",
        sender=None,
        text="Thanks, this is helpful.",
        sent_at=datetime(2026, 5, 10, 11, 55, tzinfo=timezone.utc),
        raw=None,
    )
    storage.insert_message(
        account_id=aid,
        thread_id=t2,
        platform_message_id="m3",
        direction="in",
        sender="Grace",
        text="Another conversation",
        sent_at=datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc),
        raw=None,
    )
    return aid, t1, t2


def test_migrate_adds_ops_tables_and_keeps_current_version(storage):
    tables = {r[0] for r in storage._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"draft_replies", "send_approvals", "campaigns", "campaign_recipients", "ops_audit_events"} <= tables
    assert storage._get_schema_version() >= 5


def test_account_ops_health_is_safe_and_counts_empty_db(storage):
    aid = storage.create_account(label="ops", auth=AccountAuth(li_at="AQEDsecret", jsessionid=None))
    health = storage.get_account_ops_health(aid)
    assert health["account_id"] == aid
    assert health["session"] == {"has_li_at": True, "has_jsessionid": False, "has_browser_context": False, "expires_at": None}
    assert health["counts"]["threads"] == 0
    assert "AQEDsecret" not in str(health)


def test_inbox_thread_messages_search_and_pagination_are_redacted(storage):
    aid, t1, _ = _populate(storage)

    page1 = storage.list_inbox_threads(account_id=aid, limit=1, cursor=None)
    assert len(page1["threads"]) == 1
    assert page1["page"]["next_cursor"] == "1"
    assert page1["threads"][0]["last_message_preview"] == "Thanks, this is helpful."

    page2 = storage.list_inbox_threads(account_id=aid, limit=1, cursor=page1["page"]["next_cursor"])
    assert len(page2["threads"]) == 1

    messages = storage.list_thread_messages(account_id=aid, thread_id=t1, limit=10, cursor=None)
    assert messages["messages"][0]["raw_available"] is True
    joined = str(messages)
    assert "AQED_DO_NOT_LEAK" not in joined
    assert "ajax:DO_NOT_LEAK" not in joined
    assert "[REDACTED]" in joined

    search = storage.search_messages(account_id=aid, query="bittensor", direction="in", limit=10, cursor=None)
    assert search["fts"] is False
    assert search["results"][0]["thread_id"] == t1
    assert "Bittensor" in search["results"][0]["text_snippet"]
    assert "AQED_DO_NOT_LEAK" not in str(search)


def test_ops_helpers_raise_for_unknown_account_and_thread(storage):
    with pytest.raises(KeyError):
        storage.get_account_ops_health(999)
    with pytest.raises(KeyError):
        storage.list_inbox_threads(account_id=999, limit=25, cursor=None)

    aid = storage.create_account(label="ops", auth=AccountAuth(li_at="AQEDsecret"))
    with pytest.raises(KeyError):
        storage.get_thread_detail(account_id=aid, thread_id=999)


def test_draft_approval_audit_helpers(storage):
    aid, t1, _ = _populate(storage)
    draft = storage.create_draft_reply(
        account_id=aid,
        thread_id=t1,
        recipient="urn:li:member:1",
        text="hello token=DO_NOT_LEAK",
        campaign_id=None,
        idempotency_key="idem-1",
    )
    assert draft["approval_state"] == "draft"
    assert draft["external_writes"] == 0
    assert "DO_NOT_LEAK" not in draft["preview"]

    approval = storage.approve_send_approval(draft["approval_id"], approved_by="tester")
    assert approval["state"] == "approved"
    listed = storage.list_approvals(account_id=aid, state="approved", limit=10, cursor=None)
    assert listed["approvals"][0]["approval_id"] == draft["approval_id"]

    storage.record_ops_audit_event(
        account_id=aid,
        event_type="sync.ingest",
        actor="test",
        entity_type="sync",
        entity_id=None,
        payload={"li_at": "AQED_DO_NOT_LEAK", "messages_inserted": 3},
    )
    audit = storage.list_ops_audit(account_id=aid, limit=20, cursor=None)
    assert any(e["event_type"] == "sync.ingest" for e in audit["events"])
    assert "AQED_DO_NOT_LEAK" not in str(audit)
