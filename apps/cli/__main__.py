"""CLI entrypoint: sync and send without running FastAPI.

Run from repo root (or installed package): ``python -m apps.cli sync --account-id 1``
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

import httpx

from libs.core.job_runner import run_send, run_sync, SendResult, SyncResult
from libs.core.models import AccountAuth, ProxyConfig
from libs.core.redaction import configure_logging
from libs.core.storage import Storage
from libs.providers.linkedin.provider import LinkedInProvider

logger = logging.getLogger(__name__)

_PROVIDER_TODO = "Provider not implemented. Implement libs/providers/linkedin/provider.py"

_SEND_TEXT_MAX_LEN = 8000


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


def _open_storage(db_path: str | None) -> Storage:
    if db_path is None:
        return Storage()
    return Storage(db_path=Path(db_path))


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m apps.cli",
        description="Run sync/send against local SQLite storage (no web server).",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="Fetch threads and messages into storage")
    p_sync.add_argument(
        "--db-path",
        metavar="PATH",
        default=None,
        help="SQLite database file (default: ./desearch_linkedin_dms.sqlite)",
    )
    p_sync.add_argument("--account-id", type=int, required=True, metavar="ID")
    p_sync.add_argument(
        "--limit-per-thread",
        type=int,
        default=50,
        metavar="N",
        help="Messages per provider page (default: 50, max: 500)",
    )
    p_sync.add_argument(
        "--max-pages-per-thread",
        type=int,
        default=None,
        metavar="N",
        help="Max pages per thread (default: 1). Incompatible with --exhaust-pagination.",
    )
    p_sync.add_argument(
        "--exhaust-pagination",
        action="store_true",
        help="Follow cursors until exhausted (same as API max_pages_per_thread=null)",
    )

    p_send = sub.add_parser("send", help="Send one DM via the LinkedIn provider")
    p_send.add_argument(
        "--db-path",
        metavar="PATH",
        default=None,
        help="SQLite database file (default: ./desearch_linkedin_dms.sqlite)",
    )
    p_send.add_argument("--account-id", type=int, required=True, metavar="ID")
    p_send.add_argument("--recipient", required=True, metavar="URN_OR_CONV_ID")
    p_send.add_argument("--text", required=True, metavar="BODY")
    p_send.add_argument(
        "--idempotency-key",
        default=None,
        metavar="KEY",
        help="Optional idempotency key (same as API)",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "sync":
        if args.exhaust_pagination and args.max_pages_per_thread is not None:
            parser.error("cannot combine --exhaust-pagination with --max-pages-per-thread")
        if not (1 <= args.limit_per_thread <= 500):
            parser.error("--limit-per-thread must be between 1 and 500")
        max_pages: int | None
        if args.exhaust_pagination:
            max_pages = None
        elif args.max_pages_per_thread is not None:
            if not (1 <= args.max_pages_per_thread <= 100):
                parser.error("--max-pages-per-thread must be between 1 and 100")
            max_pages = args.max_pages_per_thread
        else:
            max_pages = 1
        args._resolved_max_pages = max_pages  # type: ignore[attr-defined]

    return args


def _account_must_exist(storage: Storage, account_id: int) -> tuple[AccountAuth, ProxyConfig | None]:
    if account_id < 1:
        raise ValueError("account id must be a positive integer")
    auth = storage.get_account_auth(account_id)
    proxy = storage.get_account_proxy(account_id)
    return auth, proxy


def _load_provider(storage: Storage, account_id: int) -> LinkedInProvider | int:
    """Return a provider for ``account_id``, or exit code ``1`` on account errors."""
    try:
        auth, proxy = _account_must_exist(storage, account_id)
    except KeyError:
        _stderr(f"error: account {account_id} not found")
        return 1
    except ValueError as exc:
        _stderr(f"error: {exc}")
        return 1
    return LinkedInProvider(auth=auth, proxy=proxy)


def _cmd_sync(storage: Storage, args: argparse.Namespace) -> int:
    loaded = _load_provider(storage, args.account_id)
    if isinstance(loaded, int):
        return loaded
    provider = loaded
    max_pages: int | None = args._resolved_max_pages  # type: ignore[attr-defined]
    try:
        result: SyncResult = run_sync(
            account_id=args.account_id,
            storage=storage,
            provider=provider,
            limit_per_thread=args.limit_per_thread,
            max_pages_per_thread=max_pages,
        )
    except (NotImplementedError, ValueError):
        _stderr(_PROVIDER_TODO)
        return 1
    except Exception:
        logger.exception("sync failed")
        _stderr("error: sync failed unexpectedly")
        return 1

    payload = {
        "ok": True,
        "synced_threads": result.synced_threads,
        "messages_inserted": result.messages_inserted,
        "messages_skipped_duplicate": result.messages_skipped_duplicate,
        "pages_fetched": result.pages_fetched,
    }
    print(json.dumps(payload))
    return 0


def _cmd_send(storage: Storage, args: argparse.Namespace) -> int:
    recipient = args.recipient
    text = args.text
    if len(recipient) < 1:
        _stderr("error: --recipient must be non-empty")
        return 1
    if len(text) < 1:
        _stderr("error: --text must be non-empty")
        return 1
    if len(text) > _SEND_TEXT_MAX_LEN:
        _stderr(f"error: --text must be at most {_SEND_TEXT_MAX_LEN} characters")
        return 1

    idem = args.idempotency_key
    if idem is not None and len(idem) < 1:
        _stderr("error: --idempotency-key, if provided, must be non-empty")
        return 1

    loaded = _load_provider(storage, args.account_id)
    if isinstance(loaded, int):
        return loaded
    provider = loaded
    try:
        result: SendResult = run_send(
            account_id=args.account_id,
            storage=storage,
            provider=provider,
            recipient=recipient,
            text=text,
            idempotency_key=idem,
        )
    except NotImplementedError:
        _stderr(_PROVIDER_TODO)
        return 1
    except (ValueError, PermissionError, ConnectionError, RuntimeError) as exc:
        _stderr(f"error: {exc}")
        return 1
    except httpx.HTTPStatusError as exc:
        logger.warning("send HTTP error: %s", exc.response.status_code)
        _stderr("error: send failed (HTTP error from LinkedIn)")
        return 1
    except Exception:
        logger.exception("send failed")
        _stderr("error: send failed unexpectedly")
        return 1

    print(json.dumps({
        "ok": True,
        "send_id": result.send_id,
        "platform_message_id": result.platform_message_id,
        "status": result.status,
        "was_duplicate": result.was_duplicate,
    }))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI args, run command, return process exit code (0 = success)."""
    configure_logging()
    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return int(code) if isinstance(code, int) else 1

    storage: Storage | None = None
    try:
        storage = _open_storage(args.db_path)
        storage.migrate()
    except (OSError, sqlite3.Error):
        logger.exception("storage initialization failed")
        _stderr("error: could not open or initialize the database")
        if storage is not None:
            storage.close()
        return 1

    try:
        if args.command == "sync":
            return _cmd_sync(storage, args)
        if args.command == "send":
            return _cmd_send(storage, args)
        _stderr(f"error: unknown command {args.command!r}")
        return 1
    finally:
        storage.close()


if __name__ == "__main__":
    raise SystemExit(main())
