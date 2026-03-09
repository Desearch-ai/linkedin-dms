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

    def migrate(self) -> None:
        """Create tables if they don't exist."""
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

    def create_account(
        self,
        *,
        label: str,
        auth: AccountAuth,
        proxy: Optional[ProxyConfig] = None,
    ) -> int:
        created_at = utcnow().isoformat()
        auth_json = json.dumps(asdict(auth))
        proxy_json = json.dumps(asdict(proxy)) if proxy else None
        auth_json = encrypt_if_configured(auth_json)
        proxy_json = encrypt_if_configured(proxy_json) if proxy_json else None
        cur = self._conn.execute(
            "INSERT INTO accounts(label, auth_json, proxy_json, created_at) VALUES (?, ?, ?, ?)",
            (label, auth_json, proxy_json, created_at),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def get_account_auth(self, account_id: int) -> AccountAuth:
        row = self._conn.execute("SELECT auth_json FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise KeyError(f"account {account_id} not found")
        raw = decrypt_if_encrypted(row["auth_json"])
        d = json.loads(raw)
        return AccountAuth(**d)

    def get_account_proxy(self, account_id: int) -> Optional[ProxyConfig]:
        row = self._conn.execute("SELECT proxy_json FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise KeyError(f"account {account_id} not found")
        if not row["proxy_json"]:
            return None
        raw = decrypt_if_encrypted(row["proxy_json"])
        d = json.loads(raw)
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
                    sent_at.replace(tzinfo=timezone.utc).isoformat(),
                    json.dumps(raw) if raw else None,
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
