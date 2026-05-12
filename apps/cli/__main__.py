"""CLI entrypoint: sync and send without running FastAPI.

Run from repo root (or installed package): ``python -m apps.cli sync --account-id 1``
"""
from __future__ import annotations

import argparse
import json
import logging
import secrets
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

import httpx

from libs.core.job_runner import run_send, run_sync, SendResult, SyncConfig, SyncResult
from libs.core.models import AccountAuth, ProxyConfig
from libs.core.redaction import configure_logging, redact_string
from libs.core.storage import Storage
from libs.providers.linkedin.provider import LinkedInProvider, MAX_MESSAGES_PER_PAGE
from libs.providers.discord.provider import (
    DEFAULT_SCOPES,
    DiscordAPIError,
    DiscordOAuthConfig,
    DiscordProvider,
)

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

    p_discord = sub.add_parser("discord", help="Live Discord OAuth/app sync commands")
    dsub = p_discord.add_subparsers(dest="discord_command", required=True)

    d_auth_url = dsub.add_parser("auth-url", help="Print Discord OAuth connect URL")
    d_auth_url.add_argument("--db-path", metavar="PATH", default=None)

    dsub.add_parser("auth-status", help="Show connected Discord account/auth state").add_argument("--db-path", metavar="PATH", default=None)

    d_guilds = dsub.add_parser("sync-guilds", help="Fetch OAuth-authorized Discord guilds")
    d_guilds.add_argument("--db-path", metavar="PATH", default=None)
    d_guilds.add_argument("--account-id", type=int, required=True)

    d_channels = dsub.add_parser("sync-channels", help="Fetch bot-authorized channels for a guild")
    d_channels.add_argument("--db-path", metavar="PATH", default=None)
    d_channels.add_argument("--account-id", type=int, required=True)
    d_channels.add_argument("--guild-id", required=True)

    d_messages = dsub.add_parser("sync-messages", help="Fetch bot-authorized messages for a channel")
    d_messages.add_argument("--db-path", metavar="PATH", default=None)
    d_messages.add_argument("--account-id", type=int, required=True)
    d_messages.add_argument("--channel-id", required=True)
    d_messages.add_argument("--limit", type=int, default=50)
    d_messages.add_argument("--before", default=None)
    d_messages.add_argument("--after", default=None)

    d_list_guilds = dsub.add_parser("list-guilds", help="List stored Discord guilds")
    d_list_guilds.add_argument("--db-path", metavar="PATH", default=None)
    d_list_guilds.add_argument("--account-id", type=int, required=True)

    d_list_channels = dsub.add_parser("list-channels", help="List stored Discord channels")
    d_list_channels.add_argument("--db-path", metavar="PATH", default=None)
    d_list_channels.add_argument("--account-id", type=int, required=True)
    d_list_channels.add_argument("--guild-id", default=None)

    d_list_messages = dsub.add_parser("list-messages", help="List/search stored live Discord messages")
    d_list_messages.add_argument("--db-path", metavar="PATH", default=None)
    d_list_messages.add_argument("--account-id", type=int, default=None)
    d_list_messages.add_argument("--channel-id", default=None)
    d_list_messages.add_argument("--q", default=None)
    d_list_messages.add_argument("--limit", type=int, default=100)

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "sync":
        if args.exhaust_pagination and args.max_pages_per_thread is not None:
            parser.error("cannot combine --exhaust-pagination with --max-pages-per-thread")
        if not (1 <= args.limit_per_thread <= MAX_MESSAGES_PER_PAGE):
            parser.error(f"--limit-per-thread must be between 1 and {MAX_MESSAGES_PER_PAGE}")
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
    return LinkedInProvider(auth=auth, proxy=proxy, account_id=account_id)


def _cmd_sync(storage: Storage, args: argparse.Namespace) -> int:
    loaded = _load_provider(storage, args.account_id)
    if isinstance(loaded, int):
        return loaded
    provider = loaded
    max_pages: int | None = args._resolved_max_pages  # type: ignore[attr-defined]
    sync_config = SyncConfig(
        delay_between_threads_s=args.delay_threads,
        delay_between_pages_s=args.delay_pages,
    )
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
        "synced_threads": result.synced_threads,
        "messages_inserted": result.messages_inserted,
        "messages_skipped_duplicate": result.messages_skipped_duplicate,
        "pages_fetched": result.pages_fetched,
        "rate_limited": result.rate_limited,
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



def _discord_config_payload(storage: Storage) -> dict:
    config = DiscordOAuthConfig.from_env()
    return {
        "oauth_configured": not config.missing_oauth(),
        "missing_oauth": config.missing_oauth(),
        "bot_configured": bool(config.bot_token),
        "token_persistence_enabled": storage.discord_token_persistence_enabled(),
        "credential_refs": [
            "DISCORD_SYNC_CLIENT_ID",
            "DISCORD_SYNC_CLIENT_SECRET",
            "DISCORD_SYNC_REDIRECT_URI",
            "DISCORD_SYNC_BOT_TOKEN",
        ],
    }


def _print_json(payload: dict) -> int:
    print(json.dumps(payload))
    return 0


def _cmd_discord(storage: Storage, args: argparse.Namespace) -> int:
    provider = DiscordProvider(config=DiscordOAuthConfig.from_env())
    try:
        command = args.discord_command
        if command == "auth-url":
            state = secrets.token_urlsafe(24)
            storage.create_discord_oauth_state(state)
            try:
                url = provider.config.authorization_url(state=state, scopes=list(DEFAULT_SCOPES))
            except RuntimeError as exc:
                _stderr("error: " + redact_string(str(exc)))
                return 1
            return _print_json({"ok": True, "authorization_url": url, "state": state, "scopes": list(DEFAULT_SCOPES), "config": _discord_config_payload(storage)})

        if command == "auth-status":
            return _print_json({"ok": True, "config": _discord_config_payload(storage), "accounts": storage.list_discord_accounts()})

        if command == "sync-guilds":
            token = storage.get_discord_account_token(args.account_id)
            if not token or not token.get("access_token"):
                _stderr("error: Discord OAuth access token is not persisted; configure DESEARCH_ENCRYPTION_KEY and reconnect")
                return 1
            try:
                guilds = provider.list_user_guilds(token["access_token"])
            except DiscordAPIError as exc:
                storage.record_discord_error(account_id=args.account_id, scope="guilds", message=str(exc), status_code=exc.status_code, route=exc.route)
                _stderr("error: " + str(exc))
                return 1
            for guild in guilds:
                storage.upsert_discord_guild(account_id=args.account_id, guild=guild, provenance="oauth:guilds")
            return _print_json({"ok": True, "upserted": len(guilds)})

        if command == "sync-channels":
            try:
                bot_token = provider.config.require_bot_token()
                channels = provider.list_guild_channels(args.guild_id, bot_token=bot_token)
            except (RuntimeError, DiscordAPIError) as exc:
                storage.record_discord_error(account_id=args.account_id, scope="channels", guild_id=args.guild_id, message=str(exc), status_code=getattr(exc, "status_code", None), route=getattr(exc, "route", None))
                _stderr("error: " + redact_string(str(exc)))
                return 1
            for channel in channels:
                storage.upsert_discord_channel(account_id=args.account_id, guild_id=args.guild_id, channel=channel, provenance="bot:guild_channels")
            return _print_json({"ok": True, "upserted": len(channels)})

        if command == "sync-messages":
            try:
                bot_token = provider.config.require_bot_token()
                messages = provider.list_channel_messages(args.channel_id, bot_token=bot_token, limit=args.limit, before=args.before, after=args.after)
            except (RuntimeError, DiscordAPIError, ValueError) as exc:
                storage.record_discord_error(account_id=args.account_id, scope="messages", channel_id=args.channel_id, message=str(exc), status_code=getattr(exc, "status_code", None), route=getattr(exc, "route", None))
                _stderr("error: " + redact_string(str(exc)))
                return 1
            inserted = 0
            duplicates = 0
            for message in messages:
                if storage.insert_discord_message(account_id=args.account_id, channel_id=args.channel_id, message=message, provenance="bot:channel_messages"):
                    inserted += 1
                else:
                    duplicates += 1
            return _print_json({"ok": True, "fetched": len(messages), "inserted": inserted, "duplicates": duplicates})

        if command == "list-guilds":
            return _print_json({"guilds": storage.list_discord_guilds(account_id=args.account_id)})
        if command == "list-channels":
            return _print_json({"channels": storage.list_discord_channels(account_id=args.account_id, guild_id=args.guild_id)})
        if command == "list-messages":
            return _print_json({"messages": storage.list_discord_messages(account_id=args.account_id, channel_id=args.channel_id, q=args.q, limit=args.limit)})

        _stderr(f"error: unknown discord command {command!r}")
        return 1
    finally:
        provider.close()


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
        if args.command == "discord":
            return _cmd_discord(storage, args)
        _stderr(f"error: unknown command {args.command!r}")
        return 1
    finally:
        storage.close()


if __name__ == "__main__":
    raise SystemExit(main())
