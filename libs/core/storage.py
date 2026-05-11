from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .crypto import decrypt_if_encrypted, encrypt_if_configured
from .discord_fixtures import DISCORD_FIXTURES
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

_MIGRATION_5_DISCORD_FIXTURES = """
CREATE TABLE IF NOT EXISTS discord_accounts (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  account_type TEXT NOT NULL,
  approved_scope TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discord_guilds (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discord_channels (
  id TEXT PRIMARY KEY,
  guild_id TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  topic TEXT,
  FOREIGN KEY(guild_id) REFERENCES discord_guilds(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS discord_users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  display_name TEXT NOT NULL,
  profile_summary TEXT
);

CREATE TABLE IF NOT EXISTS discord_members (
  guild_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  roles_json TEXT NOT NULL,
  joined_at TEXT NOT NULL,
  PRIMARY KEY(guild_id, user_id),
  FOREIGN KEY(guild_id) REFERENCES discord_guilds(id) ON DELETE CASCADE,
  FOREIGN KEY(user_id) REFERENCES discord_users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS discord_messages (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  guild_id TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  author_user_id TEXT NOT NULL,
  content TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  raw_json TEXT,
  FOREIGN KEY(account_id) REFERENCES discord_accounts(id) ON DELETE CASCADE,
  FOREIGN KEY(guild_id) REFERENCES discord_guilds(id) ON DELETE CASCADE,
  FOREIGN KEY(channel_id) REFERENCES discord_channels(id) ON DELETE CASCADE,
  FOREIGN KEY(author_user_id) REFERENCES discord_users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS discord_lead_signals (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  guild_id TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  keyword TEXT NOT NULL,
  topic TEXT NOT NULL,
  summary TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(account_id) REFERENCES discord_accounts(id) ON DELETE CASCADE,
  FOREIGN KEY(guild_id) REFERENCES discord_guilds(id) ON DELETE CASCADE,
  FOREIGN KEY(channel_id) REFERENCES discord_channels(id) ON DELETE CASCADE,
  FOREIGN KEY(message_id) REFERENCES discord_messages(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_discord_channels_guild_id ON discord_channels(guild_id);
CREATE INDEX IF NOT EXISTS idx_discord_messages_account_id ON discord_messages(account_id);
CREATE INDEX IF NOT EXISTS idx_discord_messages_guild_channel ON discord_messages(guild_id, channel_id);
CREATE INDEX IF NOT EXISTS idx_discord_lead_signals_keyword ON discord_lead_signals(keyword);
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
            (5, _MIGRATION_5_DISCORD_FIXTURES),
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
    # Discord Sync fixture-only prototype storage
    # ------------------------------------------------------------------

    _DISCORD_COUNT_TABLES = {
        "accounts": "discord_accounts",
        "guilds": "discord_guilds",
        "channels": "discord_channels",
        "users": "discord_users",
        "members": "discord_members",
        "messages": "discord_messages",
        "lead_signals": "discord_lead_signals",
    }

    def ingest_discord_fixtures(self) -> dict[str, int]:
        """Upsert the synthetic Discord Sync fixture dataset.

        This deliberately does not accept tokens, cookies, emails, passwords, or
        live-sync configuration. It is a local seed path for the fixture-only
        Discord Sync prototype.
        """
        data = DISCORD_FIXTURES
        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO discord_accounts(id, label, account_type, approved_scope, created_at)
                VALUES (:id, :label, :account_type, :approved_scope, :created_at)
                ON CONFLICT(id) DO UPDATE SET
                  label=excluded.label,
                  account_type=excluded.account_type,
                  approved_scope=excluded.approved_scope,
                  created_at=excluded.created_at
                """,
                data["accounts"],
            )
            self._conn.executemany(
                """
                INSERT INTO discord_guilds(id, name, description, created_at)
                VALUES (:id, :name, :description, :created_at)
                ON CONFLICT(id) DO UPDATE SET
                  name=excluded.name,
                  description=excluded.description,
                  created_at=excluded.created_at
                """,
                data["guilds"],
            )
            self._conn.executemany(
                """
                INSERT INTO discord_channels(id, guild_id, name, kind, topic)
                VALUES (:id, :guild_id, :name, :kind, :topic)
                ON CONFLICT(id) DO UPDATE SET
                  guild_id=excluded.guild_id,
                  name=excluded.name,
                  kind=excluded.kind,
                  topic=excluded.topic
                """,
                data["channels"],
            )
            self._conn.executemany(
                """
                INSERT INTO discord_users(id, username, display_name, profile_summary)
                VALUES (:id, :username, :display_name, :profile_summary)
                ON CONFLICT(id) DO UPDATE SET
                  username=excluded.username,
                  display_name=excluded.display_name,
                  profile_summary=excluded.profile_summary
                """,
                data["users"],
            )
            self._conn.executemany(
                """
                INSERT INTO discord_members(guild_id, user_id, roles_json, joined_at)
                VALUES (:guild_id, :user_id, :roles_json, :joined_at)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                  roles_json=excluded.roles_json,
                  joined_at=excluded.joined_at
                """,
                [
                    {**member, "roles_json": json.dumps(member["roles"])}
                    for member in data["members"]
                ],
            )
            self._conn.executemany(
                """
                INSERT INTO discord_messages(
                  id, account_id, guild_id, channel_id, author_user_id, content, sent_at, raw_json
                ) VALUES (:id, :account_id, :guild_id, :channel_id, :author_user_id, :content, :sent_at, :raw_json)
                ON CONFLICT(id) DO UPDATE SET
                  account_id=excluded.account_id,
                  guild_id=excluded.guild_id,
                  channel_id=excluded.channel_id,
                  author_user_id=excluded.author_user_id,
                  content=excluded.content,
                  sent_at=excluded.sent_at,
                  raw_json=excluded.raw_json
                """,
                [
                    {**message, "raw_json": json.dumps(message.get("raw") or {})}
                    for message in data["messages"]
                ],
            )
            self._conn.executemany(
                """
                INSERT INTO discord_lead_signals(
                  id, account_id, guild_id, channel_id, message_id, keyword, topic, summary, evidence_json, created_at
                ) VALUES (
                  :id, :account_id, :guild_id, :channel_id, :message_id, :keyword, :topic, :summary, :evidence_json, :created_at
                )
                ON CONFLICT(id) DO UPDATE SET
                  account_id=excluded.account_id,
                  guild_id=excluded.guild_id,
                  channel_id=excluded.channel_id,
                  message_id=excluded.message_id,
                  keyword=excluded.keyword,
                  topic=excluded.topic,
                  summary=excluded.summary,
                  evidence_json=excluded.evidence_json,
                  created_at=excluded.created_at
                """,
                [
                    {**signal, "evidence_json": json.dumps(signal["evidence"])}
                    for signal in data["lead_signals"]
                ],
            )
        return self.discord_fixture_counts()

    def discord_fixture_counts(self) -> dict[str, int]:
        return {
            key: int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for key, table in self._DISCORD_COUNT_TABLES.items()
        }

    def list_discord_accounts(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT a.*, COUNT(m.id) AS message_count
            FROM discord_accounts a
            LEFT JOIN discord_messages m ON m.account_id = a.id
            GROUP BY a.id
            ORDER BY a.label
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def list_discord_guilds(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT g.*, COUNT(DISTINCT c.id) AS channel_count, COUNT(m.id) AS message_count
            FROM discord_guilds g
            LEFT JOIN discord_channels c ON c.guild_id = g.id
            LEFT JOIN discord_messages m ON m.guild_id = g.id
            GROUP BY g.id
            ORDER BY g.name
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def list_discord_channels(self, *, guild_id: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if guild_id:
            where = "WHERE c.guild_id = ?"
            params.append(guild_id)
        rows = self._conn.execute(
            f"""
            SELECT c.*, g.name AS guild_name, COUNT(m.id) AS message_count
            FROM discord_channels c
            JOIN discord_guilds g ON g.id = c.guild_id
            LEFT JOIN discord_messages m ON m.channel_id = c.id
            {where}
            GROUP BY c.id
            ORDER BY g.name, c.name
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def list_discord_users(self, *, guild_id: str | None = None) -> list[dict[str, Any]]:
        if guild_id:
            rows = self._conn.execute(
                """
                SELECT u.*, dm.guild_id, dm.roles_json
                FROM discord_members dm
                JOIN discord_users u ON u.id = dm.user_id
                WHERE dm.guild_id = ?
                ORDER BY u.display_name
                """,
                (guild_id,),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM discord_users ORDER BY display_name").fetchall()
        return [self._decode_discord_json_columns(dict(row)) for row in rows]

    def list_discord_messages(
        self,
        *,
        account_id: str | None = None,
        guild_id: str | None = None,
        channel_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("m.account_id", account_id),
            ("m.guild_id", guild_id),
            ("m.channel_id", channel_id),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"""
            SELECT
              m.id, m.account_id, a.label AS account_label,
              m.guild_id, g.name AS guild_name,
              m.channel_id, c.name AS channel_name,
              m.author_user_id, u.display_name AS author_display_name, u.username AS author_username,
              m.content, m.sent_at, m.raw_json
            FROM discord_messages m
            JOIN discord_accounts a ON a.id = m.account_id
            JOIN discord_guilds g ON g.id = m.guild_id
            JOIN discord_channels c ON c.id = m.channel_id
            JOIN discord_users u ON u.id = m.author_user_id
            {where}
            ORDER BY m.sent_at ASC, m.id ASC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        return [self._decode_discord_json_columns(dict(row)) for row in rows]

    def search_discord_messages(
        self,
        query: str,
        *,
        account_id: str | None = None,
        guild_id: str | None = None,
        channel_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        normalized = query.strip().lower()
        if not normalized:
            raise ValueError("query must be non-empty")
        clauses = ["LOWER(m.content) LIKE ?"]
        params: list[Any] = [f"%{normalized}%"]
        for column, value in (
            ("m.account_id", account_id),
            ("m.guild_id", guild_id),
            ("m.channel_id", channel_id),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        rows = self._conn.execute(
            f"""
            SELECT
              m.id, m.account_id, a.label AS account_label,
              m.guild_id, g.name AS guild_name,
              m.channel_id, c.name AS channel_name,
              m.author_user_id, u.display_name AS author_display_name, u.username AS author_username,
              m.content, m.sent_at, m.raw_json
            FROM discord_messages m
            JOIN discord_accounts a ON a.id = m.account_id
            JOIN discord_guilds g ON g.id = m.guild_id
            JOIN discord_channels c ON c.id = m.channel_id
            JOIN discord_users u ON u.id = m.author_user_id
            WHERE {' AND '.join(clauses)}
            ORDER BY m.sent_at ASC, m.id ASC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        return [self._decode_discord_json_columns(dict(row)) for row in rows]

    def list_discord_lead_signals(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT
              s.*, a.label AS account_label, g.name AS guild_name, c.name AS channel_name,
              m.content AS evidence_message
            FROM discord_lead_signals s
            JOIN discord_accounts a ON a.id = s.account_id
            JOIN discord_guilds g ON g.id = s.guild_id
            JOIN discord_channels c ON c.id = s.channel_id
            JOIN discord_messages m ON m.id = s.message_id
            ORDER BY s.created_at ASC, s.id ASC
            """
        ).fetchall()
        return [self._decode_discord_json_columns(dict(row)) for row in rows]

    def _decode_discord_json_columns(self, row: dict[str, Any]) -> dict[str, Any]:
        if row.get("raw_json") is not None:
            row["raw"] = json.loads(row.pop("raw_json") or "{}")
        if row.get("roles_json") is not None:
            row["roles"] = json.loads(row.pop("roles_json") or "[]")
        if row.get("evidence_json") is not None:
            row["evidence"] = json.loads(row.pop("evidence_json") or "[]")
        return row
