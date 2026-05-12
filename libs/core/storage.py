from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .crypto import decrypt_if_encrypted, encrypt_if_configured
from .models import AccountAuth, BrowserContext, ProxyConfig


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

_MIGRATION_4_BROWSER_CONTEXT = """
ALTER TABLE accounts ADD COLUMN browser_context_json TEXT;
"""

_MIGRATION_5_DISCORD_SYNC = """
CREATE TABLE IF NOT EXISTS discord_oauth_states (
  state TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  consumed_at TEXT
);

CREATE TABLE IF NOT EXISTS discord_accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  discord_user_id TEXT NOT NULL UNIQUE,
  username TEXT,
  global_name TEXT,
  scopes_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL,
  token_json TEXT,
  token_persisted INTEGER NOT NULL DEFAULT 0,
  token_expires_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discord_guilds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  discord_guild_id TEXT NOT NULL,
  name TEXT,
  icon TEXT,
  owner INTEGER,
  permissions TEXT,
  provenance TEXT NOT NULL,
  last_error TEXT,
  last_synced_at TEXT,
  raw_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(account_id, discord_guild_id),
  FOREIGN KEY(account_id) REFERENCES discord_accounts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS discord_channels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  discord_guild_id TEXT NOT NULL,
  discord_channel_id TEXT NOT NULL,
  name TEXT,
  type INTEGER,
  parent_id TEXT,
  topic TEXT,
  nsfw INTEGER,
  position INTEGER,
  provenance TEXT NOT NULL,
  last_error TEXT,
  last_synced_at TEXT,
  raw_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(account_id, discord_channel_id),
  FOREIGN KEY(account_id) REFERENCES discord_accounts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS discord_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  discord_channel_id TEXT NOT NULL,
  platform_message_id TEXT NOT NULL,
  author_id TEXT,
  author_username TEXT,
  author_global_name TEXT,
  content TEXT,
  sent_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'live',
  provenance TEXT NOT NULL,
  raw_json TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(account_id, discord_channel_id, platform_message_id),
  FOREIGN KEY(account_id) REFERENCES discord_accounts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS discord_sync_errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER,
  scope TEXT NOT NULL,
  discord_guild_id TEXT,
  discord_channel_id TEXT,
  status_code INTEGER,
  route TEXT,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(account_id) REFERENCES discord_accounts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_discord_guilds_account ON discord_guilds(account_id);
CREATE INDEX IF NOT EXISTS idx_discord_channels_guild ON discord_channels(account_id, discord_guild_id);
CREATE INDEX IF NOT EXISTS idx_discord_messages_channel ON discord_messages(account_id, discord_channel_id);
CREATE INDEX IF NOT EXISTS idx_discord_errors_account ON discord_sync_errors(account_id, created_at);
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
            (4, _MIGRATION_4_BROWSER_CONTEXT),
            (5, _MIGRATION_5_DISCORD_SYNC),
        ]
        for version, sql in migrations:
            if version > current:
                self._conn.executescript(sql)
                self._set_schema_version(version)
                self._conn.commit()
                current = version

        # Some local validation databases may have schema_version=5 recorded from a
        # prior interrupted run while the Discord tables themselves are absent.
        # Migration 5 is written with CREATE TABLE/INDEX IF NOT EXISTS, so replay it
        # as an idempotent repair when any Discord Sync table is missing.
        if current >= 5 and self._missing_discord_sync_tables():
            self._conn.executescript(_MIGRATION_5_DISCORD_SYNC)
            self._conn.commit()

    def _missing_discord_sync_tables(self) -> list[str]:
        expected = {
            "discord_oauth_states",
            "discord_accounts",
            "discord_guilds",
            "discord_channels",
            "discord_messages",
            "discord_sync_errors",
        }
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'discord_%'"
        ).fetchall()
        present = {str(row[0]) for row in rows}
        return sorted(expected - present)

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

    def update_browser_context(self, account_id: int, ctx: BrowserContext) -> None:
        """Persist extension-captured browser context for an account.

        Raises KeyError if the account does not exist.
        """
        row = self._conn.execute("SELECT id FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise KeyError(f"account {account_id} not found")
        ctx_json = encrypt_if_configured(json.dumps({"x_li_track": ctx.x_li_track, "csrf_token": ctx.csrf_token}))
        self._conn.execute(
            "UPDATE accounts SET browser_context_json=? WHERE id=?",
            (ctx_json, account_id),
        )
        self._conn.commit()

    def get_browser_context(self, account_id: int) -> Optional[BrowserContext]:
        """Return stored browser context for an account, or None if not yet captured."""
        row = self._conn.execute(
            "SELECT browser_context_json FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        if not row:
            raise KeyError(f"account {account_id} not found")
        if not row["browser_context_json"]:
            return None
        d = json.loads(decrypt_if_encrypted(row["browser_context_json"]))
        ctx = BrowserContext(**d)
        return None if ctx.is_empty() else ctx

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

    # ------------------------------------------------------------------
    # Discord Sync storage
    # ------------------------------------------------------------------

    def create_discord_oauth_state(self, state: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO discord_oauth_states(state, created_at, consumed_at) VALUES (?, ?, NULL)",
            (state, utcnow().isoformat()),
        )
        self._conn.commit()

    def consume_discord_oauth_state(self, state: str) -> bool:
        row = self._conn.execute(
            "SELECT consumed_at FROM discord_oauth_states WHERE state=?", (state,)
        ).fetchone()
        if not row or row["consumed_at"]:
            return False
        self._conn.execute(
            "UPDATE discord_oauth_states SET consumed_at=? WHERE state=?",
            (utcnow().isoformat(), state),
        )
        self._conn.commit()
        return True

    def discord_token_persistence_enabled(self) -> bool:
        return bool(os.environ.get("DESEARCH_ENCRYPTION_KEY", "").strip())

    def upsert_discord_account(
        self,
        *,
        discord_user_id: str,
        username: str | None,
        global_name: str | None,
        scopes: list[str],
        status: str,
        token_material: dict[str, Any] | None,
        token_expires_at: str | None,
        last_error: str | None = None,
    ) -> int:
        now = utcnow().isoformat()
        token_json: str | None = None
        token_persisted = 0
        if token_material is not None and self.discord_token_persistence_enabled():
            token_json = encrypt_if_configured(json.dumps(token_material))
            token_persisted = 1
        self._conn.execute(
            """
            INSERT INTO discord_accounts(
              discord_user_id, username, global_name, scopes_json, status, token_json,
              token_persisted, token_expires_at, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_user_id) DO UPDATE SET
              username=excluded.username,
              global_name=excluded.global_name,
              scopes_json=excluded.scopes_json,
              status=excluded.status,
              token_json=excluded.token_json,
              token_persisted=excluded.token_persisted,
              token_expires_at=excluded.token_expires_at,
              last_error=excluded.last_error,
              updated_at=excluded.updated_at
            """,
            (
                discord_user_id, username, global_name, json.dumps(scopes), status, token_json,
                token_persisted, token_expires_at, last_error, now, now,
            ),
        )
        self._conn.commit()
        row = self._conn.execute("SELECT id FROM discord_accounts WHERE discord_user_id=?", (discord_user_id,)).fetchone()
        return int(row["id"])

    def get_discord_account(self, account_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM discord_accounts WHERE id=?", (account_id,)).fetchone()
        return self._discord_account_row(row) if row else None

    def list_discord_accounts(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM discord_accounts ORDER BY updated_at DESC, id DESC").fetchall()
        return [self._discord_account_row(r) for r in rows]

    def get_discord_account_token(self, account_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT token_json, token_persisted FROM discord_accounts WHERE id=?", (account_id,)).fetchone()
        if not row or not row["token_json"] or not row["token_persisted"]:
            return None
        return json.loads(decrypt_if_encrypted(row["token_json"]))

    def _discord_account_row(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d.pop("token_json", None)
        d["token_persisted"] = bool(d.get("token_persisted"))
        d["scopes"] = json.loads(d.pop("scopes_json") or "[]")
        return d

    def upsert_discord_guild(self, *, account_id: int, guild: dict[str, Any], provenance: str) -> None:
        now = utcnow().isoformat()
        self._conn.execute(
            """
            INSERT INTO discord_guilds(account_id, discord_guild_id, name, icon, owner, permissions, provenance, last_error, last_synced_at, raw_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
            ON CONFLICT(account_id, discord_guild_id) DO UPDATE SET
              name=excluded.name, icon=excluded.icon, owner=excluded.owner, permissions=excluded.permissions, provenance=excluded.provenance,
              last_error=NULL, last_synced_at=excluded.last_synced_at, raw_json=excluded.raw_json, updated_at=excluded.updated_at
            """,
            (account_id, str(guild.get("id")), guild.get("name"), guild.get("icon"), int(bool(guild.get("owner"))) if guild.get("owner") is not None else None, str(guild.get("permissions")) if guild.get("permissions") is not None else None, provenance, now, json.dumps(guild), now, now),
        )
        self._conn.commit()

    def list_discord_guilds(self, *, account_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM discord_guilds WHERE account_id=? ORDER BY name COLLATE NOCASE, discord_guild_id", (account_id,)).fetchall()
        return [self._row_with_json(r, "raw_json") for r in rows]

    def upsert_discord_channel(self, *, account_id: int, guild_id: str, channel: dict[str, Any], provenance: str) -> None:
        now = utcnow().isoformat()
        self._conn.execute(
            """
            INSERT INTO discord_channels(account_id, discord_guild_id, discord_channel_id, name, type, parent_id, topic, nsfw, position, provenance, last_error, last_synced_at, raw_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
            ON CONFLICT(account_id, discord_channel_id) DO UPDATE SET
              discord_guild_id=excluded.discord_guild_id, name=excluded.name, type=excluded.type, parent_id=excluded.parent_id,
              topic=excluded.topic, nsfw=excluded.nsfw, position=excluded.position, provenance=excluded.provenance,
              last_error=NULL, last_synced_at=excluded.last_synced_at, raw_json=excluded.raw_json, updated_at=excluded.updated_at
            """,
            (account_id, guild_id, str(channel.get("id")), channel.get("name"), channel.get("type"), channel.get("parent_id"), channel.get("topic"), int(bool(channel.get("nsfw"))) if channel.get("nsfw") is not None else None, channel.get("position"), provenance, now, json.dumps(channel), now, now),
        )
        self._conn.commit()

    def list_discord_channels(self, *, account_id: int, guild_id: str | None = None) -> list[dict[str, Any]]:
        if guild_id:
            rows = self._conn.execute("SELECT * FROM discord_channels WHERE account_id=? AND discord_guild_id=? ORDER BY position, name COLLATE NOCASE", (account_id, guild_id)).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM discord_channels WHERE account_id=? ORDER BY discord_guild_id, position, name COLLATE NOCASE", (account_id,)).fetchall()
        return [self._row_with_json(r, "raw_json") for r in rows]

    def insert_discord_message(self, *, account_id: int, channel_id: str, message: dict[str, Any], provenance: str) -> bool:
        author = message.get("author") or {}
        sent_at = message.get("timestamp") or utcnow().isoformat()
        now = utcnow().isoformat()
        try:
            self._conn.execute(
                """
                INSERT INTO discord_messages(account_id, discord_channel_id, platform_message_id, author_id, author_username, author_global_name, content, sent_at, source, provenance, raw_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'live', ?, ?, ?)
                """,
                (account_id, channel_id, str(message.get("id")), author.get("id"), author.get("username"), author.get("global_name"), message.get("content"), sent_at, provenance, json.dumps(message), now),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed" in str(e):
                return False
            raise

    def list_discord_messages(self, *, account_id: int | None = None, channel_id: str | None = None, q: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if account_id is not None:
            clauses.append("account_id=?")
            params.append(account_id)
        if channel_id is not None:
            clauses.append("discord_channel_id=?")
            params.append(channel_id)
        if q:
            clauses.append("content LIKE ?")
            params.append(f"%{q}%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        rows = self._conn.execute(f"SELECT * FROM discord_messages{where} ORDER BY sent_at DESC, id DESC LIMIT ?", params).fetchall()
        return [self._row_with_json(r, "raw_json") for r in rows]

    def record_discord_error(self, *, account_id: int | None, scope: str, message: str, guild_id: str | None = None, channel_id: str | None = None, status_code: int | None = None, route: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO discord_sync_errors(account_id, scope, discord_guild_id, discord_channel_id, status_code, route, message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, scope, guild_id, channel_id, status_code, route, message, utcnow().isoformat()),
        )
        self._conn.commit()

    def list_discord_errors(self, *, account_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if account_id is None:
            rows = self._conn.execute("SELECT * FROM discord_sync_errors ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM discord_sync_errors WHERE account_id=? ORDER BY created_at DESC, id DESC LIMIT ?", (account_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def _row_with_json(self, row: sqlite3.Row, json_field: str) -> dict[str, Any]:
        d = dict(row)
        value = d.get(json_field)
        d[json_field.replace("_json", "")] = json.loads(value) if value else None
        return d
