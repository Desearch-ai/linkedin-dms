"""Tests for the local operator console UI shell."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import app


UI_ROOT = Path("apps/ui")


def test_ops_console_is_served_by_fastapi():
    client = TestClient(app)
    resp = client.get("/console")
    assert resp.status_code == 200
    assert "LinkedIn Ops Console" in resp.text
    assert "Inbox & Search" in resp.text
    assert "Account Health" in resp.text
    assert "Draft Approval" in resp.text


def test_ops_console_uses_ops_api_not_sqlite_or_secrets():
    js = (UI_ROOT / "app.js").read_text()
    assert "/ops/inbox" in js
    assert "/ops/search" in js
    assert "/ops/send-approved" in js
    assert "sqlite" not in js.lower()
    assert "li_at" not in js
    assert "csrf" not in js.lower()
