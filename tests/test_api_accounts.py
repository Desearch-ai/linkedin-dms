"""Tests for the /accounts endpoint — cookie import formats and validation."""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from libs.core import crypto


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Use a temp DB and reset crypto warning flag."""
    monkeypatch.setenv("DESEARCH_DB_PATH", str(tmp_path / "test.sqlite"))
    monkeypatch.delenv("DESEARCH_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("DESEARCH_API_TOKEN", raising=False)
    crypto._warned_no_key = False


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DESEARCH_DB_PATH", str(tmp_path / "test.sqlite"))
    monkeypatch.delenv("DESEARCH_API_TOKEN", raising=False)
    # Re-import to pick up fresh Storage with tmp db
    # We patch Storage init to use tmp_path
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


class TestCreateAccountRawFields:
    def test_create_with_li_at(self, client):
        resp = client.post(
            "/accounts",
            json={"label": "test", "li_at": "AQEDAWx0Y29va2llXXX"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "account_id" in data
        assert isinstance(data["account_id"], int)

    def test_create_with_li_at_and_jsessionid(self, client):
        resp = client.post(
            "/accounts",
            json={"label": "test", "li_at": "AQEDAWx0Y29va2llXXX", "jsessionid": "ajax:tok123"},
        )
        assert resp.status_code == 200

    def test_missing_auth_rejected(self, client):
        resp = client.post("/accounts", json={"label": "test"})
        assert resp.status_code == 422


class TestCreateAccountCookieString:
    def test_create_with_cookies_string(self, client):
        resp = client.post(
            "/accounts",
            json={"label": "test", "cookies": "li_at=AQEDAWx0Y29va2llXXX; JSESSIONID=ajax:tok123"},
        )
        assert resp.status_code == 200
        assert "account_id" in resp.json()

    def test_cookies_string_without_li_at_rejected(self, client):
        resp = client.post(
            "/accounts",
            json={"label": "test", "cookies": "JSESSIONID=ajax:tok123"},
        )
        assert resp.status_code == 422

    def test_cookies_overrides_raw_fields(self, client):
        resp = client.post(
            "/accounts",
            json={
                "label": "test",
                "li_at": "should_be_ignored",
                "cookies": "li_at=AQEDAWx0Y29va2llXXX",
            },
        )
        assert resp.status_code == 200


class TestCreateAccountValidation:
    def test_short_li_at_rejected(self, client):
        resp = client.post("/accounts", json={"label": "test", "li_at": "abc"})
        assert resp.status_code == 422

    def test_empty_li_at_rejected(self, client):
        resp = client.post("/accounts", json={"label": "test", "li_at": ""})
        assert resp.status_code == 422


class TestCreateAccountApiAuth:
    def test_create_requires_bearer_token_when_configured(self, client, monkeypatch):
        monkeypatch.setenv("DESEARCH_API_TOKEN", "secret-token")
        resp = client.post(
            "/accounts",
            json={"label": "test", "li_at": "AQEDAWx0Y29va2llXXX"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Missing or invalid bearer token"

    def test_create_accepts_bearer_token_when_configured(self, client, monkeypatch):
        monkeypatch.setenv("DESEARCH_API_TOKEN", "secret-token")
        resp = client.post(
            "/accounts",
            headers={"Authorization": "Bearer secret-token"},
            json={"label": "test", "li_at": "AQEDAWx0Y29va2llXXX"},
        )
        assert resp.status_code == 200
        assert "account_id" in resp.json()
