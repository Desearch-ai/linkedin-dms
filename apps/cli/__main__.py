"""Safe operator CLI for local LinkedIn DM sync, browsing, drafts, and approved sends.

Run from repo root (or installed package): ``python -m apps.cli status``.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any, Sequence

import httpx

from libs.core.job_runner import run_send, run_sync, SendResult, SyncConfig, SyncResult
from libs.core.models import AccountAuth, ProxyConfig
from libs.core.redaction import configure_logging
from libs.core.storage import Storage
from libs.providers.linkedin.provider import LinkedInProvider, MAX_MESSAGES_PER_PAGE

logger = logging.getLogger(__name__)

_PROVIDER_TODO = "Provider not implemented. Implement libs/providers/linkedin/provider.py"
_SEND_TEXT_MAX_LEN = 8000
_SERVICE_NAME = "linkedin-dms"
_DEFAULT_API_URL = "http://127.0.0.1:8899"


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


def _json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _service_version() -> str:
    try:
        return metadata.version("desearch-dms")
    except metadata.PackageNotFoundError:
        return "0.0.1"


def _open_storage(db_path: str | None) -> Storage:
    if db_path is None:
        return Storage()
    return Storage(db_path=Path(db_path))


def _add_db_path(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db-path",
        metavar="PATH",
        default=None,
        help="SQLite database file (default: ./desearch_linkedin_dms.sqlite)",
    )


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON (default for this CLI)")


def _add_pagination(parser: argparse.ArgumentParser, *, default_limit: int = 25) -> None:
    parser.add_argument("--limit", type=int, default=default_limit, metavar="N")
    parser.add_argument("--cursor", default=None, metavar="CURSOR")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m apps.cli",
        description="Safe local operator CLI for LinkedIn DM sync, browsing, drafts, and approved sends.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Show local service/database status")
    _add_db_path(p_status)
    _add_json_flag(p_status)

    p_auth = sub.add_parser("auth", help="Account authentication checks")
    auth_sub = p_auth.add_subparsers(dest="auth_command", required=True)
    p_auth_status = auth_sub.add_parser("status", help="Show stored auth readiness for an account")
    _add_db_path(p_auth_status)
    p_auth_status.add_argument("--account-id", type=int, required=True, metavar="ID")
    _add_json_flag(p_auth_status)

    p_sync = sub.add_parser("sync", help="Fetch threads and messages into storage")
    _add_db_path(p_sync)
    p_sync.add_argument("--account-id", type=int, required=True, metavar="ID")
    p_sync.add_argument(
        "--limit-per-thread",
        type=int,
        default=50,
        metavar="N",
        help=f"Messages per provider page (default: 50, max: {MAX_MESSAGES_PER_PAGE})",
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
    p_sync.add_argument(
        "--delay-threads",
        type=float,
        default=2.0,
        metavar="SEC",
        help="Seconds to pause between threads (default: 2.0)",
    )
    p_sync.add_argument(
        "--delay-pages",
        type=float,
        default=1.5,
        metavar="SEC",
        help="Seconds to pause between fetch_messages pages (default: 1.5)",
    )
    p_sync.add_argument("--dry-run", action="store_true", help="Validate account/options without reading LinkedIn or writing storage")

    p_inbox = sub.add_parser("inbox", help="List stored inbox threads")
    _add_db_path(p_inbox)
    p_inbox.add_argument("--account-id", type=int, required=True, metavar="ID")
    _add_pagination(p_inbox, default_limit=25)
    p_inbox.add_argument("--unread", "--unread-only", dest="unread_only", action="store_true", help="Only include locally unread threads (currently none are marked unread)")
    _add_json_flag(p_inbox)

    p_search = sub.add_parser("search", help="Search stored message text locally")
    _add_db_path(p_search)
    p_search.add_argument("--account-id", type=int, required=True, metavar="ID")
    p_search.add_argument("--query", "-q", required=True, metavar="TEXT")
    p_search.add_argument("--from", dest="from_date", default=None, metavar="YYYY-MM-DD")
    p_search.add_argument("--to", dest="to_date", default=None, metavar="YYYY-MM-DD")
    p_search.add_argument("--direction", choices=["in", "out"], default=None)
    _add_pagination(p_search, default_limit=25)
    _add_json_flag(p_search)

    p_threads = sub.add_parser("threads", help="List or inspect stored threads")
    threads_sub = p_threads.add_subparsers(dest="threads_command", required=True)
    p_threads_list = threads_sub.add_parser("list", help="List stored threads")
    _add_db_path(p_threads_list)
    p_threads_list.add_argument("--account-id", type=int, required=True, metavar="ID")
    _add_pagination(p_threads_list, default_limit=25)
    _add_json_flag(p_threads_list)
    p_threads_show = threads_sub.add_parser("show", help="Show a stored thread")
    _add_db_path(p_threads_show)
    p_threads_show.add_argument("--account-id", type=int, required=True, metavar="ID")
    p_threads_show.add_argument("--thread-id", type=int, required=True, metavar="ID")
    p_threads_show.add_argument("--include-messages", action="store_true")
    p_threads_show.add_argument("--limit", type=int, default=50, metavar="N")
    _add_json_flag(p_threads_show)

    p_messages = sub.add_parser("messages", help="Inspect stored messages")
    messages_sub = p_messages.add_subparsers(dest="messages_command", required=True)
    p_messages_list = messages_sub.add_parser("list", help="List stored messages for one thread")
    _add_db_path(p_messages_list)
    p_messages_list.add_argument("--account-id", type=int, required=True, metavar="ID")
    p_messages_list.add_argument("--thread-id", type=int, required=True, metavar="ID")
    _add_pagination(p_messages_list, default_limit=50)
    _add_json_flag(p_messages_list)

    p_draft = sub.add_parser("draft-reply", help="Create a local reply draft and approval candidate without sending")
    _add_db_path(p_draft)
    p_draft.add_argument("--account-id", type=int, required=True, metavar="ID")
    target = p_draft.add_mutually_exclusive_group(required=True)
    target.add_argument("--thread-id", type=int, metavar="ID")
    target.add_argument("--recipient", metavar="URN_OR_CONV_ID")
    body = p_draft.add_mutually_exclusive_group(required=True)
    body.add_argument("--text", metavar="BODY")
    body.add_argument("--text-file", metavar="PATH")
    p_draft.add_argument("--campaign-id", type=int, default=None, metavar="ID")
    p_draft.add_argument("--idempotency-key", default=None, metavar="KEY")
    _add_json_flag(p_draft)

    p_campaign = sub.add_parser("campaign", help="Inspect or dry-run local campaign state")
    campaign_sub = p_campaign.add_subparsers(dest="campaign_command", required=True)
    p_campaign_status = campaign_sub.add_parser("status", help="Show local campaign status")
    _add_db_path(p_campaign_status)
    p_campaign_status.add_argument("--campaign-id", type=int, required=True, metavar="ID")
    p_campaign_status.add_argument("--account-id", type=int, default=None, metavar="ID")
    _add_json_flag(p_campaign_status)
    p_campaign_run = campaign_sub.add_parser("run", help="Build a local campaign run plan")
    _add_db_path(p_campaign_run)
    p_campaign_run.add_argument("--campaign-id", type=int, required=True, metavar="ID")
    p_campaign_run.add_argument("--account-id", type=int, required=True, metavar="ID")
    p_campaign_run.add_argument("--dry-run", action="store_true", help="Required; no external writes are performed")
    p_campaign_run.add_argument("--limit", type=int, default=25, metavar="N")
    _add_json_flag(p_campaign_run)

    p_send = sub.add_parser("send", help="Send one DM only with approved local evidence")
    _add_db_path(p_send)
    p_send.add_argument("--approved", default=None, metavar="APPROVAL_ID", help="Required approval id in state approved")
    p_send.add_argument("--account-id", type=int, required=True, metavar="ID")
    p_send.add_argument("--recipient", default=None, metavar="URN_OR_CONV_ID")
    send_body = p_send.add_mutually_exclusive_group()
    send_body.add_argument("--text", default=None, metavar="BODY")
    send_body.add_argument("--draft-id", type=int, default=None, metavar="ID")
    p_send.add_argument("--idempotency-key", default=None, metavar="KEY")

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "sync":
        if args.exhaust_pagination and args.max_pages_per_thread is not None:
            parser.error("cannot combine --exhaust-pagination with --max-pages-per-thread")
        if not (1 <= args.limit_per_thread <= MAX_MESSAGES_PER_PAGE):
            parser.error(f"--limit-per-thread must be between 1 and {MAX_MESSAGES_PER_PAGE}")
        if args.delay_threads < 0 or args.delay_pages < 0:
            parser.error("sync delays must be non-negative")
        if args.exhaust_pagination:
            max_pages: int | None = None
        elif args.max_pages_per_thread is not None:
            if not (1 <= args.max_pages_per_thread <= 100):
                parser.error("--max-pages-per-thread must be between 1 and 100")
            max_pages = args.max_pages_per_thread
        else:
            max_pages = 1
        args._resolved_max_pages = max_pages  # type: ignore[attr-defined]

    for attr in ("limit",):
        if hasattr(args, attr) and getattr(args, attr) is not None and not (1 <= getattr(args, attr) <= 100):
            parser.error(f"--{attr} must be between 1 and 100")

    return args


def _account_must_exist(storage: Storage, account_id: int) -> tuple[AccountAuth, ProxyConfig | None]:
    if account_id < 1:
        raise ValueError("account id must be a positive integer")
    auth = storage.get_account_auth(account_id)
    proxy = storage.get_account_proxy(account_id)
    return auth, proxy


def _account_exists(storage: Storage, account_id: int) -> bool:
    try:
        storage.get_account_ops_health(account_id)
    except KeyError:
        return False
    return True


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
    return LinkedInProvider(auth=auth, proxy=proxy, account_id=account_id)


def _read_text_arg(args: argparse.Namespace) -> str:
    text = args.text
    if text is None and getattr(args, "text_file", None):
        text = Path(args.text_file).read_text()
    if text is None:
        raise ValueError("--text or --text-file is required")
    if len(text) < 1:
        raise ValueError("--text must be non-empty")
    if len(text) > _SEND_TEXT_MAX_LEN:
        raise ValueError(f"--text must be at most {_SEND_TEXT_MAX_LEN} characters")
    return text


def _cmd_status(storage: Storage, args: argparse.Namespace) -> int:
    payload = {
        "ok": True,
        "service": _SERVICE_NAME,
        "version": _service_version(),
        "db": {
            "path": str(args.db_path or "./desearch_linkedin_dms.sqlite"),
            "reachable": True,
            "schema_version": storage._get_schema_version(),
        },
        "api": {
            "configured_url": _DEFAULT_API_URL,
            "reachable": None,
            "auth_required": True,
        },
        "counts": storage.ops_counts(),
        "warnings": [],
    }
    _json(payload)
    return 0


def _cmd_auth_status(storage: Storage, args: argparse.Namespace) -> int:
    try:
        health = storage.get_account_ops_health(args.account_id)
    except KeyError:
        _stderr(f"error: account {args.account_id} not found")
        return 1
    payload = {
        "ok": health["status"] == "ok",
        "account_id": args.account_id,
        "status": health["status"],
        "checked_at": health["checked_at"],
        "session": health["session"],
        "error": health["error"],
        "next_action": health["next_action"],
    }
    _json(payload)
    return 0 if payload["ok"] else 1


def _cmd_sync(storage: Storage, args: argparse.Namespace) -> int:
    max_pages: int | None = args._resolved_max_pages  # type: ignore[attr-defined]
    if args.dry_run:
        if args.account_id < 1:
            _stderr("error: account id must be a positive integer")
            return 1
        if not _account_exists(storage, args.account_id):
            _stderr(f"error: account {args.account_id} not found")
            return 1
        _json({
            "ok": True,
            "account_id": args.account_id,
            "dry_run": True,
            "planned": {
                "limit_per_thread": args.limit_per_thread,
                "max_pages_per_thread": max_pages,
                "delay_between_threads_s": args.delay_threads,
                "delay_between_pages_s": args.delay_pages,
            },
            "external_reads": 0,
            "external_writes": 0,
            "warnings": [],
        })
        return 0

    loaded = _load_provider(storage, args.account_id)
    if isinstance(loaded, int):
        return loaded
    provider = loaded
    sync_config = SyncConfig(
        delay_between_threads_s=args.delay_threads,
        delay_between_pages_s=args.delay_pages,
    )
    started_at = _now_iso()
    try:
        result: SyncResult = run_sync(
            account_id=args.account_id,
            storage=storage,
            provider=provider,
            limit_per_thread=args.limit_per_thread,
            max_pages_per_thread=max_pages,
            sync_config=sync_config,
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
        "account_id": args.account_id,
        "dry_run": False,
        "synced_threads": result.synced_threads,
        "messages_inserted": result.messages_inserted,
        "messages_skipped_duplicate": result.messages_skipped_duplicate,
        "pages_fetched": result.pages_fetched,
        "rate_limited": result.rate_limited,
        "started_at": started_at,
        "finished_at": _now_iso(),
        "warnings": [],
    }
    _json(payload)
    return 0


def _cmd_inbox(storage: Storage, args: argparse.Namespace) -> int:
    try:
        page = storage.list_inbox_threads(account_id=args.account_id, limit=args.limit, cursor=args.cursor)
    except KeyError:
        _stderr(f"error: account {args.account_id} not found")
        return 1
    except ValueError as exc:
        _stderr(f"error: {exc}")
        return 1
    threads = [t for t in page["threads"] if (not args.unread_only or t["unread"])]
    _json({"ok": True, "account_id": args.account_id, "threads": threads, "page": page["page"]})
    return 0


def _cmd_search(storage: Storage, args: argparse.Namespace) -> int:
    try:
        page = storage.search_messages(
            account_id=args.account_id,
            query=args.query,
            direction=args.direction,
            from_date=args.from_date,
            to_date=args.to_date,
            limit=args.limit,
            cursor=args.cursor,
        )
    except KeyError:
        _stderr(f"error: account {args.account_id} not found")
        return 1
    except ValueError as exc:
        _stderr(f"error: {exc}")
        return 1
    _json({
        "ok": True,
        "account_id": args.account_id,
        "query": args.query,
        "filters": {"from": args.from_date, "to": args.to_date, "direction": args.direction},
        "results": page["results"],
        "page": page["page"],
        "fts": page["fts"],
    })
    return 0


def _cmd_threads(storage: Storage, args: argparse.Namespace) -> int:
    try:
        if args.threads_command == "list":
            page = storage.list_ops_threads(account_id=args.account_id, limit=args.limit, cursor=args.cursor)
            _json({"ok": True, "account_id": args.account_id, "threads": page["threads"], "page": page["page"]})
            return 0
        thread = storage.get_thread_detail(account_id=args.account_id, thread_id=args.thread_id)
        payload: dict[str, Any] = {"ok": True, "account_id": args.account_id, "thread": thread}
        if args.include_messages:
            messages = storage.list_thread_messages(
                account_id=args.account_id,
                thread_id=args.thread_id,
                limit=args.limit,
                cursor=None,
            )
            payload["messages"] = messages["messages"]
            payload["page"] = messages["page"]
        _json(payload)
        return 0
    except KeyError as exc:
        _stderr(f"error: {exc}")
        return 1
    except ValueError as exc:
        _stderr(f"error: {exc}")
        return 1


def _cmd_messages(storage: Storage, args: argparse.Namespace) -> int:
    try:
        page = storage.list_thread_messages(
            account_id=args.account_id,
            thread_id=args.thread_id,
            limit=args.limit,
            cursor=args.cursor,
        )
    except KeyError as exc:
        _stderr(f"error: {exc}")
        return 1
    except ValueError as exc:
        _stderr(f"error: {exc}")
        return 1
    _json({"ok": True, "account_id": args.account_id, "thread_id": args.thread_id, "messages": page["messages"], "page": page["page"]})
    return 0


def _cmd_draft_reply(storage: Storage, args: argparse.Namespace) -> int:
    try:
        text = _read_text_arg(args)
        if args.idempotency_key is not None and len(args.idempotency_key) < 1:
            raise ValueError("--idempotency-key, if provided, must be non-empty")
        recipient = args.recipient
        if args.thread_id is not None and recipient is None:
            recipient = storage.get_thread_detail(account_id=args.account_id, thread_id=args.thread_id)["platform_thread_id"]
        if not recipient:
            raise ValueError("recipient must be non-empty")
        result = storage.create_draft_reply(
            account_id=args.account_id,
            thread_id=args.thread_id,
            recipient=recipient,
            text=text,
            campaign_id=args.campaign_id,
            idempotency_key=args.idempotency_key,
        )
    except (KeyError, ValueError, OSError) as exc:
        _stderr(f"error: {exc}")
        return 1
    _json(result)
    return 0


def _empty_campaign_payload(campaign_id: int, account_id: int | None) -> dict[str, Any]:
    return {
        "ok": True,
        "campaign_id": campaign_id,
        "account_id": account_id,
        "state": "not_configured",
        "totals": {"prospects": 0, "drafted": 0, "approved": 0, "sent": 0, "failed": 0, "skipped": 0},
        "rate_limit": {"daily_cap": 0, "sent_today": 0, "remaining_today": 0, "next_safe_send_at": None},
        "last_run": None,
    }


def _cmd_campaign(storage: Storage, args: argparse.Namespace) -> int:
    if args.campaign_command == "status":
        try:
            status = storage.campaign_status(campaign_id=args.campaign_id, account_id=args.account_id)
        except KeyError:
            _stderr(f"error: account {args.account_id} not found")
            return 1
        _json({"ok": True, **(status or _empty_campaign_payload(args.campaign_id, args.account_id))})
        return 0

    if not args.dry_run:
        _stderr("error: campaign run requires --dry-run; live campaign sends are not implemented")
        return 2
    try:
        storage.get_account_ops_health(args.account_id)
    except KeyError:
        _stderr(f"error: account {args.account_id} not found")
        return 1
    _json({
        "ok": True,
        "campaign_id": args.campaign_id,
        "account_id": args.account_id,
        "dry_run": True,
        "external_writes": 0,
        "planned_actions": [],
        "summary": {
            "would_send_if_approved": 0,
            "blocked_not_approved": 0,
            "blocked_rate_limit": 0,
            "blocked_missing_auth": 0,
        },
    })
    return 0


def _send_material_from_args(storage: Storage, args: argparse.Namespace) -> tuple[str, str, str | None]:
    recipient = args.recipient
    text = args.text
    idem = args.idempotency_key
    if args.draft_id is not None:
        draft = storage.get_draft_reply(account_id=args.account_id, draft_id=args.draft_id)
        recipient = recipient or draft["recipient"]
        text = draft["text"]
        idem = idem if idem is not None else draft["idempotency_key"]
    if not recipient:
        raise ValueError("--recipient is required unless --draft-id supplies one")
    if text is None:
        raise ValueError("--text or --draft-id is required")
    if len(recipient) < 1:
        raise ValueError("--recipient must be non-empty")
    if len(text) < 1:
        raise ValueError("--text must be non-empty")
    if len(text) > _SEND_TEXT_MAX_LEN:
        raise ValueError(f"--text must be at most {_SEND_TEXT_MAX_LEN} characters")
    if idem is not None and len(idem) < 1:
        raise ValueError("--idempotency-key, if provided, must be non-empty")
    return recipient, text, idem


def _cmd_send(storage: Storage, args: argparse.Namespace) -> int:
    if not args.approved:
        _stderr("error: approval_required (--approved APPROVAL_ID is required before any live send)")
        return 1
    try:
        recipient, text, idem = _send_material_from_args(storage, args)
        storage.validate_send_approval(
            approval_id=args.approved,
            account_id=args.account_id,
            recipient=recipient,
            text=text,
            idempotency_key=idem,
        )
    except (KeyError, ValueError) as exc:
        _stderr(f"error: {exc}")
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

    storage.mark_approval_used(args.approved)
    _json({
        "ok": True,
        "approved": True,
        "approval_id": args.approved,
        "account_id": args.account_id,
        "send_id": result.send_id,
        "platform_message_id": result.platform_message_id,
        "status": result.status,
        "was_duplicate": result.was_duplicate,
        "idempotency_key": idem,
        "sent_at": _now_iso(),
    })
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
        if args.command == "status":
            return _cmd_status(storage, args)
        if args.command == "auth" and args.auth_command == "status":
            return _cmd_auth_status(storage, args)
        if args.command == "sync":
            return _cmd_sync(storage, args)
        if args.command == "inbox":
            return _cmd_inbox(storage, args)
        if args.command == "search":
            return _cmd_search(storage, args)
        if args.command == "threads":
            return _cmd_threads(storage, args)
        if args.command == "messages":
            return _cmd_messages(storage, args)
        if args.command == "draft-reply":
            return _cmd_draft_reply(storage, args)
        if args.command == "campaign":
            return _cmd_campaign(storage, args)
        if args.command == "send":
            return _cmd_send(storage, args)
        _stderr(f"error: unknown command {args.command!r}")
        return 1
    finally:
        storage.close()


if __name__ == "__main__":
    raise SystemExit(main())
