"""Tests for the POST /accounts/refresh endpoint and 401 session-expired handling."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from libs.core import crypto


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Use a temp DB and reset crypto warning flag."""
    monkeypatch.setenv("DESEARCH_DB_PATH", str(tmp_path / "test.sqlite"))
    monkeypatch.delenv("DESEARCH_ENCRYPTION_KEY", raising=False)
    crypto._warned_no_key = False


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DESEARCH_DB_PATH", str(tmp_path / "test.sqlite"))
    from libs.core.storage import Storage

    storage = Storage(db_path=tmp_path / "test.sqlite")
    storage.migrate()

    from apps.api.main import app

    import apps.api.main as api_mod

    original_storage = api_mod.storage
    api_mod.storage = storage
    yield TestClient(app)
    api_mod.storage = original_storage
    storage.close()


def _create_account(client) -> int:
    """Helper: create an account and return its id."""
    resp = client.post(
        "/accounts",
        json={"label": "test", "li_at": "AQEDAWx0Y29va2llXXX"},
    )
    assert resp.status_code == 200
    return resp.json()["account_id"]


class TestRefreshWithRawFields:
    def test_refresh_with_li_at(self, client):
        aid = _create_account(client)
        resp = client.post(
            "/accounts/refresh",
            json={"account_id": aid, "li_at": "AQEDNewCookieValue123"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["account_id"] == aid

    def test_refresh_with_li_at_and_jsessionid(self, client):
        aid = _create_account(client)
        resp = client.post(
            "/accounts/refresh",
            json={"account_id": aid, "li_at": "AQEDNewCookieValue123", "jsessionid": "ajax:newtok"},
        )
        assert resp.status_code == 200

    def test_refresh_updates_stored_credentials(self, client):
        """Verify that after refresh, get_account_auth returns the new credentials."""
        aid = _create_account(client)
        new_li_at = "AQEDFreshSessionCookie"
        client.post(
            "/accounts/refresh",
            json={"account_id": aid, "li_at": new_li_at},
        )
        # Use auth/check indirectly — or read storage directly
        import apps.api.main as api_mod

        auth = api_mod.storage.get_account_auth(aid)
        assert auth.li_at == new_li_at


class TestRefreshWithCookieString:
    def test_refresh_with_cookies_string(self, client):
        aid = _create_account(client)
        resp = client.post(
            "/accounts/refresh",
            json={"account_id": aid, "cookies": "li_at=AQEDNewCookieXXX; JSESSIONID=ajax:tok456"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_cookies_overrides_raw_fields(self, client):
        aid = _create_account(client)
        resp = client.post(
            "/accounts/refresh",
            json={
                "account_id": aid,
                "li_at": "should_be_ignored",
                "cookies": "li_at=AQEDFromCookieString",
            },
        )
        assert resp.status_code == 200
        import apps.api.main as api_mod

        auth = api_mod.storage.get_account_auth(aid)
        assert auth.li_at == "AQEDFromCookieString"


class TestRefreshValidation:
    def test_missing_auth_rejected(self, client):
        aid = _create_account(client)
        resp = client.post("/accounts/refresh", json={"account_id": aid})
        assert resp.status_code == 422

    def test_short_li_at_rejected(self, client):
        aid = _create_account(client)
        resp = client.post("/accounts/refresh", json={"account_id": aid, "li_at": "abc"})
        assert resp.status_code == 422

    def test_empty_li_at_rejected(self, client):
        aid = _create_account(client)
        resp = client.post("/accounts/refresh", json={"account_id": aid, "li_at": ""})
        assert resp.status_code == 422

    def test_nonexistent_account_returns_404(self, client):
        resp = client.post(
            "/accounts/refresh",
            json={"account_id": 99999, "li_at": "AQEDNewCookieValue123"},
        )
        assert resp.status_code == 404

    def test_cookies_without_li_at_rejected(self, client):
        aid = _create_account(client)
        resp = client.post(
            "/accounts/refresh",
            json={"account_id": aid, "cookies": "JSESSIONID=ajax:tok123"},
        )
        assert resp.status_code == 422


class TestSessionExpired401:
    """Verify /send and /sync return 401 with refresh hint when provider raises PermissionError."""

    def test_send_returns_401_on_expired_session(self, client):
        aid = _create_account(client)
        with patch("apps.api.main.run_send", side_effect=PermissionError("LinkedIn session expired (HTTP 401)")):
            resp = client.post(
                "/send",
                json={"account_id": aid, "recipient": "urn:li:member:123", "text": "hello"},
            )
        assert resp.status_code == 401
        assert "re-authenticate via POST /accounts/refresh" in resp.json()["detail"]

    def test_sync_returns_401_on_expired_session(self, client):
        aid = _create_account(client)
        with patch("apps.api.main.run_sync", side_effect=PermissionError("LinkedIn session expired (HTTP 401)")):
            resp = client.post(
                "/sync",
                json={"account_id": aid},
            )
        assert resp.status_code == 401
        assert "re-authenticate via POST /accounts/refresh" in resp.json()["detail"]

    def test_send_401_then_refresh_then_send_succeeds(self, client):
        """Full flow: send fails with 401, client refreshes cookies, send succeeds."""
        aid = _create_account(client)

        # First send fails with expired session
        with patch("apps.api.main.run_send", side_effect=PermissionError("HTTP 401")):
            resp = client.post(
                "/send",
                json={"account_id": aid, "recipient": "urn:li:member:123", "text": "hello"},
            )
        assert resp.status_code == 401

        # Client refreshes cookies
        resp = client.post(
            "/accounts/refresh",
            json={"account_id": aid, "li_at": "AQEDFreshNewCookie123"},
        )
        assert resp.status_code == 200

        # Second send succeeds with new cookies
        from libs.core.job_runner import SendResult

        mock_result = SendResult(
            send_id=1,
            platform_message_id="msg-id-123",
            status="sent",
            was_duplicate=False,
        )
        with patch("apps.api.main.run_send", return_value=mock_result):
            resp = client.post(
                "/send",
                json={"account_id": aid, "recipient": "urn:li:member:123", "text": "hello"},
            )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
