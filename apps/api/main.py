from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator

from libs.core.cookies import cookies_to_account_auth
from libs.core.models import AccountAuth, ProxyConfig
from libs.core.redaction import redact_for_log
from libs.core.storage import Storage
from libs.providers.linkedin.provider import LinkedInProvider

logger = logging.getLogger(__name__)

app = FastAPI(title="Desearch LinkedIn DMs", version="0.0.2")

storage = Storage()
storage.migrate()


class AccountCreateIn(BaseModel):
    """Create account: provide auth either as raw cookies or as cookie string."""

    label: str = Field(..., description="Human label, e.g. 'sales-1'")
    li_at: str | None = Field(None, description="LinkedIn li_at cookie value (required if cookies not set)")
    jsessionid: str | None = Field(None, description="Optional JSESSIONID cookie value")
    cookies: str | None = Field(
        None,
        description="Cookie string import, e.g. 'li_at=xxx; JSESSIONID=yyy'. If set, li_at/jsessionid are ignored.",
    )
    proxy_url: str | None = Field(None, description="Optional proxy URL")

    @model_validator(mode="after")
    def require_auth(self) -> "AccountCreateIn":
        if self.cookies:
            return self
        if self.li_at:
            return self
        raise ValueError("Provide either 'cookies' or 'li_at'")

    def to_account_auth(self) -> AccountAuth:
        if self.cookies:
            return cookies_to_account_auth(self.cookies)
        return AccountAuth(li_at=self.li_at or "", jsessionid=self.jsessionid)


class SendIn(BaseModel):
    account_id: int
    recipient: str
    text: str
    idempotency_key: str | None = None


class SyncIn(BaseModel):
    account_id: int
    limit_per_thread: int = 50


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/accounts")
def create_account(body: AccountCreateIn):
    try:
        auth = body.to_account_auth()
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    proxy = ProxyConfig(url=body.proxy_url) if body.proxy_url else None
    account_id = storage.create_account(label=body.label, auth=auth, proxy=proxy)
    logger.debug("Account created: %s", redact_for_log({"account_id": account_id, "label": body.label}))
    return {"account_id": account_id}


@app.get("/threads")
def list_threads(account_id: int):
    return {"threads": storage.list_threads(account_id=account_id)}


@app.post("/sync")
def sync_account(body: SyncIn):
    """Trigger a sync.

    MVP behavior:
    - Calls provider.list_threads()
    - Upserts threads into DB
    - For each thread: calls fetch_messages() until cursor is exhausted (or one page for MVP)

    NOTE: current provider is NOT implemented; this endpoint is a scaffold for contributors.
    """
    auth = storage.get_account_auth(body.account_id)
    proxy = storage.get_account_proxy(body.account_id)
    provider = LinkedInProvider(auth=auth, proxy=proxy)

    # TODO: implement once provider is implemented
    # threads = provider.list_threads()
    # for t in threads:
    #   thread_id = storage.upsert_thread(account_id=body.account_id, platform_thread_id=t.platform_thread_id, title=t.title)
    #   cursor = storage.get_cursor(account_id=body.account_id, thread_id=thread_id)
    #   msgs, next_cursor = provider.fetch_messages(platform_thread_id=t.platform_thread_id, cursor=cursor, limit=body.limit_per_thread)
    #   for m in msgs:
    #       storage.insert_message(...)
    #   storage.set_cursor(...)

    return {"ok": True, "note": "Provider not implemented yet. Implement libs/providers/linkedin/provider.py"}


@app.post("/send")
def send_message(body: SendIn):
    auth = storage.get_account_auth(body.account_id)
    proxy = storage.get_account_proxy(body.account_id)
    provider = LinkedInProvider(auth=auth, proxy=proxy)

    # TODO: implement once provider is implemented
    # platform_message_id = provider.send_message(recipient=body.recipient, text=body.text, idempotency_key=body.idempotency_key)
    # return {"platform_message_id": platform_message_id}

    return {"ok": True, "note": "Provider not implemented yet. Implement libs/providers/linkedin/provider.py"}
