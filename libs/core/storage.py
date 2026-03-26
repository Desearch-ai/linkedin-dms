from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .crypto import decrypt_if_encrypted, encrypt_if_configured
from .models import AccountAuth, ProxyConfig


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_sent_at_to_utc(dt: datetime) -> str:
    """Return ISO string in UTC. Naive datetimes are assumed UTC; aware are converted."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).isoformat()
    return dt.astimezone(timezone.utc).isoformat()


# Schema version 0 = baseline tables (accounts, threads, messages, sync_cursors).
# Later versions add indexes, CHECK constraints, etc. Migrations run in order.
_MIGRATION_1_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_threads_account_id ON threads(account_id);
CREATE INDEX IF NOT EXISTS idx_messages_thread_id ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_account_id ON messages(account_id);
"""

_MIGRATION_2_MESSAGES_CHECK = """
UPDATE messages SET direction = 'in' WHERE direction NOT IN ('in', 'out');
CREATE TABLE messages_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  thread_id INTEGER NOT NULL,
  platform_message_id TEXT NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
  sender TEXT,
  text TEXT,
  sent_at TEXT NOT NULL,
  raw_json TEXT,
  UNIQUE(account_id, platform_message_id),
  FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE,
  FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE CASCADE
);
INSERT INTO messages_new SELECT id, account_id, thread_id, platform_message_id, direction, sender, text, sent_at, raw_json FROM messages;
DROP TABLE messages;
ALTER TABLE messages_new RENAME TO messages;
CREATE INDEX IF NOT EXISTS idx_messages_thread_id ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_account_id ON messages(account_id);
"""

_MIGRATION_3_OUTBOUND_SENDS = """
CREATE TABLE IF NOT EXISTS outbound_sends (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  idempotency_key TEXT,
  recipient TEXT NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
  platform_message_id TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(account_id, idempotency_key),
  FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_outbound_sends_account_status ON outbound_sends(account_id, status);
"""


class Storage:
    """SQLite storage.

    This is intentionally tiny and dependency-free for contributors.
    """

    def __init__(self, db_path: str | Path = "./desearch_linkedin_dms.sqlite"):
        self.db_path = str(db_path)
        # FastAPI executes sync endpoints in a threadpool by default.
        # For MVP simplicity we allow cross-thread usage.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self._conn.close()

    def _get_schema_version(self) -> int:
        row = self._conn.execute("SELECT version FROM schema_version WHERE single_row = 1 LIMIT 1").fetchone()
        if row is None:
            return -1
        return int(row["version"])

    def _set_schema_version(self, version: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_version(single_row, version) VALUES(1, ?)", (version,)
        )

    def migrate(self) -> None:
        """Create tables if they don't exist and run pending migrations."""
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              label TEXT NOT NULL,
              auth_json TEXT NOT NULL,
              proxy_json TEXT,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS threads (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              account_id INTEGER NOT NULL,
              platform_thread_id TEXT NOT NULL,
              title TEXT,
              created_at TEXT NOT NULL,
              UNIQUE(account_id, platform_thread_id),
              FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              account_id INTEGER NOT NULL,
              thread_id INTEGER NOT NULL,
              platform_message_id TEXT NOT NULL,
              direction TEXT NOT NULL,
              sender TEXT,
              text TEXT,
              sent_at TEXT NOT NULL,
              raw_json TEXT,
              UNIQUE(account_id, platform_message_id),
              FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE,
              FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS sync_cursors (
              account_id INTEGER NOT NULL,
              thread_id INTEGER NOT NULL,
              cursor TEXT,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(account_id, thread_id),
              FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE,
              FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE CASCADE
            );
            """
        )
        self._conn.commit()

        # Bootstrap schema_version for existing DBs: single row storing current version (0 = baseline).
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
              single_row INTEGER NOT NULL PRIMARY KEY CHECK (single_row = 1),
              version INTEGER NOT NULL
            )
            """
        )
        if self._get_schema_version() < 0:
            self._set_schema_version(0)
            self._conn.commit()

        current = self._get_schema_version()
        migrations: list[tuple[int, str]] = [
            (1, _MIGRATION_1_INDEXES),
            (2, _MIGRATION_2_MESSAGES_CHECK),
            (3, _MIGRATION_3_OUTBOUND_SENDS),
        ]
        for version, sql in migrations:
            if version > current:
                self._conn.executescript(sql)
                self._set_schema_version(version)
                self._conn.commit()
                current = version

    def create_account(
        self,
        *,
        label: str,
        auth: AccountAuth,
        proxy: Optional[ProxyConfig] = None,
    ) -> int:
        created_at = utcnow().isoformat()
        auth_json = encrypt_if_configured(json.dumps(asdict(auth)))
        proxy_json = encrypt_if_configured(json.dumps(asdict(proxy))) if proxy else None
        cur = self._conn.execute(
            "INSERT INTO accounts(label, auth_json, proxy_json, created_at) VALUES (?, ?, ?, ?)",
            (label, auth_json, proxy_json, created_at),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def update_account_auth(
        self,
        account_id: int,
        auth: AccountAuth,
    ) -> None:
        """Replace the auth credentials for an existing account.

        Raises KeyError if the account does not exist.
        """
        row = self._conn.execute("SELECT id FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise KeyError(f"account {account_id} not found")
        auth_json = encrypt_if_configured(json.dumps(asdict(auth)))
        self._conn.execute(
            "UPDATE accounts SET auth_json=? WHERE id=?",
            (auth_json, account_id),
        )
        self._conn.commit()

    def get_account_auth(self, account_id: int) -> AccountAuth:
        row = self._conn.execute("SELECT auth_json FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise KeyError(f"account {account_id} not found")
        d = json.loads(decrypt_if_encrypted(row["auth_json"]))
        return AccountAuth(**d)

    def get_account_proxy(self, account_id: int) -> Optional[ProxyConfig]:
        row = self._conn.execute("SELECT proxy_json FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise KeyError(f"account {account_id} not found")
        if not row["proxy_json"]:
            return None
        d = json.loads(decrypt_if_encrypted(row["proxy_json"]))
        return ProxyConfig(**d)

    def upsert_thread(self, *, account_id: int, platform_thread_id: str, title: Optional[str]) -> int:
        created_at = utcnow().isoformat()
        self._conn.execute(
            """
            INSERT INTO threads(account_id, platform_thread_id, title, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(account_id, platform_thread_id) DO UPDATE SET title=excluded.title
            """,
            (account_id, platform_thread_id, title, created_at),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM threads WHERE account_id=? AND platform_thread_id=?",
            (account_id, platform_thread_id),
        ).fetchone()
        return int(row["id"])

    def list_threads(self, *, account_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, platform_thread_id, title, created_at FROM threads WHERE account_id=? ORDER BY id DESC",
            (account_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_cursor(self, *, account_id: int, thread_id: int) -> Optional[str]:
        row = self._conn.execute(
            "SELECT cursor FROM sync_cursors WHERE account_id=? AND thread_id=?",
            (account_id, thread_id),
        ).fetchone()
        return None if not row else row["cursor"]

    def set_cursor(self, *, account_id: int, thread_id: int, cursor: Optional[str]) -> None:
        self._conn.execute(
            """
            INSERT INTO sync_cursors(account_id, thread_id, cursor, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(account_id, thread_id)
            DO UPDATE SET cursor=excluded.cursor, updated_at=excluded.updated_at
            """,
            (account_id, thread_id, cursor, utcnow().isoformat()),
        )
        self._conn.commit()

    def insert_message(
        self,
        *,
        account_id: int,
        thread_id: int,
        platform_message_id: str,
        direction: str,
        sender: Optional[str],
        text: Optional[str],
        sent_at: datetime,
        raw: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Insert message if not exists. Returns True if inserted, False if duplicate."""
        try:
            self._conn.execute(
                """
                INSERT INTO messages(
                  account_id, thread_id, platform_message_id, direction, sender, text, sent_at, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    thread_id,
                    platform_message_id,
                    direction,
                    sender,
                    text,
                    _normalize_sent_at_to_utc(sent_at),
                    json.dumps(raw) if raw else None,
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError as e:
            # Only treat UNIQUE (duplicate message) as non-fatal; CHECK (invalid direction) should propagate.
            if "UNIQUE constraint failed" in str(e):
                return False
            raise

    # ------------------------------------------------------------------
    # Outbound send tracking
    # ------------------------------------------------------------------

    _VALID_SEND_STATUSES = frozenset({"pending", "sent", "failed"})

    def create_or_get_outbound_send(
        self,
        *,
        account_id: int,
        idempotency_key: Optional[str],
        recipient: str,
        text: str,
    ) -> tuple[int, Optional[dict[str, Any]]]:
        """Create a pending outbound send record, or return an existing one.

        When *idempotency_key* is non-None and a record already exists for the
        same ``(account_id, idempotency_key)`` pair, the existing row is
        returned without creating a duplicate.  SQLite treats NULL as distinct
        for UNIQUE constraints, so calls with ``idempotency_key=None`` always
        create a new record.

        Returns:
            ``(send_id, existing_row_or_None)``.  If the second element is not
            None the caller should inspect its ``status`` to decide whether to
            re-attempt the send.
        """
        if idempotency_key is not None:
            existing = self._conn.execute(
                "SELECT * FROM outbound_sends WHERE account_id=? AND idempotency_key=?",
                (account_id, idempotency_key),
            ).fetchone()
            if existing:
                return int(existing["id"]), dict(existing)

        now = utcnow().isoformat()
        try:
            cur = self._conn.execute(
                """
                INSERT INTO outbound_sends(
                  account_id, idempotency_key, recipient, text, status, attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (account_id, idempotency_key, recipient, text, now, now),
            )
            self._conn.commit()
            return int(cur.lastrowid), None
        except sqlite3.IntegrityError:
            if idempotency_key is None:
                raise
            row = self._conn.execute(
                "SELECT * FROM outbound_sends WHERE account_id=? AND idempotency_key=?",
                (account_id, idempotency_key),
            ).fetchone()
            if row:
                return int(row["id"]), dict(row)
            raise

    def mark_outbound_sent(
        self,
        *,
        send_id: int,
        platform_message_id: str,
    ) -> None:
        """Atomically mark an outbound send as successfully sent."""
        now = utcnow().isoformat()
        self._conn.execute(
            """
            UPDATE outbound_sends
            SET status='sent', platform_message_id=?, attempts=attempts+1, updated_at=?
            WHERE id=?
            """,
            (platform_message_id, now, send_id),
        )
        self._conn.commit()

    def mark_outbound_failed(
        self,
        *,
        send_id: int,
        error: str,
    ) -> None:
        """Atomically mark an outbound send as failed."""
        now = utcnow().isoformat()
        self._conn.execute(
            """
            UPDATE outbound_sends
            SET status='failed', last_error=?, attempts=attempts+1, updated_at=?
            WHERE id=?
            """,
            (error, now, send_id),
        )
        self._conn.commit()

    def get_outbound_send(self, *, send_id: int) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM outbound_sends WHERE id=?",
            (send_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_outbound_sends(
        self,
        *,
        account_id: int,
        status: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if status is not None and status not in self._VALID_SEND_STATUSES:
            raise ValueError(
                f"invalid status filter {status!r}; expected one of {sorted(self._VALID_SEND_STATUSES)}"
            )
        if status:
            rows = self._conn.execute(
                "SELECT * FROM outbound_sends WHERE account_id=? AND status=? ORDER BY id DESC",
                (account_id, status),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM outbound_sends WHERE account_id=? ORDER BY id DESC",
                (account_id,),
            ).fetchall()
        return [dict(r) for r in rows]
