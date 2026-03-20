from __future__ import annotations

import logging
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator

from libs.core.cookies import cookies_to_account_auth, validate_li_at
from libs.core.job_runner import SyncResult, run_send, run_sync
from libs.core.models import AccountAuth, ProxyConfig
from libs.core.redaction import configure_logging, redact_for_log, redact_string
from libs.core.storage import Storage
from libs.providers.linkedin.provider import LinkedInProvider

logger = logging.getLogger(__name__)

configure_logging()

app = FastAPI(title="Desearch LinkedIn DMs", version="0.0.2")

storage = Storage()
storage.migrate()


class AuthCheckResponse(BaseModel):
    status: str
    error: Optional[str] = None


class AccountCreateIn(BaseModel):
    label: str = Field(..., description="Human label, e.g. 'sales-1'")
    li_at: str | None = Field(
        None,
        description="LinkedIn li_at cookie value (required if cookies not provided)",
    )
    jsessionid: str | None = Field(None, description="Optional JSESSIONID cookie value")
    cookies: str | None = Field(
        None,
        description="Cookie header string, e.g. 'li_at=xxx; JSESSIONID=yyy'. Overrides li_at/jsessionid fields.",
    )
    proxy_url: str | None = Field(None, description="Optional proxy URL")

    @model_validator(mode="after")
    def require_auth(self) -> AccountCreateIn:
        if not self.cookies and not self.li_at:
            raise ValueError("Provide either 'cookies' string or 'li_at' field")
        return self

    def to_account_auth(self) -> AccountAuth:
        if self.cookies:
            return cookies_to_account_auth(self.cookies)
        return AccountAuth(
            li_at=validate_li_at(self.li_at or ""), jsessionid=self.jsessionid
        )


class SendIn(BaseModel):
    account_id: int
    recipient: str = Field(
        ..., min_length=1, description="Recipient id (profile URN or conversation id)"
    )
    text: str = Field(..., min_length=1, max_length=8000, description="Message body")
    idempotency_key: str | None = None


class SyncIn(BaseModel):
    account_id: int
    limit_per_thread: int = Field(50, ge=1, le=200, description="Messages per page")
    max_pages_per_thread: int | None = Field(
        1,
        ge=1,
        le=100,
        description="Max pages per thread (1=MVP); omit or null to exhaust cursor",
    )


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/accounts")
def create_account(body: AccountCreateIn):
    try:
        auth = body.to_account_auth()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=redact_string(str(exc)))
    proxy = ProxyConfig(url=body.proxy_url) if body.proxy_url else None
    account_id = storage.create_account(label=body.label, auth=auth, proxy=proxy)
    logger.info(
        "Account created: %s",
        redact_for_log({"account_id": account_id, "label": body.label}),
    )
    return {"account_id": account_id}


@app.get("/auth/check", response_model=AuthCheckResponse)
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


@app.get("/threads")
def list_threads(account_id: int):
    return {"threads": storage.list_threads(account_id=account_id)}


@app.post("/sync")
def sync_account(body: SyncIn):
    """Trigger a sync. Default one page per thread (MVP); set max_pages_per_thread or null to exhaust."""
    try:
        auth = storage.get_account_auth(body.account_id)
        proxy = storage.get_account_proxy(body.account_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=redact_string(str(e))) from e
    with LinkedInProvider(auth=auth, proxy=proxy) as provider:
        try:
            result: SyncResult = run_sync(
                account_id=body.account_id,
                storage=storage,
                provider=provider,
                limit_per_thread=body.limit_per_thread,
                max_pages_per_thread=body.max_pages_per_thread,
            )
            return {
                "ok": True,
                "synced_threads": result.synced_threads,
                "messages_inserted": result.messages_inserted,
                "messages_skipped_duplicate": result.messages_skipped_duplicate,
                "pages_fetched": result.pages_fetched,
            }
        except NotImplementedError:
            raise HTTPException(
                status_code=501,
                detail="Provider not implemented. Implement libs/providers/linkedin/provider.py",
            ) from None
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (301, 302, 303, 307, 308):
                raise HTTPException(status_code=401, detail=str(exc)) from None
            raise HTTPException(status_code=502, detail=str(exc)) from None


@app.post("/send")
def send_message(body: SendIn):
    try:
        auth = storage.get_account_auth(body.account_id)
        proxy = storage.get_account_proxy(body.account_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=redact_string(str(e))) from e
    with LinkedInProvider(auth=auth, proxy=proxy) as provider:
        try:
            platform_message_id = run_send(
                account_id=body.account_id,
                storage=storage,
                provider=provider,
                recipient=body.recipient,
                text=body.text,
                idempotency_key=body.idempotency_key,
            )
            return {"ok": True, "platform_message_id": platform_message_id}
        except NotImplementedError:
            raise HTTPException(
                status_code=501,
                detail="Provider not implemented. Implement libs/providers/linkedin/provider.py",
            ) from None
