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
    """Minimal local UI for live Discord Sync state and actions."""
    return """
<!doctype html>
<html><head><title>Discord Sync</title><style>body{font-family:system-ui;margin:2rem;max-width:1100px}button,input{margin:.25rem;padding:.45rem}.badge{background:#eef;border-radius:.5rem;padding:.15rem .4rem}pre{background:#111;color:#eee;padding:1rem;overflow:auto}</style></head>
<body>
<h1>Discord Sync <span class="badge">session/web read-only live API MVP</span></h1>
<p>Primary path: connect an approved logged-in Discord Web session, then read only guilds, channels, and messages visible to that user. OAuth/app auth remains optional fallback.</p>
<button onclick="call('/discord/auth/status')">Session/auth status</button>
<button onclick="call('/discord/auth/start')">Optional OAuth URL</button>
<br>
<input id="statePath" placeholder="session_state_path preferred"><input id="cookie" placeholder="Cookie header fallback"><input id="ua" placeholder="User-Agent optional">
<button onclick="post('/discord/session/connect',{session_state_path:statePath.value||null,cookie_header:cookie.value||null,user_agent:ua.value||null})">Connect session/web</button>
<br>
<input id="account" placeholder="account_id"><input id="guild" placeholder="guild_id"><input id="channel" placeholder="channel_id"><input id="q" placeholder="search">
<br>
<button onclick="post('/discord/sync/guilds',{account_id:+account.value})">Sync guilds</button>
<button onclick="post('/discord/sync/channels',{account_id:+account.value,guild_id:guild.value})">Sync channels</button>
<button onclick="post('/discord/sync/messages',{account_id:+account.value,channel_id:channel.value,limit:50})">Sync messages</button>
<button onclick="call('/discord/guilds?account_id='+account.value)">List guilds</button>
<button onclick="call('/discord/channels?account_id='+account.value+'&guild_id='+guild.value)">List channels</button>
<button onclick="call('/discord/messages?account_id='+account.value+'&channel_id='+channel.value+'&q='+encodeURIComponent(q.value))">Search messages</button>
<button onclick="call('/discord/errors?account_id='+account.value)">Errors</button>
<pre id="out">Ready. session/web provenance, live-vs-fixture source badges, last sync timestamps, and permission/auth errors render here.</pre>
<script>async function call(u){let r=await fetch(u); out.textContent=JSON.stringify(await r.json(),null,2)} async function post(u,b){let r=await fetch(u,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(b)}); out.textContent=JSON.stringify(await r.json(),null,2)}</script>
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
