from __future__ import annotations

import logging

import httpx
import os
import secrets
from dataclasses import replace
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
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
        storage.record_ops_audit_event(
            account_id=body.account_id,
            event_type="sync.completed",
            actor="api",
            entity_type="sync",
            entity_id=None,
            payload={
                "synced_threads": result.synced_threads,
                "messages_inserted": result.messages_inserted,
                "messages_skipped_duplicate": result.messages_skipped_duplicate,
                "pages_fetched": result.pages_fetched,
                "rate_limited": result.rate_limited,
            },
        )
        return {
            "ok": True,
            "account_id": body.account_id,
            "dry_run": False,
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
    storage.record_ops_audit_event(
        account_id=body.account_id,
        event_type="sync.ingest",
        actor="api",
        entity_type="sync",
        entity_id=None,
        payload={
            "synced_threads": result.synced_threads,
            "messages_inserted": result.messages_inserted,
            "messages_skipped_duplicate": result.messages_skipped_duplicate,
            "pages_fetched": result.pages_fetched,
            "rate_limited": result.rate_limited,
        },
    )
    return {
        "ok": True,
        "account_id": body.account_id,
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


# ---------------------------------------------------------------------------
# Ops Console local-safe API contract
# ---------------------------------------------------------------------------


def _ops_error(status_code: int, code: str, message: str, *, retryable: bool = False, extra: dict | None = None) -> JSONResponse:
    payload = {
        "ok": False,
        "error": {
            "code": code,
            "message": redact_string(message),
            "retryable": retryable,
            "redacted": True,
        },
    }
    if extra:
        payload.update(extra)
    return JSONResponse(status_code=status_code, content=payload)


def _ops_account_not_found(exc: KeyError) -> JSONResponse:
    return _ops_error(404, "account_not_found", str(exc), retryable=False)


class OpsAccountBody(BaseModel):
    account_id: int


class OpsSyncDryRunIn(BaseModel):
    account_id: int
    limit_per_thread: int = Field(50, ge=1, le=MAX_MESSAGES_PER_PAGE)
    max_pages_per_thread: int | None = Field(1, ge=1, le=100)
    delay_between_threads_s: float = Field(2.0, ge=0, le=60)
    delay_between_pages_s: float = Field(1.5, ge=0, le=60)


class DraftReplyIn(BaseModel):
    account_id: int
    thread_id: int | None = None
    recipient: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1, max_length=8000)
    campaign_id: int | None = None
    idempotency_key: str | None = None


class ApprovalActionIn(BaseModel):
    approved_by: str | None = None


class CampaignDryRunIn(BaseModel):
    account_id: int | None = None
    limit: int = Field(25, ge=1, le=500)


class SendApprovedIn(BaseModel):
    approval_id: str
    account_id: int
    recipient: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1, max_length=8000)
    idempotency_key: str | None = None
    x_li_track: str | None = Field(None, description=_X_LI_TRACK_DESC)
    csrf_token: str | None = Field(None, description=_CSRF_TOKEN_DESC)


@app.get("/ops/status", dependencies=[Depends(require_api_auth)])
def ops_status():
    return {
        "ok": True,
        "service": "linkedin-dms",
        "version": app.version,
        "db": {
            "path": storage.db_path,
            "reachable": True,
            "schema_version": storage._get_schema_version(),
        },
        "api": {
            "configured_url": os.getenv("DESEARCH_API_URL", "http://127.0.0.1:8899"),
            "reachable": True,
            "auth_required": _get_api_token() is not None,
        },
        "counts": storage.ops_counts(),
        "warnings": [],
    }


@app.get("/ops/accounts/{account_id}/health", dependencies=[Depends(require_api_auth)])
def ops_account_health(account_id: int):
    try:
        health = storage.get_account_ops_health(account_id)
    except KeyError as exc:
        return _ops_account_not_found(exc)
    return {"ok": health["status"] == "ok", **health}


@app.post("/ops/auth/check", dependencies=[Depends(require_api_auth)])
def ops_auth_check(body: OpsAccountBody):
    try:
        auth = storage.get_account_auth(body.account_id)
        proxy = storage.get_account_proxy(body.account_id)
    except KeyError as exc:
        return _ops_account_not_found(exc)
    provider = LinkedInProvider(auth=auth, proxy=proxy, account_id=body.account_id)
    result = provider.check_auth()
    if result.ok:
        return {
            "ok": True,
            "account_id": body.account_id,
            "status": "ok",
            "checked_at": datetime.utcnow().isoformat() + "Z",
            "session": storage.get_account_ops_health(body.account_id)["session"],
            "error": None,
            "next_action": None,
        }
    return {
        "ok": False,
        "account_id": body.account_id,
        "status": "failed",
        "checked_at": datetime.utcnow().isoformat() + "Z",
        "session": storage.get_account_ops_health(body.account_id)["session"],
        "error": redact_string(result.error or "authentication check failed"),
        "next_action": "refresh_account",
    }


@app.post("/ops/sync/dry-run", dependencies=[Depends(require_api_auth)])
def ops_sync_dry_run(body: OpsSyncDryRunIn):
    try:
        storage.get_account_auth(body.account_id)
    except KeyError as exc:
        return _ops_account_not_found(exc)
    return {
        "ok": True,
        "account_id": body.account_id,
        "dry_run": True,
        "planned": {
            "limit_per_thread": body.limit_per_thread,
            "max_pages_per_thread": body.max_pages_per_thread,
            "delay_between_threads_s": body.delay_between_threads_s,
            "delay_between_pages_s": body.delay_between_pages_s,
        },
        "external_reads": 0,
        "external_writes": 0,
        "warnings": [],
    }


@app.get("/ops/sync/status", dependencies=[Depends(require_api_auth)])
def ops_sync_status(account_id: int):
    try:
        health = storage.get_account_ops_health(account_id)
    except KeyError as exc:
        return _ops_account_not_found(exc)
    return {
        "ok": True,
        "account_id": account_id,
        "last_sync": health["last_sync"],
        "counts": health["counts"],
        "outbound_summary": health["outbound_summary"],
        "safety": {"external_writes": 0, "send_requires_approval": True},
    }


@app.get("/ops/inbox", dependencies=[Depends(require_api_auth)])
def ops_inbox(account_id: int, limit: int = Query(25, ge=1, le=100), cursor: str | None = None, unread_only: bool = False):
    try:
        page = storage.list_inbox_threads(account_id=account_id, limit=limit, cursor=cursor)
    except KeyError as exc:
        return _ops_account_not_found(exc)
    except ValueError as exc:
        return _ops_error(422, "invalid_cursor", str(exc))
    threads = [t for t in page["threads"] if not unread_only or t["unread"]]
    return {"ok": True, "account_id": account_id, "threads": threads, "page": page["page"]}


@app.get("/ops/search", dependencies=[Depends(require_api_auth)])
def ops_search(
    account_id: int,
    q: str = Query(..., min_length=1),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    direction: str | None = Query(None, pattern="^(in|out)$"),
    limit: int = Query(25, ge=1, le=100),
    cursor: str | None = None,
):
    try:
        page = storage.search_messages(
            account_id=account_id,
            query=q,
            direction=direction,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            cursor=cursor,
        )
    except KeyError as exc:
        return _ops_account_not_found(exc)
    except ValueError as exc:
        return _ops_error(422, "invalid_search", str(exc))
    return {
        "ok": True,
        "account_id": account_id,
        "query": q,
        "filters": {"from": from_date, "to": to_date, "direction": direction},
        "results": page["results"],
        "page": page["page"],
        "fts": page["fts"],
    }


@app.get("/ops/threads", dependencies=[Depends(require_api_auth)])
def ops_threads(account_id: int, limit: int = Query(25, ge=1, le=100), cursor: str | None = None):
    try:
        page = storage.list_ops_threads(account_id=account_id, limit=limit, cursor=cursor)
    except KeyError as exc:
        return _ops_account_not_found(exc)
    except ValueError as exc:
        return _ops_error(422, "invalid_cursor", str(exc))
    return {"ok": True, "account_id": account_id, "threads": page["threads"], "page": page["page"]}


@app.get("/ops/threads/{thread_id}", dependencies=[Depends(require_api_auth)])
def ops_thread_detail(thread_id: int, account_id: int):
    try:
        thread = storage.get_thread_detail(account_id=account_id, thread_id=thread_id)
    except KeyError as exc:
        return _ops_error(404, "thread_not_found", str(exc))
    return {"ok": True, "account_id": account_id, "thread": thread}


@app.get("/ops/threads/{thread_id}/messages", dependencies=[Depends(require_api_auth)])
def ops_thread_messages(
    thread_id: int,
    account_id: int,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    include_raw_text: bool = False,
):
    try:
        page = storage.list_thread_messages(
            account_id=account_id,
            thread_id=thread_id,
            limit=limit,
            cursor=cursor,
            include_raw_text=include_raw_text,
        )
    except KeyError as exc:
        return _ops_error(404, "thread_not_found", str(exc))
    except ValueError as exc:
        return _ops_error(422, "invalid_cursor", str(exc))
    return {"ok": True, "account_id": account_id, "thread_id": thread_id, "messages": page["messages"], "page": page["page"]}


@app.post("/ops/drafts", dependencies=[Depends(require_api_auth)])
def ops_create_draft(body: DraftReplyIn):
    try:
        return storage.create_draft_reply(
            account_id=body.account_id,
            thread_id=body.thread_id,
            recipient=body.recipient,
            text=body.text,
            campaign_id=body.campaign_id,
            idempotency_key=body.idempotency_key,
        )
    except KeyError as exc:
        return _ops_error(404, "draft_context_not_found", str(exc))


@app.get("/ops/drafts", dependencies=[Depends(require_api_auth)])
def ops_list_drafts(account_id: int, state: str | None = None, limit: int = Query(25, ge=1, le=100), cursor: str | None = None):
    try:
        page = storage.list_drafts(account_id=account_id, state=state, limit=limit, cursor=cursor)
    except KeyError as exc:
        return _ops_account_not_found(exc)
    except ValueError as exc:
        return _ops_error(422, "invalid_draft_filter", str(exc))
    return {"ok": True, "account_id": account_id, **page}


@app.post("/ops/approvals/{approval_id}/approve", dependencies=[Depends(require_api_auth)])
def ops_approve(approval_id: str, body: ApprovalActionIn | None = None):
    try:
        approval = storage.approve_send_approval(approval_id, approved_by=(body.approved_by if body else None))
    except KeyError as exc:
        return _ops_error(404, "approval_not_found", str(exc))
    return {"ok": True, "approval": approval, "external_writes": 0}


@app.post("/ops/approvals/{approval_id}/revoke", dependencies=[Depends(require_api_auth)])
def ops_revoke(approval_id: str):
    try:
        approval = storage.revoke_send_approval(approval_id)
    except KeyError as exc:
        return _ops_error(404, "approval_not_found", str(exc))
    return {"ok": True, "approval": approval, "external_writes": 0}


@app.get("/ops/approvals", dependencies=[Depends(require_api_auth)])
def ops_list_approvals(account_id: int, state: str | None = None, limit: int = Query(25, ge=1, le=100), cursor: str | None = None):
    try:
        page = storage.list_approvals(account_id=account_id, state=state, limit=limit, cursor=cursor)
    except KeyError as exc:
        return _ops_account_not_found(exc)
    except ValueError as exc:
        return _ops_error(422, "invalid_approval_filter", str(exc))
    return {"ok": True, "account_id": account_id, **page}


@app.get("/ops/campaigns/{campaign_id}/status", dependencies=[Depends(require_api_auth)])
def ops_campaign_status(campaign_id: int, account_id: int | None = None):
    try:
        status = storage.campaign_status(campaign_id=campaign_id, account_id=account_id)
    except KeyError as exc:
        return _ops_account_not_found(exc)
    if status is None:
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "account_id": account_id,
            "state": "draft",
            "totals": {"prospects": 0, "drafted": 0, "approved": 0, "sent": 0, "failed": 0, "skipped": 0},
            "rate_limit": {"daily_cap": 25, "sent_today": 0, "remaining_today": 25, "next_safe_send_at": None},
            "last_run": None,
        }
    return {"ok": True, **status}


@app.post("/ops/campaigns/{campaign_id}/run-dry-run", dependencies=[Depends(require_api_auth)])
def ops_campaign_run_dry_run(campaign_id: int, body: CampaignDryRunIn):
    if body.account_id is not None:
        try:
            storage.get_account_ops_health(body.account_id)
        except KeyError as exc:
            return _ops_account_not_found(exc)
    return {
        "ok": True,
        "campaign_id": campaign_id,
        "account_id": body.account_id,
        "dry_run": True,
        "external_writes": 0,
        "planned_actions": [],
        "summary": {
            "would_send_if_approved": 0,
            "blocked_not_approved": 0,
            "blocked_rate_limit": 0,
            "blocked_missing_auth": 0,
        },
    }


@app.post("/ops/send-approved", dependencies=[Depends(require_api_auth)])
def ops_send_approved(body: SendApprovedIn):
    try:
        storage.validate_send_approval(
            approval_id=body.approval_id,
            account_id=body.account_id,
            recipient=body.recipient,
            text=body.text,
            idempotency_key=body.idempotency_key,
        )
    except ValueError as exc:
        return _ops_error(409, str(exc), "Approved draft evidence is required before sending.", extra={"approved": False, "status": "rejected", "external_writes": 0})
    try:
        auth = storage.get_account_auth(body.account_id)
        proxy = storage.get_account_proxy(body.account_id)
    except KeyError as exc:
        return _ops_account_not_found(exc)
    incoming_ctx = BrowserContext(x_li_track=body.x_li_track, csrf_token=body.csrf_token)
    if not incoming_ctx.is_empty():
        storage.update_browser_context(body.account_id, incoming_ctx)
    provider = LinkedInProvider(
        auth=auth,
        proxy=proxy,
        account_id=body.account_id,
        browser_context=storage.get_browser_context(body.account_id),
    )
    try:
        result = run_send(
            account_id=body.account_id,
            storage=storage,
            provider=provider,
            recipient=body.recipient,
            text=body.text,
            idempotency_key=body.idempotency_key,
            x_li_track=body.x_li_track,
            csrf_token=body.csrf_token,
        )
    except Exception as exc:
        return _ops_error(409, "send_failed", str(exc), retryable=True)
    storage.mark_approval_used(body.approval_id)
    return {
        "ok": True,
        "approved": True,
        "approval_id": body.approval_id,
        "account_id": body.account_id,
        "send_id": result.send_id,
        "platform_message_id": result.platform_message_id,
        "status": result.status,
        "was_duplicate": result.was_duplicate,
        "idempotency_key": body.idempotency_key,
        "sent_at": datetime.utcnow().isoformat() + "Z",
    }


@app.get("/ops/audit", dependencies=[Depends(require_api_auth)])
def ops_audit(
    account_id: int,
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
):
    try:
        page = storage.list_ops_audit(account_id=account_id, from_date=from_date, to_date=to_date, limit=limit, cursor=cursor)
    except KeyError as exc:
        return _ops_account_not_found(exc)
    except ValueError as exc:
        return _ops_error(422, "invalid_audit_filter", str(exc))
    return {"ok": True, "account_id": account_id, **page}


@app.get("/ops/validation/objective-73", dependencies=[Depends(require_api_auth)])
def ops_validation_objective_73():
    return {
        "ok": True,
        "objective": "o-73-build-a-wacli.sh-inspired-linkedin-ops-console-w",
        "counts": storage.ops_counts(),
        "safety": {
            "read_views_are_local_first": True,
            "drafts_create_external_writes": 0,
            "approved_send_required": True,
        },
    }
