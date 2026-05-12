from __future__ import annotations

import logging

import httpx
import os
import secrets
from dataclasses import replace
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, model_validator

from libs.core.cookies import cookies_to_account_auth, validate_li_at
from libs.core.job_runner import (
    IngestMessage,
    IngestThread,
    SendResult,
    SyncConfig,
    SyncResult,
    run_ingest,
    run_send,
    run_sync,
)
from libs.core.models import AccountAuth, BrowserContext, ProxyConfig
from libs.core.redaction import configure_logging, redact_for_log, redact_string
from libs.core.storage import Storage
from libs.providers.linkedin.provider import LinkedInProvider, MAX_MESSAGES_PER_PAGE
from libs.providers.discord.provider import (
    DEFAULT_SCOPES,
    DiscordAPIError,
    DiscordOAuthConfig,
    DiscordProvider,
    token_expires_at,
)
from libs.providers.discord.session_provider import DiscordSessionAuth, DiscordSessionProvider

logger = logging.getLogger(__name__)

configure_logging()

app = FastAPI(title="Desearch LinkedIn DMs", version="0.0.2")

storage = Storage()
storage.migrate()


def _get_api_token() -> str | None:
    value = os.getenv("DESEARCH_API_TOKEN")
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def require_api_auth(authorization: str | None = Header(default=None)) -> None:
    expected = _get_api_token()
    if expected is None:
        return

    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token or not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


class AuthCheckResponse(BaseModel):
    status: str
    error: Optional[str] = None


def _provider_http_exception(exc: httpx.HTTPStatusError | ConnectionError) -> HTTPException:
    if isinstance(exc, ConnectionError):
        return HTTPException(
            status_code=503,
            detail=(
                "LinkedIn upstream network error — retry shortly. "
                "If this persists, refresh via POST /accounts/refresh and try again."
            ),
        )

    response = exc.response
    status_code = response.status_code if response is not None else None
    safe_detail = redact_string(str(exc))

    if status_code in (401, 403):
        if status_code == 403:
            detail = (
                "LinkedIn rejected the session — re-authenticate via POST /accounts/refresh and retry."
            )
        else:
            detail = (
                "LinkedIn session expired — re-authenticate via POST /accounts/refresh and retry."
            )
        return HTTPException(status_code=401, detail=detail)

    if status_code in (429, 999):
        headers: dict[str, str] = {}
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after:
            headers["Retry-After"] = retry_after
        detail = "LinkedIn upstream rate limit reached — retry later."
        if retry_after:
            detail = f"{detail} Retry-After: {retry_after}s."
        return HTTPException(status_code=429, detail=detail, headers=headers or None)

    if status_code in (400, 404):
        return HTTPException(
            status_code=502,
            detail=(
                "LinkedIn messaging contract drift detected upstream — refresh the request contract and retry."
            ),
        )

    return HTTPException(
        status_code=502,
        detail=(
            "LinkedIn upstream request failed — retry shortly. "
            f"Upstream detail: {safe_detail}"
        ),
    )


_X_LI_TRACK_DESC = "Browser-captured x-li-track header value (from Chrome extension)"
_CSRF_TOKEN_DESC = "Browser-captured csrf-token header value (from Chrome extension)"


def _merge_browser_context(
    auth: AccountAuth,
    x_li_track: str | None,
    csrf_token: str | None,
) -> AccountAuth:
    """Attach captured header values to an AccountAuth without mutating the caller."""
    if x_li_track is None and csrf_token is None:
        return auth
    return replace(auth, x_li_track=x_li_track, csrf_token=csrf_token)


class AccountCreateIn(BaseModel):
    label: str = Field(..., description="Human label, e.g. 'sales-1'")
    li_at: str | None = Field(None, description="LinkedIn li_at cookie value (required if cookies not provided)")
    jsessionid: str | None = Field(None, description="Optional JSESSIONID cookie value")
    cookies: str | None = Field(
        None,
        description="Cookie header string, e.g. 'li_at=xxx; JSESSIONID=yyy'. Overrides li_at/jsessionid fields.",
    )
    proxy_url: str | None = Field(None, description="Optional proxy URL")
    x_li_track: str | None = Field(None, description=_X_LI_TRACK_DESC)
    csrf_token: str | None = Field(None, description=_CSRF_TOKEN_DESC)

    @model_validator(mode="after")
    def require_auth(self) -> AccountCreateIn:
        if not self.cookies and not self.li_at:
            raise ValueError("Provide either 'cookies' string or 'li_at' field")
        return self

    def to_account_auth(self) -> AccountAuth:
        if self.cookies:
            base = cookies_to_account_auth(self.cookies)
        else:
            base = AccountAuth(li_at=validate_li_at(self.li_at or ""), jsessionid=self.jsessionid)
        return _merge_browser_context(base, self.x_li_track, self.csrf_token)


class AccountRefreshIn(BaseModel):
    account_id: int
    li_at: str | None = Field(None, description="LinkedIn li_at cookie value (required if cookies not provided)")
    jsessionid: str | None = Field(None, description="Optional JSESSIONID cookie value")
    cookies: str | None = Field(
        None,
        description="Cookie header string, e.g. 'li_at=xxx; JSESSIONID=yyy'. Overrides li_at/jsessionid fields.",
    )
    x_li_track: str | None = Field(None, description=_X_LI_TRACK_DESC)
    csrf_token: str | None = Field(None, description=_CSRF_TOKEN_DESC)

    @model_validator(mode="after")
    def require_auth(self) -> AccountRefreshIn:
        if not self.cookies and not self.li_at:
            raise ValueError("Provide either 'cookies' string or 'li_at' field")
        return self

    def to_account_auth(self) -> AccountAuth:
        if self.cookies:
            base = cookies_to_account_auth(self.cookies)
        else:
            base = AccountAuth(li_at=validate_li_at(self.li_at or ""), jsessionid=self.jsessionid)
        return _merge_browser_context(base, self.x_li_track, self.csrf_token)


class SendIn(BaseModel):
    account_id: int
    recipient: str = Field(..., min_length=1, description="Recipient id (profile URN or conversation id)")
    text: str = Field(..., min_length=1, max_length=8000, description="Message body")
    idempotency_key: str | None = None
    x_li_track: str | None = Field(None, description=_X_LI_TRACK_DESC)
    csrf_token: str | None = Field(None, description=_CSRF_TOKEN_DESC)


class IngestMessageIn(BaseModel):
    platform_message_id: str = Field(..., min_length=1)
    direction: str = Field(..., description="'in' or 'out'")
    sender: str | None = None
    text: str | None = None
    sent_at: datetime
    raw: dict | None = None

    @model_validator(mode="after")
    def _check_direction(self) -> IngestMessageIn:
        if self.direction not in ("in", "out"):
            raise ValueError("direction must be 'in' or 'out'")
        return self


class IngestThreadIn(BaseModel):
    platform_thread_id: str = Field(..., min_length=1)
    title: str | None = None
    messages: list[IngestMessageIn] = Field(default_factory=list)


class IngestIn(BaseModel):
    account_id: int
    threads: list[IngestThreadIn]
    pages_fetched: int = Field(0, ge=0)
    rate_limited: bool = False
    messaging_contract: dict | None = Field(
        None,
        description="Captured messaging contract metadata (queryIds + capturedAt). No secrets.",
    )


class SyncIn(BaseModel):
    account_id: int
    limit_per_thread: int = Field(50, ge=1, le=MAX_MESSAGES_PER_PAGE, description="Messages per page")
    max_pages_per_thread: int | None = Field(
        1,
        ge=1,
        le=100,
        description="Max pages per thread (1=MVP); omit or null to exhaust cursor",
    )
    delay_between_threads_s: float = Field(
        2.0, ge=0, le=60, description="Seconds to pause between threads",
    )
    delay_between_pages_s: float = Field(
        1.5, ge=0, le=60, description="Seconds to pause between fetch_messages pages",
    )
    x_li_track: str | None = Field(None, description=_X_LI_TRACK_DESC)
    csrf_token: str | None = Field(None, description=_CSRF_TOKEN_DESC)


class DiscordAccountSyncIn(BaseModel):
    account_id: int = Field(..., ge=1)


class DiscordChannelSyncIn(BaseModel):
    account_id: int = Field(..., ge=1)
    guild_id: str = Field(..., min_length=1)


class DiscordMessageSyncIn(BaseModel):
    account_id: int = Field(..., ge=1)
    channel_id: str = Field(..., min_length=1)
    limit: int = Field(50, ge=1, le=100)
    before: str | None = None
    after: str | None = None


class DiscordSessionConnectIn(BaseModel):
    cookie_header: str | None = Field(
        None,
        description="Approved local Discord Web Cookie header. Prefer session_state_path when possible; never log this value.",
    )
    session_state_path: str | None = Field(
        None,
        description="Local Playwright/browser storage-state JSON path containing discord.com cookies.",
    )
    authorization: str | None = Field(None, description="Optional approved Discord Web authorization value; treated as session material, never bot auth")
    user_agent: str | None = Field(None, description="Optional browser User-Agent captured with the approved session")
    x_super_properties: str | None = Field(None, description="Optional Discord Web X-Super-Properties header")
    locale: str | None = "en-US"

    def to_session_auth(self) -> DiscordSessionAuth:
        return DiscordSessionAuth.from_sources(
            cookie_header=self.cookie_header,
            session_state_path=self.session_state_path,
            authorization=self.authorization,
            user_agent=self.user_agent,
            x_super_properties=self.x_super_properties,
            locale=self.locale,
        )


def _make_discord_provider() -> DiscordProvider:
    return DiscordProvider(config=DiscordOAuthConfig.from_env())


def _make_discord_session_provider(auth: DiscordSessionAuth) -> DiscordSessionProvider:
    return DiscordSessionProvider(auth=auth)


def _discord_config_status() -> dict:
    config = DiscordOAuthConfig.from_env()
    return {
        "oauth_configured": not config.missing_oauth(),
        "missing_oauth": config.missing_oauth(),
        "session_web_supported": True,
        "bot_configured": bool(config.bot_token),
        "optional_bot_configured": bool(config.bot_token),
        "session_persistence_enabled": storage.discord_token_persistence_enabled(),
        "token_persistence_enabled": storage.discord_token_persistence_enabled(),
        "credential_refs": [
            "DESEARCH_ENCRYPTION_KEY",
            "DISCORD_SYNC_CLIENT_ID",
            "DISCORD_SYNC_CLIENT_SECRET",
            "DISCORD_SYNC_REDIRECT_URI",
        ],
    }


def _record_discord_api_error(
    *,
    account_id: int | None,
    scope: str,
    exc: DiscordAPIError | RuntimeError,
    guild_id: str | None = None,
    channel_id: str | None = None,
) -> dict:
    status_code = exc.status_code if isinstance(exc, DiscordAPIError) else None
    route = exc.route if isinstance(exc, DiscordAPIError) else None
    message = redact_string(str(exc))
    storage.record_discord_error(
        account_id=account_id,
        scope=scope,
        guild_id=guild_id,
        channel_id=channel_id,
        status_code=status_code,
        route=route,
        message=message,
    )
    return {"scope": scope, "status_code": status_code, "route": route, "message": message}


def _discord_missing_session_error() -> RuntimeError:
    return RuntimeError(
        "Discord session:web material is not persisted; connect with /discord/session/connect after configuring DESEARCH_ENCRYPTION_KEY, "
        "or provide an approved ephemeral runtime session for validation"
    )


def _discord_account_is_session_web(account: dict) -> bool:
    return "session:web" in account.get("scopes", []) or str(account.get("status") or "").startswith("session_connected")


def _session_auth_from_material(material: dict) -> DiscordSessionAuth:
    return DiscordSessionAuth.from_material(material)


def _session_not_persisted_error(*, account_id: int, scope: str, guild_id: str | None = None, channel_id: str | None = None) -> dict:
    return _record_discord_api_error(
        account_id=account_id,
        scope=scope,
        guild_id=guild_id,
        channel_id=channel_id,
        exc=_discord_missing_session_error(),
    )


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/accounts", dependencies=[Depends(require_api_auth)])
def create_account(body: AccountCreateIn):
    try:
        auth = body.to_account_auth()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=redact_string(str(exc)))
    proxy = ProxyConfig(url=body.proxy_url) if body.proxy_url else None
    account_id = storage.create_account(label=body.label, auth=auth, proxy=proxy)
    logger.info("Account created: %s", redact_for_log({"account_id": account_id, "label": body.label}))
    return {"account_id": account_id}


@app.post("/accounts/refresh", dependencies=[Depends(require_api_auth)])
def refresh_account(body: AccountRefreshIn):
    """Update session cookies for an existing account without recreating it.

    Preserves previously persisted browser-context headers (``x_li_track`` /
    ``csrf_token``) when the refresh payload does not supply fresh values,
    so callers that only rotate cookies don't wipe the persisted fallback
    this account relies on (issue #54).
    """
    try:
        auth = body.to_account_auth()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=redact_string(str(exc)))
    try:
        existing = storage.get_account_auth(body.account_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=redact_string(str(e))) from e

    preserved: dict[str, str] = {}
    if auth.x_li_track is None and existing.x_li_track:
        preserved["x_li_track"] = existing.x_li_track
    if auth.csrf_token is None and existing.csrf_token:
        preserved["csrf_token"] = existing.csrf_token
    if preserved:
        auth = replace(auth, **preserved)

    storage.update_account_auth(body.account_id, auth)
    logger.info("Account refreshed: %s", redact_for_log({"account_id": body.account_id}))
    return {"ok": True, "account_id": body.account_id}


@app.get("/auth/check", response_model=AuthCheckResponse, dependencies=[Depends(require_api_auth)])
def auth_check(account_id: int):
    try:
        auth = storage.get_account_auth(account_id)
        proxy = storage.get_account_proxy(account_id)
    except KeyError:
        return {"status": "failed", "error": "account not found"}

    provider = LinkedInProvider(auth=auth, proxy=proxy)
    result = provider.check_auth()

    if result.ok:
        return {"status": "ok", "error": None}

    return {"status": "failed", "error": result.error or "authentication check failed"}


@app.get("/threads", dependencies=[Depends(require_api_auth)])
def list_threads(account_id: int):
    return {"threads": storage.list_threads(account_id=account_id)}


@app.post("/sync", dependencies=[Depends(require_api_auth)])
def sync_account(body: SyncIn):
    """Trigger a sync. Default one page per thread (MVP); set max_pages_per_thread or null to exhaust."""
    try:
        auth = storage.get_account_auth(body.account_id)
        proxy = storage.get_account_proxy(body.account_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=redact_string(str(e))) from e
    incoming_ctx = BrowserContext(x_li_track=body.x_li_track, csrf_token=body.csrf_token)
    if not incoming_ctx.is_empty():
        storage.update_browser_context(body.account_id, incoming_ctx)
    browser_context = storage.get_browser_context(body.account_id)
    provider = LinkedInProvider(auth=auth, proxy=proxy, account_id=body.account_id, browser_context=browser_context)
    sync_config = SyncConfig(
        delay_between_threads_s=body.delay_between_threads_s,
        delay_between_pages_s=body.delay_between_pages_s,
    )
    try:
        result: SyncResult = run_sync(
            account_id=body.account_id,
            storage=storage,
            provider=provider,
            limit_per_thread=body.limit_per_thread,
            max_pages_per_thread=body.max_pages_per_thread,
            sync_config=sync_config,
            x_li_track=body.x_li_track,
            csrf_token=body.csrf_token,
        )
        return {
            "ok": True,
            "synced_threads": result.synced_threads,
            "messages_inserted": result.messages_inserted,
            "messages_skipped_duplicate": result.messages_skipped_duplicate,
            "pages_fetched": result.pages_fetched,
            "rate_limited": result.rate_limited,
        }
    except PermissionError as exc:
        detail = redact_string(str(exc))
        if "POST /accounts/refresh" not in detail:
            detail = "LinkedIn session expired — re-authenticate via POST /accounts/refresh"
        raise HTTPException(
            status_code=401,
            detail=detail,
        ) from exc
    except (httpx.HTTPStatusError, ConnectionError) as exc:
        raise _provider_http_exception(exc) from exc
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail="Provider not implemented. Implement libs/providers/linkedin/provider.py",
        ) from None
    except (ValueError, RuntimeError) as e:
        raise HTTPException(
            status_code=422,
            detail=redact_string(str(e)),
        ) from None


@app.post("/sync/ingest", dependencies=[Depends(require_api_auth)])
def ingest_sync(body: IngestIn):
    """Ingest extension-captured threads/messages.

    The Chrome extension reads LinkedIn directly from the browser session
    and POSTs normalized data here. This is the primary path for manual
    Sync Now in the extension; the legacy /sync endpoint remains as
    fallback. Storage dedupe semantics match run_sync.
    """
    try:
        # Validate the account exists; reuse existing lookup helper.
        storage.get_account_auth(body.account_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=redact_string(str(e))) from e

    threads = [
        IngestThread(
            platform_thread_id=t.platform_thread_id,
            title=t.title,
            messages=[
                IngestMessage(
                    platform_message_id=m.platform_message_id,
                    direction=m.direction,
                    sender=m.sender,
                    text=m.text,
                    sent_at=m.sent_at,
                    raw=m.raw,
                )
                for m in t.messages
            ],
        )
        for t in body.threads
    ]
    try:
        result: SyncResult = run_ingest(
            account_id=body.account_id,
            storage=storage,
            threads=threads,
            pages_fetched=body.pages_fetched,
            rate_limited=body.rate_limited,
        )
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=422, detail=redact_string(str(e))) from None

    contract_meta = body.messaging_contract or {}
    logger.info(
        "Ingest accepted: %s",
        redact_for_log({
            "account_id": body.account_id,
            "synced_threads": result.synced_threads,
            "messages_inserted": result.messages_inserted,
            "messages_skipped_duplicate": result.messages_skipped_duplicate,
            "pages_fetched": result.pages_fetched,
            "rate_limited": result.rate_limited,
            "contract_captured_at": contract_meta.get("capturedAt"),
        }),
    )
    return {
        "ok": True,
        "synced_threads": result.synced_threads,
        "messages_inserted": result.messages_inserted,
        "messages_skipped_duplicate": result.messages_skipped_duplicate,
        "pages_fetched": result.pages_fetched,
        "rate_limited": result.rate_limited,
    }


@app.post("/send", dependencies=[Depends(require_api_auth)])
def send_message(body: SendIn):
    try:
        auth = storage.get_account_auth(body.account_id)
        proxy = storage.get_account_proxy(body.account_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=redact_string(str(e))) from e
    incoming_ctx = BrowserContext(x_li_track=body.x_li_track, csrf_token=body.csrf_token)
    if not incoming_ctx.is_empty():
        storage.update_browser_context(body.account_id, incoming_ctx)
    browser_context = storage.get_browser_context(body.account_id)
    provider = LinkedInProvider(auth=auth, proxy=proxy, account_id=body.account_id, browser_context=browser_context)
    try:
        result: SendResult = run_send(
            account_id=body.account_id,
            storage=storage,
            provider=provider,
            recipient=body.recipient,
            text=body.text,
            idempotency_key=body.idempotency_key,
            x_li_track=body.x_li_track,
            csrf_token=body.csrf_token,
        )
        return {
            "ok": True,
            "send_id": result.send_id,
            "platform_message_id": result.platform_message_id,
            "status": result.status,
            "was_duplicate": result.was_duplicate,
        }
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(
            status_code=401,
            detail="LinkedIn session expired — re-authenticate via POST /accounts/refresh",
        ) from exc
    except (httpx.HTTPStatusError, ConnectionError) as exc:
        raise _provider_http_exception(exc) from exc
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail="Provider not implemented. Implement libs/providers/linkedin/provider.py",
        ) from None


@app.get("/sends", dependencies=[Depends(require_api_auth)])
def list_sends(account_id: int, status: str | None = None):
    """Query outbound send records for an account, optionally filtered by status."""
    try:
        sends = storage.list_outbound_sends(account_id=account_id, status=status)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"sends": sends}

@app.get("/discord", response_class=HTMLResponse, dependencies=[Depends(require_api_auth)])
def discord_ui():
    """Usable local UI for live Discord Sync browsing and guarded read-only sync."""
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Discord Sync</title>
  <style>
    :root{color-scheme:light dark;--bg:#0f172a;--panel:#111827;--panel2:#1f2937;--text:#e5e7eb;--muted:#9ca3af;--line:#374151;--accent:#8b5cf6;--ok:#22c55e;--warn:#f59e0b;--bad:#ef4444;--chip:#312e81}
    body{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;background:linear-gradient(135deg,#0b1021,#161826);color:var(--text)}
    main{max-width:1280px;margin:0 auto;padding:2rem} h1{margin:0 0 .35rem;font-size:2rem} h2{font-size:1rem;margin:0 0 .75rem} p{color:var(--muted)}
    button,input{font:inherit} button{border:1px solid var(--line);border-radius:.7rem;background:#263244;color:var(--text);padding:.55rem .75rem;cursor:pointer} button:hover:not(:disabled){border-color:var(--accent)} button:disabled{opacity:.45;cursor:not-allowed}
    input{width:100%;box-sizing:border-box;border:1px solid var(--line);border-radius:.7rem;background:#0b1220;color:var(--text);padding:.65rem;margin:.2rem 0 .55rem}.badge,.chip{display:inline-flex;align-items:center;gap:.25rem;border-radius:999px;padding:.2rem .55rem;background:var(--chip);font-size:.78rem}.muted{color:var(--muted)}
    .grid{display:grid;grid-template-columns:1.1fr 1fr 1fr;gap:1rem}.panel{background:rgba(17,24,39,.88);border:1px solid var(--line);border-radius:1rem;padding:1rem;box-shadow:0 10px 30px rgba(0,0,0,.18)}.wide{grid-column:1/-1}.stack{display:flex;flex-direction:column;gap:.65rem}.row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}.item{width:100%;text-align:left;background:#111827;border:1px solid var(--line);border-radius:.8rem;padding:.75rem}.item.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}.item strong{display:block}.meta{font-size:.82rem;color:var(--muted);margin-top:.35rem}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.empty{border:1px dashed var(--line);border-radius:.8rem;padding:.9rem;color:var(--muted);background:#0b1220}.error{border-color:rgba(239,68,68,.55);background:rgba(127,29,29,.25);color:#fecaca}.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.75rem}.card{background:#0b1220;border:1px solid var(--line);border-radius:.8rem;padding:.75rem}.card b{display:block;font-size:1.15rem} pre{background:#020617;color:#d1d5db;border:1px solid var(--line);border-radius:.8rem;padding:1rem;overflow:auto;max-height:360px}.message{white-space:pre-wrap}.debug-note{font-size:.82rem;color:var(--muted)}
    @media(max-width:980px){.grid,.cards{grid-template-columns:1fr}}
  </style>
</head>
<body>
<main>
  <header class="wide">
    <h1>Discord Sync <span class="badge">session/web read-only live API MVP</span></h1>
    <p>Browse approved Discord accounts, servers, channels, recent messages, search results, provenance, last sync timestamps, and permission/fetch errors without typing raw IDs. credential material is never rendered in the product flow.</p>
  </header>

  <section id="globalError" class="panel error wide" hidden></section>

  <section class="grid">
    <section class="panel">
      <h2>Session status</h2>
      <div id="statusPanel" class="stack"><div class="empty">Loading Discord auth status…</div></div>
      <div class="row" style="margin-top:.75rem">
        <button id="refreshStatusBtn" type="button">Refresh status</button>
        <button id="oauthBtn" type="button">Optional OAuth URL</button>
      </div>
    </section>

    <section class="panel">
      <h2>Connected accounts</h2>
      <div id="accountList" class="stack"><div class="empty">No connected Discord accounts yet.</div></div>
    </section>

    <section class="panel">
      <h2>Missing config</h2>
      <div id="configPanel" class="stack"><div class="empty">Checking local configuration…</div></div>
    </section>

    <section class="panel">
      <h2>Guilds / servers</h2>
      <div class="row" style="margin-bottom:.65rem"><button id="syncGuildsBtn" type="button" disabled>Sync guilds</button></div>
      <div id="guildList" class="stack"><div class="empty">No account selected.</div></div>
    </section>

    <section class="panel">
      <h2>Channels</h2>
      <div class="row" style="margin-bottom:.65rem"><button id="syncChannelsBtn" type="button" disabled>Sync channels</button></div>
      <div id="channelList" class="stack"><div class="empty">Select a guild to load channels.</div></div>
    </section>

    <section class="panel">
      <h2>Messages & search</h2>
      <label class="muted" for="searchBox">Search selected channel</label>
      <input id="searchBox" placeholder="keyword/topic (optional)" autocomplete="off">
      <div class="row" style="margin-bottom:.65rem">
        <button id="searchBtn" type="button" disabled>Search messages</button>
        <button id="syncMessagesBtn" type="button" disabled>Sync recent messages</button>
      </div>
      <div id="messageList" class="stack"><div class="empty">Select a channel to load messages.</div></div>
    </section>

    <section class="panel wide">
      <h2>Counts</h2>
      <div id="countsPanel" class="cards">
        <div class="card"><span class="muted">Accounts</span><b>0</b></div>
        <div class="card"><span class="muted">Guilds</span><b>0</b></div>
        <div class="card"><span class="muted">Channels</span><b>0</b></div>
        <div class="card"><span class="muted">Messages</span><b>0</b></div>
      </div>
    </section>

    <section class="panel wide">
      <h2>Permission/fetch errors</h2>
      <div id="errorList" class="stack"><div class="empty">No permission/fetch errors loaded.</div></div>
    </section>

    <section class="panel wide">
      <h2>Debug JSON</h2>
      <p class="debug-note">Last API response for troubleshooting. Account/server/channel identifiers can appear here, but Discord tokens, cookies, authorization headers, and other credential material are never rendered by the API/UI.</p>
      <pre id="debugPanel">Ready.</pre>
    </section>

    <section class="panel wide">
      <h2>Advanced: connect approved session/web</h2>
      <p class="muted">Prefer a local storage-state file. Paste cookie/header material only for an explicitly approved local validation session; the response/debug panel is redacted.</p>
      <label class="muted" for="sessionStatePath">Session state path</label><input id="sessionStatePath" placeholder="/path/to/storage-state.json">
      <label class="muted" for="cookieHeader">Cookie header fallback</label><input id="cookieHeader" placeholder="approved Discord Web Cookie header">
      <label class="muted" for="userAgent">User-Agent optional</label><input id="userAgent" placeholder="Mozilla/5.0">
      <button id="connectSessionBtn" type="button">Connect session/web</button>
    </section>
  </section>
</main>
<script>
const state = { accountId: null, guildId: null, channelId: null, status: null, guilds: [], channels: [], messages: [], errors: [] };
const $ = (id) => document.getElementById(id);
function esc(value){ return String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function setGlobalError(message){ const box = $('globalError'); if(!message){ box.hidden = true; box.textContent = ''; return; } box.hidden = false; box.textContent = message; }
function empty(message){ return `<div class="empty">${esc(message)}</div>`; }
function pill(text, cls='chip'){ return `<span class="${cls}">${esc(text)}</span>`; }
function setDebug(label, data){ $('debugPanel').textContent = `${label}\n` + JSON.stringify(data, null, 2); }
async function api(path, options = {}){
  setGlobalError('');
  try {
    const response = await fetch(path, options);
    const body = await response.json().catch(() => ({}));
    setDebug(`${options.method || 'GET'} ${path} → ${response.status}`, body);
    if(!response.ok){ throw new Error(body.detail || body.message || `Request failed with ${response.status}`); }
    return body;
  } catch (error) {
    setGlobalError(error.message || String(error));
    throw error;
  }
}
function renderCounts(){
  const accounts = (state.status?.accounts || []).length;
  $('countsPanel').innerHTML = [
    ['Accounts', accounts], ['Guilds', state.guilds.length], ['Channels', state.channels.length], ['Messages', state.messages.length]
  ].map(([label, value]) => `<div class="card"><span class="muted">${label}</span><b>${value}</b></div>`).join('');
}
function renderStatus(data){
  const accounts = data.accounts || [];
  $('statusPanel').innerHTML = `
    <div class="row">${pill(data.ok ? 'API reachable' : 'API issue', data.ok ? 'chip ok' : 'chip bad')} ${pill(data.config?.session_web_supported ? 'session/web supported' : 'session/web unavailable')}</div>
    <div><strong>Session status</strong><div class="meta">${accounts.length ? `${accounts.length} connected account(s)` : 'No connected Discord accounts yet'}</div></div>
    <div><strong>Persistence</strong><div class="meta">${data.config?.session_persistence_enabled ? 'Encrypted local session persistence enabled' : 'DESEARCH_ENCRYPTION_KEY missing — new session material cannot persist'}</div></div>
    <div><strong>Scopes</strong><div class="meta">${accounts.flatMap(a => a.scopes || []).join(', ') || 'No account scopes available yet'}</div></div>`;
}
function renderConfig(config = {}){
  const missing = [...(config.missing_oauth || [])];
  if(!config.session_persistence_enabled) missing.unshift('DESEARCH_ENCRYPTION_KEY');
  $('configPanel').innerHTML = missing.length
    ? `<div class="empty warn"><strong>Missing config</strong><div class="meta">${missing.map(esc).join(', ')}</div></div>`
    : `<div class="empty ok"><strong>Config ready</strong><div class="meta">OAuth/session settings available for local use.</div></div>`;
}
function renderAccounts(accounts){
  if(!accounts.length){ $('accountList').innerHTML = empty('No connected Discord accounts yet. Connect an approved session/web account or complete OAuth first.'); return; }
  $('accountList').innerHTML = accounts.map(account => `
    <button class="item ${account.id === state.accountId ? 'active' : ''}" type="button" data-account-id="${esc(account.id)}">
      <strong>${esc(account.global_name || account.username || account.discord_user_id || ('Account ' + account.id))}</strong>
      <div class="meta">Status: ${esc(account.status || 'unknown')} · Persistence: ${account.token_persisted ? 'persisted' : 'not persisted'}</div>
      <div class="meta">Scopes: ${esc((account.scopes || []).join(', ') || 'none')} · Updated: ${esc(account.updated_at || 'never')}</div>
    </button>`).join('');
  $('accountList').querySelectorAll('[data-account-id]').forEach(btn => btn.addEventListener('click', () => selectAccount(Number(btn.dataset.accountId))));
}
async function loadStatus(){
  const data = await api('/discord/auth/status');
  state.status = data;
  renderStatus(data); renderConfig(data.config); renderAccounts(data.accounts || []);
  if((data.accounts || []).length && !state.accountId){ const account = data.accounts[0]; selectAccount(account.id); }
  renderCounts();
}
function setSelectionGuards(){
  $('syncGuildsBtn').disabled = !state.accountId;
  $('syncChannelsBtn').disabled = !(state.accountId && state.guildId);
  $('searchBtn').disabled = !(state.accountId && state.channelId);
  $('syncMessagesBtn').disabled = !(state.accountId && state.channelId);
}
async function selectAccount(accountId){
  if(!accountId){ $('guildList').innerHTML = empty('No account selected.'); setSelectionGuards(); return; }
  state.accountId = accountId; state.guildId = null; state.channelId = null; state.channels = []; state.messages = [];
  renderAccounts(state.status?.accounts || []); setSelectionGuards();
  $('channelList').innerHTML = empty('Select a guild to load channels.'); $('messageList').innerHTML = empty('Select a channel to load messages.');
  await Promise.all([loadGuilds(), loadErrors()]);
}
async function loadGuilds(){
  if(!state.accountId){ $('guildList').innerHTML = empty('No account selected.'); return; }
  const params = new URLSearchParams({account_id: String(state.accountId)});
  const data = await api(`/discord/guilds?${params}`); state.guilds = data.guilds || [];
  $('guildList').innerHTML = state.guilds.length ? state.guilds.map(guild => `
    <button class="item ${guild.discord_guild_id === state.guildId ? 'active' : ''}" type="button" data-guild-id="${esc(guild.discord_guild_id)}">
      <strong>${esc(guild.name || guild.discord_guild_id)}</strong>
      <div class="meta">Provenance: ${esc(guild.provenance || 'unknown')} · Last synced: ${esc(guild.last_synced_at || 'never')}</div>
      <div class="meta">Owner: ${guild.owner ? 'yes' : 'no'} · Permissions: ${esc(guild.permissions || 'unknown')}</div>
    </button>`).join('') : empty('No guilds synced yet. Use Sync guilds when an approved persisted session is present.');
  $('guildList').querySelectorAll('[data-guild-id]').forEach(btn => btn.addEventListener('click', () => selectGuild(btn.dataset.guildId)));
  renderCounts(); setSelectionGuards();
}
async function selectGuild(guildId){ state.guildId = guildId; state.channelId = null; state.messages = []; $('messageList').innerHTML = empty('Select a channel to load messages.'); await loadChannels(); setSelectionGuards(); }
async function loadChannels(){
  if(!state.accountId){ $('channelList').innerHTML = empty('No account selected.'); return; }
  if(!state.guildId){ $('channelList').innerHTML = empty('Select a guild to load channels.'); return; }
  const params = new URLSearchParams({account_id: String(state.accountId), guild_id: state.guildId});
  const data = await api(`/discord/channels?${params}`); state.channels = data.channels || [];
  $('channelList').innerHTML = state.channels.length ? state.channels.map(channel => `
    <button class="item ${channel.discord_channel_id === state.channelId ? 'active' : ''}" type="button" data-channel-id="${esc(channel.discord_channel_id)}">
      <strong>#${esc(channel.name || channel.discord_channel_id)}</strong>
      <div class="meta">Provenance: ${esc(channel.provenance || 'unknown')} · Last synced: ${esc(channel.last_synced_at || 'never')}</div>
      <div class="meta">Type: ${esc(channel.type ?? 'unknown')} · Topic: ${esc(channel.topic || 'none')}</div>
    </button>`).join('') : empty('No channels synced for this guild yet. Use Sync channels when permissions allow it.');
  $('channelList').querySelectorAll('[data-channel-id]').forEach(btn => btn.addEventListener('click', () => selectChannel(btn.dataset.channelId)));
  renderCounts(); setSelectionGuards();
}
async function selectChannel(channelId){ state.channelId = channelId; await loadMessages(); setSelectionGuards(); }
async function loadMessages(){
  if(!state.accountId){ $('messageList').innerHTML = empty('No account selected.'); return; }
  if(!state.channelId){ $('messageList').innerHTML = empty('Select a channel to load messages.'); return; }
  const params = new URLSearchParams({account_id: String(state.accountId), channel_id: state.channelId, limit: '50'});
  const q = $('searchBox').value.trim(); if(q) params.set('q', q);
  const data = await api(`/discord/messages?${params}`); state.messages = data.messages || [];
  $('messageList').innerHTML = state.messages.length ? state.messages.map(message => `
    <article class="item">
      <strong>${esc(message.author_global_name || message.author_username || message.author_id || 'Unknown author')}</strong>
      <div class="message">${esc(message.content || '[empty message]')}</div>
      <div class="meta">Source: ${esc(message.source || 'unknown')} · Provenance: ${esc(message.provenance || 'unknown')} · Sent: ${esc(message.sent_at || 'unknown')}</div>
    </article>`).join('') : empty(q ? 'No messages matched this search in the selected channel.' : 'No messages loaded for this channel yet.');
  renderCounts();
}
async function loadErrors(){
  const params = state.accountId ? new URLSearchParams({account_id: String(state.accountId)}) : new URLSearchParams();
  const data = await api(`/discord/errors?${params}`); state.errors = data.errors || [];
  $('errorList').innerHTML = state.errors.length ? state.errors.map(error => `
    <div class="item error"><strong>${esc(error.scope || 'sync')} ${error.status_code ? '(' + esc(error.status_code) + ')' : ''}</strong>
      <div>${esc(error.message || 'Unknown error')}</div><div class="meta">Route: ${esc(error.route || 'n/a')} · Created: ${esc(error.created_at || 'unknown')}</div></div>`).join('') : empty('No permission/fetch errors for the selected account.');
}
function requireAccount(){ if(!state.accountId){ setGlobalError('No account selected.'); return false; } return true; }
function requireGuild(){ if(!requireAccount()) return false; if(!state.guildId){ setGlobalError('Select a guild to load channels.'); return false; } return true; }
function requireChannel(){ if(!requireAccount()) return false; if(!state.channelId){ setGlobalError('Select a channel to load messages.'); return false; } return true; }
async function postJson(path, body){ return api(path, {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(body)}); }
async function syncGuilds(){ if(!requireAccount()) return; const result = await postJson('/discord/sync/guilds', {account_id: state.accountId}); await Promise.all([loadGuilds(), loadErrors()]); if(result.ok === false) setGlobalError('Guild sync reported an error. See Permission/fetch errors.'); }
async function syncChannels(){ if(!requireGuild()) return; const result = await postJson('/discord/sync/channels', {account_id: state.accountId, guild_id: state.guildId}); await Promise.all([loadChannels(), loadErrors()]); if(result.ok === false) setGlobalError('Channel sync reported an error. See Permission/fetch errors.'); }
async function syncMessages(){ if(!requireChannel()) return; const result = await postJson('/discord/sync/messages', {account_id: state.accountId, channel_id: state.channelId, limit: 50}); await Promise.all([loadMessages(), loadErrors()]); if(result.ok === false) setGlobalError('Message sync reported an error. See Permission/fetch errors.'); }
async function connectSession(){
  const body = {session_state_path: $('sessionStatePath').value.trim() || null, cookie_header: $('cookieHeader').value.trim() || null, user_agent: $('userAgent').value.trim() || null};
  if(!body.session_state_path && !body.cookie_header){ setGlobalError('Provide a session state path or approved Cookie header before connecting.'); return; }
  await postJson('/discord/session/connect', body); $('cookieHeader').value = ''; await loadStatus();
}
async function openOAuth(){ const data = await api('/discord/auth/start'); if(data.authorization_url) window.open(data.authorization_url, '_blank', 'noopener,noreferrer'); }
window.addEventListener('DOMContentLoaded', () => {
  $('refreshStatusBtn').addEventListener('click', loadStatus); $('oauthBtn').addEventListener('click', openOAuth); $('syncGuildsBtn').addEventListener('click', syncGuilds); $('syncChannelsBtn').addEventListener('click', syncChannels); $('syncMessagesBtn').addEventListener('click', syncMessages); $('searchBtn').addEventListener('click', loadMessages); $('searchBox').addEventListener('keydown', (event) => { if(event.key === 'Enter') loadMessages(); }); $('connectSessionBtn').addEventListener('click', connectSession);
  setSelectionGuards(); loadStatus().catch(() => {});
});
</script>
</body></html>
"""


@app.get("/discord/auth/status", dependencies=[Depends(require_api_auth)])
def discord_auth_status():
    return {"ok": True, "config": _discord_config_status(), "accounts": storage.list_discord_accounts()}




@app.get("/discord/auth/start", dependencies=[Depends(require_api_auth)])
def discord_auth_start():
    provider = _make_discord_provider()
    try:
        state = secrets.token_urlsafe(24)
        storage.create_discord_oauth_state(state)
        url = provider.config.authorization_url(state=state, scopes=list(DEFAULT_SCOPES))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=redact_string(str(exc))) from exc
    finally:
        provider.close()
    return {"ok": True, "authorization_url": url, "state": state, "scopes": list(DEFAULT_SCOPES), "config": _discord_config_status()}


@app.get("/discord/auth/callback")
def discord_auth_callback(code: str, state: str):
    if not storage.consume_discord_oauth_state(state):
        raise HTTPException(status_code=400, detail="Invalid or already-consumed Discord OAuth state")
    provider = _make_discord_provider()
    try:
        token_payload = provider.exchange_code(code)
        access_token = token_payload.get("access_token")
        if not access_token:
            raise HTTPException(status_code=502, detail="Discord OAuth token response did not include an access token")
        user = provider.get_current_user(access_token)
        scopes = str(token_payload.get("scope") or "").split()
        expires_at = token_expires_at(token_payload)
        persist_tokens = storage.discord_token_persistence_enabled()
        status = "connected" if persist_tokens else "connected_token_not_persisted"
        last_error = None if persist_tokens else "DESEARCH_ENCRYPTION_KEY not configured; Discord OAuth token material was not persisted"
        token_material = {
            "access_token": token_payload.get("access_token"),
            "refresh_token": token_payload.get("refresh_token"),
            "token_type": token_payload.get("token_type", "Bearer"),
            "expires_at": expires_at,
            "scope": token_payload.get("scope"),
        } if persist_tokens else None
        discord_user_id = user.get("id")
        if not discord_user_id:
            raise HTTPException(status_code=502, detail="Discord user payload did not include an id")
        account_id = storage.upsert_discord_account(
            discord_user_id=str(discord_user_id),
            username=user.get("username"),
            global_name=user.get("global_name"),
            scopes=scopes,
            status=status,
            token_material=token_material,
            token_expires_at=expires_at,
            last_error=last_error,
        )
        account = storage.get_discord_account(account_id)
        logger.info("Discord account connected: %s", redact_for_log({"account_id": account_id, "discord_user_id": user.get("id"), "status": status}))
        return {"ok": True, "account": account, "token_persistence_enabled": persist_tokens}
    except DiscordAPIError as exc:
        raise HTTPException(status_code=502, detail=redact_string(str(exc))) from exc
    finally:
        provider.close()


@app.post("/discord/session/connect", dependencies=[Depends(require_api_auth)])
def discord_session_connect(body: DiscordSessionConnectIn):
    try:
        auth = body.to_session_auth()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=redact_string(str(exc))) from exc
    provider = _make_discord_session_provider(auth)
    try:
        user = provider.get_current_user()
        persist_session = storage.discord_token_persistence_enabled()
        status = "session_connected" if persist_session else "session_connected_not_persisted"
        last_error = None if persist_session else "DESEARCH_ENCRYPTION_KEY not configured; Discord session:web material was not persisted"
        token_material = auth.to_material() if persist_session else None
        discord_user_id = user.get("id")
        if not discord_user_id:
            raise HTTPException(status_code=502, detail="Discord session user payload did not include an id")
        account_id = storage.upsert_discord_account(
            discord_user_id=str(discord_user_id),
            username=user.get("username"),
            global_name=user.get("global_name"),
            scopes=["session:web"],
            status=status,
            token_material=token_material,
            token_expires_at=None,
            last_error=last_error,
        )
        account = storage.get_discord_account(account_id)
        logger.info("Discord session/web account connected: %s", redact_for_log({"account_id": account_id, "discord_user_id": discord_user_id, "status": status, "cookie_header": auth.cookie_header}))
        return {"ok": True, "account": account, "token_persistence_enabled": persist_session}
    except DiscordAPIError as exc:
        raise HTTPException(status_code=502, detail=redact_string(str(exc))) from exc
    finally:
        provider.close()


@app.post("/discord/sync/guilds", dependencies=[Depends(require_api_auth)])
def discord_sync_guilds(body: DiscordAccountSyncIn):
    account = storage.get_discord_account(body.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Discord account not found")

    if _discord_account_is_session_web(account):
        session = storage.get_discord_account_session(body.account_id)
        if session is None:
            err = _session_not_persisted_error(account_id=body.account_id, scope="guilds")
            return {"ok": False, "upserted": 0, "errors": [err]}
        provider = _make_discord_session_provider(_session_auth_from_material(session))
        try:
            guilds = provider.list_user_guilds()
            for guild in guilds:
                storage.upsert_discord_guild(account_id=body.account_id, guild=guild, provenance="session:web")
            return {"ok": True, "upserted": len(guilds), "guilds": storage.list_discord_guilds(account_id=body.account_id), "errors": []}
        except DiscordAPIError as exc:
            err = _record_discord_api_error(account_id=body.account_id, scope="guilds", exc=exc)
            return {"ok": False, "upserted": 0, "errors": [err]}
        finally:
            provider.close()

    token = storage.get_discord_account_token(body.account_id)
    if not token or not token.get("access_token"):
        err = _record_discord_api_error(account_id=body.account_id, scope="guilds", exc=RuntimeError("Discord OAuth access token is not persisted; configure DESEARCH_ENCRYPTION_KEY and reconnect, or connect session:web"))
        return {"ok": False, "upserted": 0, "errors": [err]}
    provider = _make_discord_provider()
    try:
        guilds = provider.list_user_guilds(token["access_token"])
        for guild in guilds:
            storage.upsert_discord_guild(account_id=body.account_id, guild=guild, provenance="oauth:guilds")
        return {"ok": True, "upserted": len(guilds), "guilds": storage.list_discord_guilds(account_id=body.account_id), "errors": []}
    except DiscordAPIError as exc:
        err = _record_discord_api_error(account_id=body.account_id, scope="guilds", exc=exc)
        return {"ok": False, "upserted": 0, "errors": [err]}
    finally:
        provider.close()


@app.post("/discord/sync/channels", dependencies=[Depends(require_api_auth)])
def discord_sync_channels(body: DiscordChannelSyncIn):
    account = storage.get_discord_account(body.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Discord account not found")

    if _discord_account_is_session_web(account):
        session = storage.get_discord_account_session(body.account_id)
        if session is None:
            err = _session_not_persisted_error(account_id=body.account_id, guild_id=body.guild_id, scope="channels")
            return {"ok": False, "upserted": 0, "errors": [err]}
        provider = _make_discord_session_provider(_session_auth_from_material(session))
        try:
            channels = provider.list_guild_channels(body.guild_id)
            for channel in channels:
                storage.upsert_discord_channel(account_id=body.account_id, guild_id=body.guild_id, channel=channel, provenance="session:web")
            return {"ok": True, "upserted": len(channels), "channels": storage.list_discord_channels(account_id=body.account_id, guild_id=body.guild_id), "errors": []}
        except DiscordAPIError as exc:
            err = _record_discord_api_error(account_id=body.account_id, guild_id=body.guild_id, scope="channels", exc=exc)
            return {"ok": False, "upserted": 0, "errors": [err]}
        finally:
            provider.close()

    provider = _make_discord_provider()
    try:
        bot_token = provider.config.require_bot_token()
        channels = provider.list_guild_channels(body.guild_id, bot_token=bot_token)
        for channel in channels:
            storage.upsert_discord_channel(account_id=body.account_id, guild_id=body.guild_id, channel=channel, provenance="bot:guild_channels")
        return {"ok": True, "upserted": len(channels), "channels": storage.list_discord_channels(account_id=body.account_id, guild_id=body.guild_id), "errors": []}
    except (DiscordAPIError, RuntimeError) as exc:
        err_exc = exc if isinstance(exc, DiscordAPIError) else _discord_missing_session_error()
        err = _record_discord_api_error(account_id=body.account_id, guild_id=body.guild_id, scope="channels", exc=err_exc)
        return {"ok": False, "upserted": 0, "errors": [err]}
    finally:
        provider.close()


@app.post("/discord/sync/messages", dependencies=[Depends(require_api_auth)])
def discord_sync_messages(body: DiscordMessageSyncIn):
    account = storage.get_discord_account(body.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Discord account not found")

    if _discord_account_is_session_web(account):
        session = storage.get_discord_account_session(body.account_id)
        if session is None:
            err = _session_not_persisted_error(account_id=body.account_id, channel_id=body.channel_id, scope="messages")
            return {"ok": False, "fetched": 0, "inserted": 0, "duplicates": 0, "errors": [err]}
        provider = _make_discord_session_provider(_session_auth_from_material(session))
        try:
            messages = provider.list_channel_messages(body.channel_id, limit=body.limit, before=body.before, after=body.after)
            inserted = 0
            duplicates = 0
            for message in messages:
                if storage.insert_discord_message(account_id=body.account_id, channel_id=body.channel_id, message=message, provenance="session:web"):
                    inserted += 1
                else:
                    duplicates += 1
            return {"ok": True, "fetched": len(messages), "inserted": inserted, "duplicates": duplicates, "errors": []}
        except (DiscordAPIError, ValueError) as exc:
            if isinstance(exc, DiscordAPIError):
                err = _record_discord_api_error(account_id=body.account_id, channel_id=body.channel_id, scope="messages", exc=exc)
            else:
                err = _record_discord_api_error(account_id=body.account_id, channel_id=body.channel_id, scope="messages", exc=RuntimeError(str(exc)))
            return {"ok": False, "fetched": 0, "inserted": 0, "duplicates": 0, "errors": [err]}
        finally:
            provider.close()

    provider = _make_discord_provider()
    try:
        bot_token = provider.config.require_bot_token()
        messages = provider.list_channel_messages(body.channel_id, bot_token=bot_token, limit=body.limit, before=body.before, after=body.after)
        inserted = 0
        duplicates = 0
        for message in messages:
            if storage.insert_discord_message(account_id=body.account_id, channel_id=body.channel_id, message=message, provenance="bot:channel_messages"):
                inserted += 1
            else:
                duplicates += 1
        return {"ok": True, "fetched": len(messages), "inserted": inserted, "duplicates": duplicates, "errors": []}
    except (DiscordAPIError, RuntimeError, ValueError) as exc:
        if isinstance(exc, DiscordAPIError):
            err = _record_discord_api_error(account_id=body.account_id, channel_id=body.channel_id, scope="messages", exc=exc)
        else:
            err = _record_discord_api_error(account_id=body.account_id, channel_id=body.channel_id, scope="messages", exc=_discord_missing_session_error())
        return {"ok": False, "fetched": 0, "inserted": 0, "duplicates": 0, "errors": [err]}
    finally:
        provider.close()


@app.get("/discord/guilds", dependencies=[Depends(require_api_auth)])
def discord_list_guilds(account_id: int):
    return {"guilds": storage.list_discord_guilds(account_id=account_id)}


@app.get("/discord/channels", dependencies=[Depends(require_api_auth)])
def discord_list_channels(account_id: int, guild_id: str | None = None):
    return {"channels": storage.list_discord_channels(account_id=account_id, guild_id=guild_id)}


@app.get("/discord/messages", dependencies=[Depends(require_api_auth)])
def discord_list_messages(account_id: int | None = None, channel_id: str | None = None, q: str | None = None, limit: int = 100):
    return {"messages": storage.list_discord_messages(account_id=account_id, channel_id=channel_id, q=q, limit=min(max(limit, 1), 100))}


@app.get("/discord/errors", dependencies=[Depends(require_api_auth)])
def discord_list_errors(account_id: int | None = None, limit: int = 100):
    return {"errors": storage.list_discord_errors(account_id=account_id, limit=min(max(limit, 1), 100))}
