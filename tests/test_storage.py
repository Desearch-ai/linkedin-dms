"""Tests for libs.core.storage — schema, migrations, and CRUD."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from libs.core import crypto
from libs.core.models import AccountAuth
from libs.core.storage import Storage


@pytest.fixture(autouse=True)
def _storage_env(monkeypatch, tmp_path):
    """Use temp DB and plaintext storage (no encryption) for storage tests."""
    monkeypatch.setenv("DESEARCH_DB_PATH", str(tmp_path / "storage.sqlite"))
    monkeypatch.delenv("DESEARCH_ENCRYPTION_KEY", raising=False)
    crypto._warned_no_key = False


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "storage.sqlite"


@pytest.fixture
def storage(db_path):
    s = Storage(db_path=db_path)
    s.migrate()
    yield s
    s.close()


def test_migrate_creates_tables_and_indexes(storage):
    """Regression: after migrate(), all four tables and indexes exist."""
    conn = sqlite3.connect(storage.db_path)
    cur = conn.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'index') ORDER BY type, name"
    )
    rows = cur.fetchall()
    conn.close()
    tables = {r[0] for r in rows if r[1] == "table"}
    indexes = {r[0] for r in rows if r[1] == "index"}
    assert tables >= {"accounts", "threads", "messages", "sync_cursors", "schema_version"}
    assert "idx_threads_account_id" in indexes
    assert "idx_messages_thread_id" in indexes
    assert "idx_messages_account_id" in indexes


def test_schema_version_exists_after_migrate(storage):
    """Edge case: existing DB without schema_version gets version 0 then migrations run."""
    v = storage._get_schema_version()
    assert v >= 0


def test_schema_version_is_current_after_migrate(storage):
    """Regression: migrate() leaves schema at current version (3 = outbound_sends)."""
    assert storage._get_schema_version() == 3


def test_migrate_idempotent(storage):
    """Edge case: calling migrate() twice does not fail and version does not regress."""
    v1 = storage._get_schema_version()
    storage.migrate()
    v2 = storage._get_schema_version()
    assert v2 == v1


def test_migrate_upgrades_preexisting_baseline_db(tmp_path):
    """Regression: a DB created by the original main-branch storage.py (no schema_version,
    no indexes, no CHECK) is upgraded correctly by migrate()."""
    db_path = tmp_path / "legacy.sqlite"
    # Simulate the original main-branch schema (no schema_version, no indexes, no CHECK).
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE accounts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL,
          auth_json TEXT NOT NULL, proxy_json TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE threads (
          id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
          platform_thread_id TEXT NOT NULL, title TEXT, created_at TEXT NOT NULL,
          UNIQUE(account_id, platform_thread_id),
          FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        );
        CREATE TABLE messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
          thread_id INTEGER NOT NULL, platform_message_id TEXT NOT NULL,
          direction TEXT NOT NULL, sender TEXT, text TEXT,
          sent_at TEXT NOT NULL, raw_json TEXT,
          UNIQUE(account_id, platform_message_id),
          FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE,
          FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE CASCADE
        );
        CREATE TABLE sync_cursors (
          account_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
          cursor TEXT, updated_at TEXT NOT NULL,
          PRIMARY KEY(account_id, thread_id),
          FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE,
          FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE CASCADE
        );
    """)
    # Insert pre-existing data including an invalid direction value.
    conn.execute("INSERT INTO accounts VALUES (1, 'old', '{\"li_at\":\"x\"}', NULL, '2024-01-01')")
    conn.execute("INSERT INTO threads VALUES (1, 1, 't1', 'Title', '2024-01-01')")
    conn.execute("INSERT INTO messages VALUES (1, 1, 1, 'm1', 'BOGUS', NULL, 'hi', '2024-01-01T00:00:00', NULL)")
    conn.execute("INSERT INTO messages VALUES (2, 1, 1, 'm2', 'out', NULL, 'bye', '2024-01-01T00:00:00', NULL)")
    conn.commit()
    conn.close()

    # Now open via Storage and run migrate — should upgrade in place.
    s = Storage(db_path=str(db_path))
    s.migrate()

    # Verify schema_version is current.
    assert s._get_schema_version() == 3

    # Verify indexes exist.
    indexes = {r[0] for r in s._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    assert "idx_threads_account_id" in indexes
    assert "idx_messages_thread_id" in indexes
    assert "idx_messages_account_id" in indexes

    # Verify existing data preserved; BOGUS direction normalized to 'in'.
    rows = s._conn.execute("SELECT direction FROM messages ORDER BY id").fetchall()
    assert rows[0]["direction"] == "in"   # was BOGUS, now 'in'
    assert rows[1]["direction"] == "out"   # preserved

    # Verify CHECK now enforced.
    with pytest.raises(sqlite3.IntegrityError):
        s._conn.execute(
            "INSERT INTO messages VALUES (3, 1, 1, 'm3', 'bad', NULL, 'x', '2024', NULL)"
        )

    s.close()


def test_messages_direction_check_rejects_invalid(storage, db_path):
    """Edge case: CHECK (direction IN ('in','out')) is enforced."""
    s = Storage(db_path=db_path)
    s.migrate()
    aid = s.create_account(label="a", auth=AccountAuth(li_at="x"), proxy=None)
    tid = s.upsert_thread(account_id=aid, platform_thread_id="t1", title=None)
    s.close()

    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO messages(account_id, thread_id, platform_message_id, direction, sender, text, sent_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (aid, tid, "mid1", "invalid", None, "hi", "2024-01-01T00:00:00+00:00", None),
        )
    conn.close()


def test_insert_message_raises_on_invalid_direction(storage):
    """insert_message raises IntegrityError for invalid direction (CHECK enforced via API)."""
    aid = storage.create_account(label="a", auth=AccountAuth(li_at="x"), proxy=None)
    tid = storage.upsert_thread(account_id=aid, platform_thread_id="t1", title=None)
    ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(sqlite3.IntegrityError):
        storage.insert_message(
            account_id=aid,
            thread_id=tid,
            platform_message_id="mid1",
            direction="invalid",
            sender=None,
            text="hi",
            sent_at=ts,
            raw=None,
        )


def test_messages_direction_check_accepts_in_and_out(storage):
    """direction 'in' and 'out' are accepted."""
    auth = AccountAuth(li_at="x")
    aid = storage.create_account(label="a", auth=auth, proxy=None)
    tid = storage.upsert_thread(account_id=aid, platform_thread_id="t1", title=None)
    ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert storage.insert_message(
        account_id=aid, thread_id=tid, platform_message_id="m1", direction="in", sender=None, text="hi", sent_at=ts, raw=None
    ) is True
    assert storage.insert_message(
        account_id=aid, thread_id=tid, platform_message_id="m2", direction="out", sender=None, text="bye", sent_at=ts, raw=None
    ) is True


def test_create_account_returns_id(storage):
    """CRUD: create_account returns new id."""
    aid = storage.create_account(label="test", auth=AccountAuth(li_at="cookie"), proxy=None)
    assert isinstance(aid, int)
    assert aid >= 1


def test_update_account_auth_replaces_credentials(storage):
    """CRUD: update_account_auth replaces stored auth and new value is retrievable."""
    aid = storage.create_account(label="test", auth=AccountAuth(li_at="old_cookie"), proxy=None)
    new_auth = AccountAuth(li_at="new_cookie", jsessionid="ajax:new")
    storage.update_account_auth(aid, new_auth)
    got = storage.get_account_auth(aid)
    assert got.li_at == "new_cookie"
    assert got.jsessionid == "ajax:new"


def test_update_account_auth_raises_for_unknown(storage):
    """Edge case: update_account_auth raises KeyError for missing account."""
    with pytest.raises(KeyError, match="account 99999 not found"):
        storage.update_account_auth(99999, AccountAuth(li_at="x"))


def test_get_account_auth_raises_for_unknown(storage):
    """Edge case: get_account_auth raises KeyError for missing account."""
    with pytest.raises(KeyError, match="account 99999 not found"):
        storage.get_account_auth(99999)


def test_list_threads_returns_empty_when_no_results(storage):
    """Edge case: list_threads returns empty list for account with no threads."""
    aid = storage.create_account(label="a", auth=AccountAuth(li_at="x"), proxy=None)
    rows = storage.list_threads(account_id=aid)
    assert rows == []


def test_upsert_thread_preserves_created_at_on_conflict(storage):
    """Edge case: second upsert only updates title, not created_at."""
    aid = storage.create_account(label="a", auth=AccountAuth(li_at="x"), proxy=None)
    t1 = storage.upsert_thread(account_id=aid, platform_thread_id="pt1", title="First")
    rows1 = storage.list_threads(account_id=aid)
    assert len(rows1) == 1
    created_first = rows1[0]["created_at"]
    t2 = storage.upsert_thread(account_id=aid, platform_thread_id="pt1", title="Second")
    assert t2 == t1
    rows2 = storage.list_threads(account_id=aid)
    assert len(rows2) == 1
    assert rows2[0]["created_at"] == created_first
    assert rows2[0]["platform_thread_id"] == "pt1"
    assert rows2[0]["title"] == "Second"


def test_get_cursor_returns_none_when_not_set(storage):
    """Edge case: get_cursor returns None when no row exists."""
    aid = storage.create_account(label="a", auth=AccountAuth(li_at="x"), proxy=None)
    tid = storage.upsert_thread(account_id=aid, platform_thread_id="pt1", title=None)
    assert storage.get_cursor(account_id=aid, thread_id=tid) is None


def test_set_cursor_and_get_cursor_roundtrip(storage):
    """CRUD: set_cursor then get_cursor returns value."""
    aid = storage.create_account(label="a", auth=AccountAuth(li_at="x"), proxy=None)
    tid = storage.upsert_thread(account_id=aid, platform_thread_id="pt1", title=None)
    storage.set_cursor(account_id=aid, thread_id=tid, cursor="next_page_xyz")
    assert storage.get_cursor(account_id=aid, thread_id=tid) == "next_page_xyz"


def test_insert_message_returns_true_first_time_false_duplicate(storage):
    """Regression: insert_message returns True when inserted, False on duplicate."""
    aid = storage.create_account(label="a", auth=AccountAuth(li_at="x"), proxy=None)
    tid = storage.upsert_thread(account_id=aid, platform_thread_id="pt1", title=None)
    ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    first = storage.insert_message(
        account_id=aid, thread_id=tid, platform_message_id="mid1", direction="in", sender=None, text="hi", sent_at=ts, raw=None
    )
    second = storage.insert_message(
        account_id=aid, thread_id=tid, platform_message_id="mid1", direction="in", sender=None, text="hi", sent_at=ts, raw=None
    )
    assert first is True
    assert second is False


def test_insert_message_normalizes_naive_datetime_as_utc(storage):
    """Edge case: naive sent_at is stored as UTC (no local-time misinterpretation)."""
    aid = storage.create_account(label="a", auth=AccountAuth(li_at="x"), proxy=None)
    tid = storage.upsert_thread(account_id=aid, platform_thread_id="pt1", title=None)
    naive = datetime(2024, 6, 15, 14, 30, 0)  # no tz
    storage.insert_message(
        account_id=aid, thread_id=tid, platform_message_id="mid1", direction="in", sender=None, text="x", sent_at=naive, raw=None
    )
    conn = sqlite3.connect(storage.db_path)
    row = conn.execute("SELECT sent_at FROM messages WHERE platform_message_id = 'mid1'").fetchone()
    conn.close()
    # Stored string should look like UTC (either +00:00 or Z), not local offset
    assert row is not None
    stored = row[0]
    assert "+00:00" in stored or stored.endswith("Z"), f"Expected UTC suffix, got {stored!r}"


def test_insert_message_converts_aware_non_utc_to_utc(storage):
    """Edge case: aware non-UTC sent_at is converted to UTC for storage."""
    from datetime import timedelta

    aid = storage.create_account(label="a", auth=AccountAuth(li_at="x"), proxy=None)
    tid = storage.upsert_thread(account_id=aid, platform_thread_id="pt1", title=None)
    # UTC+2 so 14:30 local = 12:30 UTC
    tz = timezone(timedelta(hours=2))
    aware = datetime(2024, 6, 15, 14, 30, 0, tzinfo=tz)
    storage.insert_message(
        account_id=aid, thread_id=tid, platform_message_id="mid1", direction="in", sender=None, text="x", sent_at=aware, raw=None
    )
    conn = sqlite3.connect(storage.db_path)
    row = conn.execute("SELECT sent_at FROM messages WHERE platform_message_id = 'mid1'").fetchone()
    conn.close()
    assert row is not None
    # Should store 12:30 UTC
    assert "12:30:00" in row[0]
    assert "+00:00" in row[0]


def test_get_account_proxy_returns_none_when_not_set(storage):
    """Edge case: get_account_proxy returns None when proxy_json is NULL."""
    aid = storage.create_account(label="a", auth=AccountAuth(li_at="x"), proxy=None)
    assert storage.get_account_proxy(aid) is None


def test_foreign_key_cascade_deletes_threads_on_account_delete(storage):
    """Edge case: deleting account cascades to threads, messages, cursors."""
    aid = storage.create_account(label="a", auth=AccountAuth(li_at="x"), proxy=None)
    tid = storage.upsert_thread(account_id=aid, platform_thread_id="pt1", title=None)
    storage.set_cursor(account_id=aid, thread_id=tid, cursor="c1")
    ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    storage.insert_message(
        account_id=aid, thread_id=tid, platform_message_id="m1", direction="in", sender=None, text="x", sent_at=ts, raw=None
    )
    storage._conn.execute("DELETE FROM accounts WHERE id = ?", (aid,))
    storage._conn.commit()
    rows = storage._conn.execute("SELECT COUNT(*) FROM threads").fetchone()
    assert rows[0] == 0
    rows = storage._conn.execute("SELECT COUNT(*) FROM messages").fetchone()
    assert rows[0] == 0
    rows = storage._conn.execute("SELECT COUNT(*) FROM sync_cursors").fetchone()
    assert rows[0] == 0
