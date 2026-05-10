"""Tests for bearer-protected /ops API endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from libs.core import crypto
from libs.core.models import AccountAuth
from libs.core.storage import Storage


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DESEARCH_DB_PATH", str(tmp_path / "api.sqlite"))
    monkeypatch.delenv("DESEARCH_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("DESEARCH_API_TOKEN", raising=False)
    crypto._warned_no_key = False


@pytest.fixture()
def api(tmp_path, monkeypatch):
    storage = Storage(db_path=tmp_path / "api.sqlite")
    storage.migrate()
    from apps.api.main import app
    import apps.api.main as api_mod

    original_storage = api_mod.storage
    api_mod.storage = storage
    yield TestClient(app), storage
    api_mod.storage = original_storage
    storage.close()


def _seed(storage: Storage) -> tuple[int, int]:
    aid = storage.create_account(label="ops", auth=AccountAuth(li_at="AQEDsecret", jsessionid="ajax:csrf"))
    tid = storage.upsert_thread(account_id=aid, platform_thread_id="urn:thread:1", title="Ada")
    storage.insert_message(
        account_id=aid,
        thread_id=tid,
        platform_message_id="m1",
        direction="in",
        sender="Ada",
        text="Bittensor details li_at=AQED_DO_NOT_LEAK",
        sent_at=datetime(2026, 5, 8, 9, 30, tzinfo=timezone.utc),
        raw=None,
    )
    return aid, tid


def test_ops_routes_require_bearer_token(api, monkeypatch):
    client, _ = api
    monkeypatch.setenv("DESEARCH_API_TOKEN", "secret-token")
    checks = [
        ("GET", "/ops/status", None),
        ("GET", "/ops/accounts/1/health", None),
        ("POST", "/ops/auth/check", {"account_id": 1}),
        ("POST", "/ops/sync/dry-run", {"account_id": 1}),
        ("GET", "/ops/sync/status?account_id=1", None),
        ("GET", "/ops/inbox?account_id=1", None),
        ("GET", "/ops/search?account_id=1&q=x", None),
        ("GET", "/ops/threads?account_id=1", None),
        ("GET", "/ops/threads/1?account_id=1", None),
        ("GET", "/ops/threads/1/messages?account_id=1", None),
        ("POST", "/ops/drafts", {"account_id": 1, "recipient": "r", "text": "t"}),
        ("GET", "/ops/drafts?account_id=1", None),
        ("POST", "/ops/approvals/appr_missing/approve", None),
        ("POST", "/ops/approvals/appr_missing/revoke", None),
        ("GET", "/ops/approvals?account_id=1", None),
        ("GET", "/ops/campaigns/1/status?account_id=1", None),
        ("POST", "/ops/campaigns/1/run-dry-run", {"account_id": 1}),
        ("POST", "/ops/send-approved", {"approval_id": "x", "account_id": 1, "recipient": "r", "text": "t"}),
        ("GET", "/ops/audit?account_id=1", None),
        ("GET", "/ops/validation/objective-73", None),
    ]
    for method, url, body in checks:
        resp = client.request(method, url, json=body)
        assert resp.status_code == 401, url
    assert client.get("/health").status_code == 200


def test_ops_empty_and_unknown_account_responses(api):
    client, storage = api
    aid = storage.create_account(label="ops", auth=AccountAuth(li_at="AQEDsecret"))
    assert client.get("/ops/inbox", params={"account_id": aid}).json()["threads"] == []

    resp = client.get("/ops/accounts/999/health")
    assert resp.status_code == 404
    assert resp.json()["ok"] is False
    assert resp.json()["error"]["code"] == "account_not_found"


def test_ops_inbox_search_threads_messages_shapes_and_redaction(api):
    client, storage = api
    aid, tid = _seed(storage)

    inbox = client.get("/ops/inbox", params={"account_id": aid, "limit": 10}).json()
    assert inbox["ok"] is True
    assert inbox["threads"][0]["thread_id"] == tid
    assert "AQED_DO_NOT_LEAK" not in str(inbox)

    search = client.get("/ops/search", params={"account_id": aid, "q": "bittensor", "limit": 10}).json()
    assert search["results"][0]["message_id"]
    assert search["filters"] == {"from": None, "to": None, "direction": None}
    assert "AQED_DO_NOT_LEAK" not in str(search)

    detail = client.get(f"/ops/threads/{tid}", params={"account_id": aid}).json()
    assert detail["thread"]["id"] == tid

    messages = client.get(f"/ops/threads/{tid}/messages", params={"account_id": aid}).json()
    assert messages["thread_id"] == tid
    assert messages["messages"][0]["text"].endswith("[REDACTED]")


def test_ops_limit_validation_and_draft_flow(api):
    client, storage = api
    aid, tid = _seed(storage)
    assert client.get("/ops/inbox", params={"account_id": aid, "limit": 0}).status_code == 422

    draft_resp = client.post(
        "/ops/drafts",
        json={"account_id": aid, "thread_id": tid, "recipient": "urn:li:member:1", "text": "hello", "idempotency_key": "idem"},
    )
    assert draft_resp.status_code == 200
    draft = draft_resp.json()
    assert draft["approval_state"] == "draft"

    approved = client.post(f"/ops/approvals/{draft['approval_id']}/approve").json()
    assert approved["approval"]["state"] == "approved"

    approvals = client.get("/ops/approvals", params={"account_id": aid, "state": "approved"}).json()
    assert approvals["approvals"][0]["approval_id"] == draft["approval_id"]


def test_ops_dry_run_sync_status_audit_and_send_approved_reject(api):
    client, storage = api
    aid, _ = _seed(storage)
    dry = client.post("/ops/sync/dry-run", json={"account_id": aid, "limit_per_thread": 25}).json()
    assert dry["dry_run"] is True
    assert dry["external_writes"] == 0

    status = client.get("/ops/sync/status", params={"account_id": aid}).json()
    assert status["ok"] is True
    assert status["last_sync"] is None

    rejected = client.post(
        "/ops/send-approved",
        json={"approval_id": "appr_missing", "account_id": aid, "recipient": "r", "text": "hello"},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "approval_required"
    assert rejected.json()["external_writes"] == 0

    audit = client.get("/ops/audit", params={"account_id": aid}).json()
    assert audit["ok"] is True
