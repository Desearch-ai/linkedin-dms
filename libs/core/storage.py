from __future__ import annotations

import json
import hashlib
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .crypto import decrypt_if_encrypted, encrypt_if_configured
from .models import AccountAuth, BrowserContext, ProxyConfig
from .redaction import redact_for_log, redact_string


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

_MIGRATION_5_OPS_CONSOLE = """
CREATE TABLE IF NOT EXISTS draft_replies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  thread_id INTEGER,
  recipient TEXT NOT NULL,
  text TEXT NOT NULL,
  text_sha256 TEXT NOT NULL,
  campaign_id INTEGER,
  idempotency_key TEXT,
  state TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft', 'approved', 'revoked', 'sent')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE,
  FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_draft_replies_account_state ON draft_replies(account_id, state);

CREATE TABLE IF NOT EXISTS send_approvals (
  approval_id TEXT PRIMARY KEY,
  draft_id INTEGER NOT NULL,
  account_id INTEGER NOT NULL,
  recipient TEXT NOT NULL,
  text_sha256 TEXT NOT NULL,
  idempotency_key TEXT,
  state TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft', 'approved', 'revoked', 'used')),
  approved_by TEXT,
  approved_at TEXT,
  revoked_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(draft_id) REFERENCES draft_replies(id) ON DELETE CASCADE,
  FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_send_approvals_account_state ON send_approvals(account_id, state);

CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft', 'active', 'paused', 'archived')),
  rate_limit_daily_cap INTEGER NOT NULL DEFAULT 25,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_campaigns_account_state ON campaigns(account_id, state);

CREATE TABLE IF NOT EXISTS campaign_recipients (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id INTEGER NOT NULL,
  recipient TEXT NOT NULL,
  thread_id INTEGER,
  draft_id INTEGER,
  approval_id TEXT,
  state TEXT NOT NULL DEFAULT 'draft',
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE,
  FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE SET NULL,
  FOREIGN KEY(draft_id) REFERENCES draft_replies(id) ON DELETE SET NULL,
  FOREIGN KEY(approval_id) REFERENCES send_approvals(approval_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_campaign_recipients_campaign_state ON campaign_recipients(campaign_id, state);

CREATE TABLE IF NOT EXISTS ops_audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER,
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  entity_type TEXT,
  entity_id TEXT,
  redacted_payload_json TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ops_audit_account_created ON ops_audit_events(account_id, created_at);
"""


_MAX_PREVIEW_CHARS = 160


def _safe_text(text: str | None, *, max_chars: int | None = None) -> str | None:
    if text is None:
        return None
    safe = redact_string(text)
    if max_chars is not None and len(safe) > max_chars:
        safe = safe[: max_chars - 1].rstrip() + "…"
    return safe


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _offset_from_cursor(cursor: str | None) -> int:
    if cursor in (None, ""):
        return 0
    try:
        offset = int(cursor)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid cursor") from exc
    if offset < 0:
        raise ValueError("invalid cursor")
    return offset


def _page_meta(*, limit: int, offset: int, returned: int) -> dict[str, Any]:
    return {"limit": limit, "next_cursor": str(offset + limit) if returned == limit else None}


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
            (5, _MIGRATION_5_OPS_CONSOLE),
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
    # Ops Console safe read/query helpers
    # ------------------------------------------------------------------

    def _ensure_account_exists(self, account_id: int) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT id, label, proxy_json, browser_context_json, created_at FROM accounts WHERE id=?",
            (account_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"account {account_id} not found")
        return row

    def _thread_row(self, *, account_id: int, thread_id: int) -> sqlite3.Row:
        self._ensure_account_exists(account_id)
        row = self._conn.execute(
            """
            SELECT id, account_id, platform_thread_id, title, created_at
            FROM threads
            WHERE account_id=? AND id=?
            """,
            (account_id, thread_id),
        ).fetchone()
        if not row:
            raise KeyError(f"thread {thread_id} not found for account {account_id}")
        return row

    def ops_counts(self) -> dict[str, int]:
        return {
            "accounts": int(self._conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]),
            "threads": int(self._conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]),
            "messages": int(self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]),
            "pending_approvals": int(
                self._conn.execute("SELECT COUNT(*) FROM send_approvals WHERE state='approved'").fetchone()[0]
            ),
            "outbound_sends": int(self._conn.execute("SELECT COUNT(*) FROM outbound_sends").fetchone()[0]),
        }

    def get_account_ops_health(self, account_id: int) -> dict[str, Any]:
        account = self._ensure_account_exists(account_id)
        auth = self.get_account_auth(account_id)
        has_browser_context = self.get_browser_context(account_id) is not None
        counts = self._conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM threads WHERE account_id=?) AS threads,
              (SELECT COUNT(*) FROM messages WHERE account_id=?) AS messages,
              (SELECT COUNT(*) FROM outbound_sends WHERE account_id=?) AS outbound_sends,
              (SELECT COUNT(*) FROM send_approvals WHERE account_id=? AND state='approved') AS pending_approvals
            """,
            (account_id, account_id, account_id, account_id),
        ).fetchone()
        send_summary = self._conn.execute(
            """
            SELECT status, COUNT(*) AS count, MAX(updated_at) AS last_at
            FROM outbound_sends
            WHERE account_id=?
            GROUP BY status
            """,
            (account_id,),
        ).fetchall()
        last_sync = self._conn.execute(
            """
            SELECT event_type, redacted_payload_json, created_at
            FROM ops_audit_events
            WHERE account_id=? AND event_type IN ('sync.completed', 'sync.ingest', 'sync.failed')
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        return {
            "account_id": account_id,
            "label": account["label"],
            "status": "ok" if auth.li_at else "failed",
            "checked_at": utcnow().isoformat(),
            "session": {
                "has_li_at": bool(auth.li_at),
                "has_jsessionid": bool(auth.jsessionid),
                "has_browser_context": has_browser_context,
                "expires_at": None,
            },
            "proxy": {"configured": bool(account["proxy_json"])},
            "counts": {key: int(counts[key]) for key in counts.keys()},
            "outbound_summary": {r["status"]: {"count": int(r["count"]), "last_at": r["last_at"]} for r in send_summary},
            "last_sync": dict(last_sync) if last_sync else None,
            "error": None if auth.li_at else "missing li_at cookie",
            "next_action": None if auth.li_at else "refresh_account",
        }

    def _thread_summaries(self, *, account_id: int, limit: int, cursor: str | None) -> dict[str, Any]:
        self._ensure_account_exists(account_id)
        offset = _offset_from_cursor(cursor)
        rows = self._conn.execute(
            """
            SELECT
              t.id,
              t.platform_thread_id,
              t.title,
              t.created_at,
              COUNT(m.id) AS message_count,
              MAX(m.sent_at) AS last_message_at,
              (
                SELECT m2.text FROM messages m2
                WHERE m2.thread_id=t.id
                ORDER BY m2.sent_at DESC, m2.id DESC LIMIT 1
              ) AS last_message_text,
              (
                SELECT m2.direction FROM messages m2
                WHERE m2.thread_id=t.id
                ORDER BY m2.sent_at DESC, m2.id DESC LIMIT 1
              ) AS last_direction,
              (
                SELECT c.cursor FROM sync_cursors c
                WHERE c.account_id=t.account_id AND c.thread_id=t.id
              ) AS sync_cursor
            FROM threads t
            LEFT JOIN messages m ON m.thread_id=t.id
            WHERE t.account_id=?
            GROUP BY t.id
            ORDER BY COALESCE(last_message_at, t.created_at) DESC, t.id DESC
            LIMIT ? OFFSET ?
            """,
            (account_id, limit, offset),
        ).fetchall()
        threads: list[dict[str, Any]] = []
        for row in rows:
            threads.append({
                "thread_id": int(row["id"]),
                "id": int(row["id"]),
                "platform_thread_id": row["platform_thread_id"],
                "title": row["title"],
                "created_at": row["created_at"],
                "last_message_at": row["last_message_at"],
                "last_message_preview": _safe_text(row["last_message_text"], max_chars=_MAX_PREVIEW_CHARS),
                "last_direction": row["last_direction"],
                "message_count": int(row["message_count"]),
                "unread": False,
                "health": "syncing" if row["sync_cursor"] else "ready",
            })
        return {"threads": threads, "page": _page_meta(limit=limit, offset=offset, returned=len(rows))}

    def list_inbox_threads(self, *, account_id: int, limit: int, cursor: str | None) -> dict[str, Any]:
        return self._thread_summaries(account_id=account_id, limit=limit, cursor=cursor)

    def list_ops_threads(self, *, account_id: int, limit: int, cursor: str | None) -> dict[str, Any]:
        page = self._thread_summaries(account_id=account_id, limit=limit, cursor=cursor)
        slim = []
        for thread in page["threads"]:
            slim.append({
                "id": thread["id"],
                "platform_thread_id": thread["platform_thread_id"],
                "title": thread["title"],
                "created_at": thread["created_at"],
                "message_count": thread["message_count"],
                "last_message_at": thread["last_message_at"],
            })
        return {"threads": slim, "page": page["page"]}

    def get_thread_detail(self, *, account_id: int, thread_id: int) -> dict[str, Any]:
        row = self._thread_row(account_id=account_id, thread_id=thread_id)
        counts = self._conn.execute(
            "SELECT COUNT(*) AS message_count, MAX(sent_at) AS last_message_at FROM messages WHERE account_id=? AND thread_id=?",
            (account_id, thread_id),
        ).fetchone()
        return {
            "id": int(row["id"]),
            "platform_thread_id": row["platform_thread_id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "message_count": int(counts["message_count"]),
            "last_message_at": counts["last_message_at"],
        }

    def list_thread_messages(
        self,
        *,
        account_id: int,
        thread_id: int,
        limit: int,
        cursor: str | None,
        include_raw_text: bool = False,
    ) -> dict[str, Any]:
        self._thread_row(account_id=account_id, thread_id=thread_id)
        offset = _offset_from_cursor(cursor)
        rows = self._conn.execute(
            """
            SELECT id, platform_message_id, direction, sender, text, sent_at, raw_json
            FROM messages
            WHERE account_id=? AND thread_id=?
            ORDER BY sent_at ASC, id ASC
            LIMIT ? OFFSET ?
            """,
            (account_id, thread_id, limit, offset),
        ).fetchall()
        messages = []
        for row in rows:
            text = row["text"] if include_raw_text else _safe_text(row["text"])
            messages.append({
                "id": int(row["id"]),
                "platform_message_id": row["platform_message_id"],
                "direction": row["direction"],
                "sender": _safe_text(row["sender"]),
                "text": text,
                "sent_at": row["sent_at"],
                "raw_available": bool(row["raw_json"]),
            })
        return {
            "messages": messages,
            "page": _page_meta(limit=limit, offset=offset, returned=len(rows)),
        }

    def search_messages(
        self,
        *,
        account_id: int,
        query: str,
        direction: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        self._ensure_account_exists(account_id)
        if not query.strip():
            raise ValueError("query must not be empty")
        if direction is not None and direction not in {"in", "out"}:
            raise ValueError("direction must be 'in' or 'out'")
        offset = _offset_from_cursor(cursor)
        clauses = ["m.account_id=?", "LOWER(COALESCE(m.text, '')) LIKE ?"]
        params: list[Any] = [account_id, f"%{query.lower()}%"]
        if direction:
            clauses.append("m.direction=?")
            params.append(direction)
        if from_date:
            clauses.append("date(m.sent_at) >= date(?)")
            params.append(from_date)
        if to_date:
            clauses.append("date(m.sent_at) <= date(?)")
            params.append(to_date)
        params.extend([limit, offset])
        rows = self._conn.execute(
            f"""
            SELECT m.id, m.thread_id, m.platform_message_id, m.direction, m.sender, m.text, m.sent_at, t.title
            FROM messages m
            JOIN threads t ON t.id=m.thread_id
            WHERE {' AND '.join(clauses)}
            ORDER BY m.sent_at DESC, m.id DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params),
        ).fetchall()
        results = []
        for row in rows:
            results.append({
                "message_id": int(row["id"]),
                "thread_id": int(row["thread_id"]),
                "platform_message_id": row["platform_message_id"],
                "title": row["title"],
                "direction": row["direction"],
                "sender": _safe_text(row["sender"]),
                "text_snippet": _safe_text(row["text"], max_chars=_MAX_PREVIEW_CHARS),
                "sent_at": row["sent_at"],
            })
        return {
            "results": results,
            "page": _page_meta(limit=limit, offset=offset, returned=len(rows)),
            "fts": False,
        }

    def record_ops_audit_event(
        self,
        *,
        account_id: int | None,
        event_type: str,
        actor: str,
        entity_type: str | None,
        entity_id: str | None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        if account_id is not None:
            self._ensure_account_exists(account_id)
        now = utcnow().isoformat()
        safe_payload = redact_for_log(payload or {})
        cur = self._conn.execute(
            """
            INSERT INTO ops_audit_events(account_id, event_type, actor, entity_type, entity_id, redacted_payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (account_id, event_type, actor, entity_type, entity_id, json.dumps(safe_payload, sort_keys=True, default=str), now),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_ops_audit(
        self,
        *,
        account_id: int,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_account_exists(account_id)
        offset = _offset_from_cursor(cursor)
        clauses = ["account_id=?"]
        params: list[Any] = [account_id]
        if from_date:
            clauses.append("date(created_at) >= date(?)")
            params.append(from_date)
        if to_date:
            clauses.append("date(created_at) <= date(?)")
            params.append(to_date)
        params.extend([limit, offset])
        audit_rows = self._conn.execute(
            f"""
            SELECT id, account_id, event_type, actor, entity_type, entity_id, redacted_payload_json, created_at
            FROM ops_audit_events
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params),
        ).fetchall()
        send_rows = self._conn.execute(
            """
            SELECT id, account_id, status, recipient, idempotency_key, attempts, platform_message_id, last_error, created_at, updated_at
            FROM outbound_sends
            WHERE account_id=?
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (account_id, limit),
        ).fetchall()
        events = [
            {
                "id": int(row["id"]),
                "account_id": row["account_id"],
                "event_type": row["event_type"],
                "actor": row["actor"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "payload": json.loads(row["redacted_payload_json"] or "{}"),
                "created_at": row["created_at"],
            }
            for row in audit_rows
        ]
        outbound = [dict(row) for row in send_rows]
        for row in outbound:
            if row.get("last_error"):
                row["last_error"] = redact_string(row["last_error"])
        return {"events": events, "outbound_sends": outbound, "page": _page_meta(limit=limit, offset=offset, returned=len(audit_rows))}

    # ------------------------------------------------------------------
    # Draft and approval state
    # ------------------------------------------------------------------

    _VALID_DRAFT_STATES = frozenset({"draft", "approved", "revoked", "sent"})
    _VALID_APPROVAL_STATES = frozenset({"draft", "approved", "revoked", "used"})

    def create_draft_reply(
        self,
        *,
        account_id: int,
        thread_id: int | None,
        recipient: str,
        text: str,
        campaign_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_account_exists(account_id)
        if thread_id is not None:
            self._thread_row(account_id=account_id, thread_id=thread_id)
        now = utcnow().isoformat()
        text_hash = _text_sha256(text)
        cur = self._conn.execute(
            """
            INSERT INTO draft_replies(account_id, thread_id, recipient, text, text_sha256, campaign_id, idempotency_key, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (account_id, thread_id, recipient, text, text_hash, campaign_id, idempotency_key, now, now),
        )
        draft_id = int(cur.lastrowid)
        approval_id = f"appr_{utcnow().strftime('%Y%m%d')}_{draft_id:06d}"
        self._conn.execute(
            """
            INSERT INTO send_approvals(approval_id, draft_id, account_id, recipient, text_sha256, idempotency_key, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (approval_id, draft_id, account_id, recipient, text_hash, idempotency_key, now, now),
        )
        self._conn.commit()
        self.record_ops_audit_event(
            account_id=account_id,
            event_type="draft.created",
            actor="operator",
            entity_type="draft_reply",
            entity_id=str(draft_id),
            payload={"draft_id": draft_id, "approval_id": approval_id, "text_sha256": text_hash},
        )
        return {
            "ok": True,
            "draft_id": draft_id,
            "approval_id": approval_id,
            "approval_state": "draft",
            "account_id": account_id,
            "thread_id": thread_id,
            "recipient": recipient,
            "text_sha256": text_hash,
            "preview": _safe_text(text, max_chars=_MAX_PREVIEW_CHARS),
            "idempotency_key": idempotency_key,
            "external_writes": 0,
        }

    def _draft_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "account_id": int(row["account_id"]),
            "thread_id": row["thread_id"],
            "recipient": row["recipient"],
            "text_sha256": row["text_sha256"],
            "campaign_id": row["campaign_id"],
            "idempotency_key": row["idempotency_key"],
            "state": row["state"],
            "preview": _safe_text(row["text"], max_chars=_MAX_PREVIEW_CHARS),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_drafts(self, *, account_id: int, state: str | None, limit: int, cursor: str | None) -> dict[str, Any]:
        self._ensure_account_exists(account_id)
        if state is not None and state not in self._VALID_DRAFT_STATES:
            raise ValueError("invalid draft state")
        offset = _offset_from_cursor(cursor)
        if state:
            rows = self._conn.execute(
                "SELECT * FROM draft_replies WHERE account_id=? AND state=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (account_id, state, limit, offset),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM draft_replies WHERE account_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (account_id, limit, offset),
            ).fetchall()
        return {"drafts": [self._draft_payload(r) for r in rows], "page": _page_meta(limit=limit, offset=offset, returned=len(rows))}

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM send_approvals WHERE approval_id=?", (approval_id,)).fetchone()
        return dict(row) if row else None

    def _set_approval_state(self, approval_id: str, state: str, *, approved_by: str | None = None) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM send_approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if not row:
            raise KeyError(f"approval {approval_id} not found")
        now = utcnow().isoformat()
        if state == "approved":
            self._conn.execute(
                "UPDATE send_approvals SET state='approved', approved_by=?, approved_at=?, revoked_at=NULL, updated_at=? WHERE approval_id=?",
                (approved_by, now, now, approval_id),
            )
            self._conn.execute("UPDATE draft_replies SET state='approved', updated_at=? WHERE id=?", (now, row["draft_id"]))
        elif state == "revoked":
            self._conn.execute(
                "UPDATE send_approvals SET state='revoked', revoked_at=?, updated_at=? WHERE approval_id=?",
                (now, now, approval_id),
            )
            self._conn.execute("UPDATE draft_replies SET state='revoked', updated_at=? WHERE id=?", (now, row["draft_id"]))
        elif state == "used":
            self._conn.execute(
                "UPDATE send_approvals SET state='used', updated_at=? WHERE approval_id=?",
                (now, approval_id),
            )
            self._conn.execute("UPDATE draft_replies SET state='sent', updated_at=? WHERE id=?", (now, row["draft_id"]))
        else:
            raise ValueError("invalid approval state")
        self._conn.commit()
        updated = self.get_approval(approval_id)
        assert updated is not None
        self.record_ops_audit_event(
            account_id=updated["account_id"],
            event_type=f"approval.{state}",
            actor=approved_by or "operator",
            entity_type="send_approval",
            entity_id=approval_id,
            payload={"approval_id": approval_id, "state": state},
        )
        return updated

    def approve_send_approval(self, approval_id: str, approved_by: str | None = None) -> dict[str, Any]:
        return self._set_approval_state(approval_id, "approved", approved_by=approved_by)

    def revoke_send_approval(self, approval_id: str) -> dict[str, Any]:
        return self._set_approval_state(approval_id, "revoked")

    def mark_approval_used(self, approval_id: str) -> dict[str, Any]:
        return self._set_approval_state(approval_id, "used")

    def list_approvals(self, *, account_id: int, state: str | None, limit: int, cursor: str | None) -> dict[str, Any]:
        self._ensure_account_exists(account_id)
        if state is not None and state not in self._VALID_APPROVAL_STATES:
            raise ValueError("invalid approval state")
        offset = _offset_from_cursor(cursor)
        if state:
            rows = self._conn.execute(
                "SELECT * FROM send_approvals WHERE account_id=? AND state=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (account_id, state, limit, offset),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM send_approvals WHERE account_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (account_id, limit, offset),
            ).fetchall()
        approvals = [dict(r) for r in rows]
        return {"approvals": approvals, "page": _page_meta(limit=limit, offset=offset, returned=len(rows))}

    def validate_send_approval(
        self,
        *,
        approval_id: str,
        account_id: int,
        recipient: str,
        text: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        approval = self.get_approval(approval_id)
        if not approval or approval["state"] != "approved":
            raise ValueError("approval_required")
        if int(approval["account_id"]) != account_id:
            raise ValueError("approval_account_mismatch")
        if approval["recipient"] != recipient:
            raise ValueError("approval_recipient_mismatch")
        if approval["text_sha256"] != _text_sha256(text):
            raise ValueError("approval_text_mismatch")
        if idempotency_key is not None and approval["idempotency_key"] != idempotency_key:
            raise ValueError("approval_idempotency_mismatch")
        return approval

    def campaign_status(self, *, campaign_id: int, account_id: int | None = None) -> dict[str, Any] | None:
        if account_id is not None:
            self._ensure_account_exists(account_id)
            row = self._conn.execute("SELECT * FROM campaigns WHERE id=? AND account_id=?", (campaign_id, account_id)).fetchone()
        else:
            row = self._conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not row:
            return None
        totals = self._conn.execute(
            "SELECT state, COUNT(*) AS count FROM campaign_recipients WHERE campaign_id=? GROUP BY state",
            (campaign_id,),
        ).fetchall()
        total_map = {"prospects": 0, "drafted": 0, "approved": 0, "sent": 0, "failed": 0, "skipped": 0}
        for r in totals:
            state = r["state"]
            if state in total_map:
                total_map[state] = int(r["count"])
            if state in {"draft", "drafted"}:
                total_map["drafted"] += int(r["count"])
            total_map["prospects"] += int(r["count"])
        sent_today = int(self._conn.execute(
            "SELECT COUNT(*) FROM outbound_sends WHERE account_id=? AND status='sent' AND date(updated_at)=date('now')",
            (row["account_id"],),
        ).fetchone()[0])
        daily_cap = int(row["rate_limit_daily_cap"])
        return {
            "campaign_id": int(row["id"]),
            "account_id": int(row["account_id"]),
            "state": row["state"],
            "totals": total_map,
            "rate_limit": {
                "daily_cap": daily_cap,
                "sent_today": sent_today,
                "remaining_today": max(daily_cap - sent_today, 0),
                "next_safe_send_at": None,
            },
            "last_run": None,
        }

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
