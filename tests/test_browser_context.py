"""End-to-end threading of Chrome-extension browser-captured request context
(``x-li-track`` / ``csrf-token``) through API -> core -> provider (issue #54).
"""
from __future__ import annotations

import json
from dataclasses import asdict
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from libs.core import crypto
from libs.core.job_runner import run_send, run_sync
from libs.core.models import AccountAuth
from libs.core.storage import Storage
from libs.providers.linkedin.provider import LinkedInProvider


# ---------------------------------------------------------------------------
# AccountAuth field + normalization
# ---------------------------------------------------------------------------


class TestAccountAuthFields:
    def test_new_fields_default_to_none(self):
        auth = AccountAuth(li_at="x")
        assert auth.x_li_track is None
        assert auth.csrf_token is None

    def test_legacy_kwargs_still_construct(self):
        """Existing callsites passing only li_at/jsessionid must keep working."""
        auth = AccountAuth(li_at="x", jsessionid="ajax:tok")
        assert auth.li_at == "x"
        assert auth.jsessionid == "ajax:tok"

    def test_whitespace_only_normalized_to_none(self):
        auth = AccountAuth(li_at="x", x_li_track="   ", csrf_token="\t\n")
        assert auth.x_li_track is None
        assert auth.csrf_token is None

    def test_values_trimmed(self):
        auth = AccountAuth(li_at="x", x_li_track="  {\"v\":1}  ", csrf_token=" ajax:tok ")
        assert auth.x_li_track == '{"v":1}'
        assert auth.csrf_token == "ajax:tok"

    def test_repr_redacts_new_fields(self):
        auth = AccountAuth(
            li_at="SECRET_LI_AT",
            jsessionid="ajax:tok",
            x_li_track='{"clientVersion":"1.13.42912"}',
            csrf_token="ajax:CSRF_SECRET",
        )
        r = repr(auth)
        assert "SECRET_LI_AT" not in r
        assert "ajax:tok" not in r
        assert "1.13.42912" not in r
        assert "CSRF_SECRET" not in r
        assert "REDACTED" in r

    def test_asdict_roundtrip(self):
        """dataclasses.asdict -> json -> AccountAuth(**d) must preserve new fields.

        This is the exact path Storage uses to persist AccountAuth.
        """
        original = AccountAuth(
            li_at="li",
            jsessionid="ajax:j",
            x_li_track='{"clientVersion":"1.13.42912"}',
            csrf_token="ajax:j",
        )
        restored = AccountAuth(**json.loads(json.dumps(asdict(original))))
        assert restored == original

    def test_legacy_json_missing_new_keys_loads(self):
        """Rows written before this change store only li_at/jsessionid."""
        legacy = {"li_at": "x", "jsessionid": "ajax:j"}
        restored = AccountAuth(**legacy)
        assert restored.x_li_track is None
        assert restored.csrf_token is None


# ---------------------------------------------------------------------------
# Storage persistence (round-trip through create_account / get_account_auth)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _plaintext_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("DESEARCH_DB_PATH", str(tmp_path / "bctx.sqlite"))
    monkeypatch.delenv("DESEARCH_ENCRYPTION_KEY", raising=False)
    crypto._warned_no_key = False


@pytest.fixture
def storage(tmp_path):
    s = Storage(db_path=tmp_path / "bctx.sqlite")
    s.migrate()
    yield s
    s.close()


class TestStoragePersistence:
    def test_create_account_persists_new_fields(self, storage):
        auth = AccountAuth(
            li_at="li",
            jsessionid="ajax:j",
            x_li_track='{"clientVersion":"1.13.42912"}',
            csrf_token="ajax:CSRF",
        )
        aid = storage.create_account(label="a", auth=auth, proxy=None)
        got = storage.get_account_auth(aid)
        assert got.x_li_track == '{"clientVersion":"1.13.42912"}'
        assert got.csrf_token == "ajax:CSRF"

    def test_update_account_auth_replaces_new_fields(self, storage):
        aid = storage.create_account(label="a", auth=AccountAuth(li_at="li"), proxy=None)
        storage.update_account_auth(
            aid,
            AccountAuth(li_at="li", x_li_track="T1", csrf_token="C1"),
        )
        assert storage.get_account_auth(aid).x_li_track == "T1"
        storage.update_account_auth(
            aid,
            AccountAuth(li_at="li", x_li_track="T2", csrf_token="C2"),
        )
        got = storage.get_account_auth(aid)
        assert got.x_li_track == "T2"
        assert got.csrf_token == "C2"

    def test_partial_capture_persists_independently(self, storage):
        aid = storage.create_account(
            label="a",
            auth=AccountAuth(li_at="li", x_li_track="ONLY_TRACK"),
            proxy=None,
        )
        got = storage.get_account_auth(aid)
        assert got.x_li_track == "ONLY_TRACK"
        assert got.csrf_token is None


# ---------------------------------------------------------------------------
# Provider: header precedence (runtime > auth > default)
# ---------------------------------------------------------------------------


class TestProviderHeaderPrecedence:
    def _provider(self, **auth_kwargs):
        return LinkedInProvider(
            auth=AccountAuth(li_at="li", jsessionid="ajax:j", **auth_kwargs)
        )

    def test_graphql_headers_use_captured_values(self):
        p = self._provider(x_li_track="CAPTURED_TRACK", csrf_token="CAPTURED_CSRF")
        headers = p._build_graphql_headers()
        assert headers["x-li-track"] == "CAPTURED_TRACK"
        assert headers["csrf-token"] == "CAPTURED_CSRF"

    def test_graphql_headers_fall_back_to_jsessionid_for_csrf(self):
        p = self._provider()
        headers = p._build_graphql_headers()
        assert headers["csrf-token"] == "ajax:j"
        assert "clientVersion" in headers["x-li-track"]

    def test_send_headers_use_captured_values(self):
        p = self._provider(x_li_track="CAPTURED_TRACK", csrf_token="CAPTURED_CSRF")
        headers = p._build_headers()
        assert headers["x-li-track"] == "CAPTURED_TRACK"
        assert headers["csrf-token"] == "CAPTURED_CSRF"

    def test_runtime_override_wins_over_stored(self):
        p = self._provider(x_li_track="STORED_TRACK", csrf_token="STORED_CSRF")
        p.update_browser_context(x_li_track="RUNTIME_TRACK", csrf_token="RUNTIME_CSRF")
        headers = p._build_graphql_headers()
        assert headers["x-li-track"] == "RUNTIME_TRACK"
        assert headers["csrf-token"] == "RUNTIME_CSRF"

    def test_runtime_override_empty_does_not_clobber(self):
        """Partial captures (only one header present) must not wipe the other."""
        p = self._provider(x_li_track="STORED_TRACK", csrf_token="STORED_CSRF")
        p.update_browser_context(x_li_track=None, csrf_token="FRESH_CSRF")
        headers = p._build_graphql_headers()
        assert headers["x-li-track"] == "STORED_TRACK"
        assert headers["csrf-token"] == "FRESH_CSRF"

    def test_runtime_override_blank_string_ignored(self):
        p = self._provider(x_li_track="STORED_TRACK")
        p.update_browser_context(x_li_track="   ", csrf_token="")
        assert p._build_graphql_headers()["x-li-track"] == "STORED_TRACK"

    def test_profile_id_request_uses_captured_track(self):
        """The /voyager/api/me bootstrap that was returning 302 on live main."""
        p = self._provider(x_li_track="FRESH_TRACK", csrf_token="FRESH_CSRF")
        captured: dict[str, dict[str, str]] = {}

        class _FakeResp:
            status_code = 500  # terminate early; we only care about headers

            def json(self):  # pragma: no cover - not reached
                return {}

        class _FakeClient:
            def get(self, url, headers, cookies):
                captured["headers"] = headers
                return _FakeResp()

            @property
            def is_closed(self):
                return False

        p._client = _FakeClient()
        p._get_profile_id()
        assert captured["headers"]["x-li-track"] == "FRESH_TRACK"
        assert captured["headers"]["csrf-token"] == "FRESH_CSRF"


# ---------------------------------------------------------------------------
# Job runner: kwargs propagate to provider before network calls
# ---------------------------------------------------------------------------


class TestJobRunnerPropagation:
    def _mock_provider(self):
        p = MagicMock(spec=LinkedInProvider)
        p.rate_limit_encountered = False
        p.list_threads.return_value = []
        return p

    def test_run_sync_calls_update_browser_context_before_list_threads(self, storage):
        aid = storage.create_account(label="a", auth=AccountAuth(li_at="li"), proxy=None)
        provider = self._mock_provider()
        run_sync(
            account_id=aid,
            storage=storage,
            provider=provider,
            x_li_track="RUNTIME_TRACK",
            csrf_token="RUNTIME_CSRF",
        )
        provider.update_browser_context.assert_called_once_with(
            x_li_track="RUNTIME_TRACK",
            csrf_token="RUNTIME_CSRF",
        )
        # Must run before network work begins.
        names = [c[0] for c in provider.method_calls]
        assert names.index("update_browser_context") < names.index("list_threads")

    def test_run_sync_defaults_are_none(self, storage):
        aid = storage.create_account(label="a", auth=AccountAuth(li_at="li"), proxy=None)
        provider = self._mock_provider()
        run_sync(account_id=aid, storage=storage, provider=provider)
        provider.update_browser_context.assert_called_once_with(
            x_li_track=None, csrf_token=None,
        )

    def test_run_send_propagates_overrides(self, storage):
        aid = storage.create_account(label="a", auth=AccountAuth(li_at="li"), proxy=None)
        provider = MagicMock(spec=LinkedInProvider)
        provider.send_message.return_value = "urn:li:msg:1"
        run_send(
            account_id=aid,
            storage=storage,
            provider=provider,
            recipient="urn:li:member:1",
            text="hi",
            idempotency_key=None,
            x_li_track="T",
            csrf_token="C",
        )
        provider.update_browser_context.assert_called_once_with(
            x_li_track="T", csrf_token="C",
        )


# ---------------------------------------------------------------------------
# API surface: request models accept + persist the new fields
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DESEARCH_DB_PATH", str(tmp_path / "api.sqlite"))
    monkeypatch.delenv("DESEARCH_API_TOKEN", raising=False)
    s = Storage(db_path=tmp_path / "api.sqlite")
    s.migrate()

    import apps.api.main as api_mod
    from apps.api.main import app

    original = api_mod.storage
    api_mod.storage = s
    yield TestClient(app)
    api_mod.storage = original
    s.close()


class TestApiCreateAndRefresh:
    def test_create_persists_captured_headers(self, client):
        resp = client.post(
            "/accounts",
            json={
                "label": "t",
                "li_at": "AQEDAWx0Y29va2llXXX",
                "x_li_track": '{"clientVersion":"1.13.42912"}',
                "csrf_token": "ajax:CSRF123",
            },
        )
        assert resp.status_code == 200
        aid = resp.json()["account_id"]

        import apps.api.main as api_mod
        got = api_mod.storage.get_account_auth(aid)
        assert got.x_li_track == '{"clientVersion":"1.13.42912"}'
        assert got.csrf_token == "ajax:CSRF123"

    def test_refresh_updates_captured_headers(self, client):
        aid = client.post(
            "/accounts",
            json={"label": "t", "li_at": "AQEDAWx0Y29va2llXXX"},
        ).json()["account_id"]
        resp = client.post(
            "/accounts/refresh",
            json={
                "account_id": aid,
                "li_at": "AQEDAWx0Y29va2llNEW",
                "x_li_track": "FRESH_TRACK",
                "csrf_token": "FRESH_CSRF",
            },
        )
        assert resp.status_code == 200

        import apps.api.main as api_mod
        got = api_mod.storage.get_account_auth(aid)
        assert got.x_li_track == "FRESH_TRACK"
        assert got.csrf_token == "FRESH_CSRF"

    def test_refresh_without_headers_preserves_stored_headers(self, client):
        """Reviewer regression (#57): rotating only cookies must not wipe the
        previously persisted browser-context headers that this PR introduces
        as a fallback.
        """
        aid = client.post(
            "/accounts",
            json={
                "label": "t",
                "li_at": "AQEDAWx0Y29va2llXXX",
                "x_li_track": "STORED_TRACK",
                "csrf_token": "STORED_CSRF",
            },
        ).json()["account_id"]

        resp = client.post(
            "/accounts/refresh",
            json={"account_id": aid, "li_at": "AQEDAWx0Y29va2llNEW"},
        )
        assert resp.status_code == 200

        import apps.api.main as api_mod
        got = api_mod.storage.get_account_auth(aid)
        assert got.li_at == "AQEDAWx0Y29va2llNEW"
        assert got.x_li_track == "STORED_TRACK"
        assert got.csrf_token == "STORED_CSRF"

    def test_refresh_partial_headers_preserves_the_other(self, client):
        """Supplying only one captured header must not wipe the other."""
        aid = client.post(
            "/accounts",
            json={
                "label": "t",
                "li_at": "AQEDAWx0Y29va2llXXX",
                "x_li_track": "STORED_TRACK",
                "csrf_token": "STORED_CSRF",
            },
        ).json()["account_id"]

        resp = client.post(
            "/accounts/refresh",
            json={
                "account_id": aid,
                "li_at": "AQEDAWx0Y29va2llNEW",
                "csrf_token": "FRESH_CSRF",
            },
        )
        assert resp.status_code == 200

        import apps.api.main as api_mod
        got = api_mod.storage.get_account_auth(aid)
        assert got.x_li_track == "STORED_TRACK"
        assert got.csrf_token == "FRESH_CSRF"

    def test_refresh_blank_headers_treated_as_missing(self, client):
        """Whitespace-only captures normalize to None and must not clobber."""
        aid = client.post(
            "/accounts",
            json={
                "label": "t",
                "li_at": "AQEDAWx0Y29va2llXXX",
                "x_li_track": "STORED_TRACK",
                "csrf_token": "STORED_CSRF",
            },
        ).json()["account_id"]

        resp = client.post(
            "/accounts/refresh",
            json={
                "account_id": aid,
                "li_at": "AQEDAWx0Y29va2llNEW",
                "x_li_track": "   ",
                "csrf_token": "",
            },
        )
        assert resp.status_code == 200

        import apps.api.main as api_mod
        got = api_mod.storage.get_account_auth(aid)
        assert got.x_li_track == "STORED_TRACK"
        assert got.csrf_token == "STORED_CSRF"

    def test_refresh_unknown_account_returns_404(self, client):
        resp = client.post(
            "/accounts/refresh",
            json={"account_id": 9999, "li_at": "AQEDAWx0Y29va2llXXX"},
        )
        assert resp.status_code == 404

    def test_cookies_path_also_accepts_headers(self, client):
        """Verify the cookies-string branch of to_account_auth() merges context."""
        resp = client.post(
            "/accounts",
            json={
                "label": "t",
                "cookies": "li_at=AQEDAWx0Y29va2llXXX; JSESSIONID=ajax:j",
                "x_li_track": "T_VIA_COOKIES",
                "csrf_token": "C_VIA_COOKIES",
            },
        )
        assert resp.status_code == 200
        aid = resp.json()["account_id"]
        import apps.api.main as api_mod
        got = api_mod.storage.get_account_auth(aid)
        assert got.x_li_track == "T_VIA_COOKIES"
        assert got.csrf_token == "C_VIA_COOKIES"


class TestApiSyncRuntimeOverride:
    def test_sync_forwards_runtime_headers_to_run_sync(self, client, monkeypatch):
        aid = client.post(
            "/accounts",
            json={"label": "t", "li_at": "AQEDAWx0Y29va2llXXX"},
        ).json()["account_id"]

        captured_kwargs: dict = {}

        def _fake_run_sync(**kwargs):
            captured_kwargs.update(kwargs)
            from libs.core.job_runner import SyncResult
            return SyncResult(0, 0, 0, 0, False)

        monkeypatch.setattr("apps.api.main.run_sync", _fake_run_sync)
        resp = client.post(
            "/sync",
            json={
                "account_id": aid,
                "x_li_track": "RUNTIME_TRACK",
                "csrf_token": "RUNTIME_CSRF",
            },
        )
        assert resp.status_code == 200
        assert captured_kwargs["x_li_track"] == "RUNTIME_TRACK"
        assert captured_kwargs["csrf_token"] == "RUNTIME_CSRF"

    def test_sync_without_overrides_passes_none(self, client, monkeypatch):
        aid = client.post(
            "/accounts",
            json={"label": "t", "li_at": "AQEDAWx0Y29va2llXXX"},
        ).json()["account_id"]

        captured_kwargs: dict = {}

        def _fake_run_sync(**kwargs):
            captured_kwargs.update(kwargs)
            from libs.core.job_runner import SyncResult
            return SyncResult(0, 0, 0, 0, False)

        monkeypatch.setattr("apps.api.main.run_sync", _fake_run_sync)
        resp = client.post("/sync", json={"account_id": aid})
        assert resp.status_code == 200
        assert captured_kwargs["x_li_track"] is None
        assert captured_kwargs["csrf_token"] is None

    def test_send_forwards_runtime_headers_to_run_send(self, client, monkeypatch):
        aid = client.post(
            "/accounts",
            json={"label": "t", "li_at": "AQEDAWx0Y29va2llXXX"},
        ).json()["account_id"]

        captured_kwargs: dict = {}

        def _fake_run_send(**kwargs):
            captured_kwargs.update(kwargs)
            from libs.core.job_runner import SendResult
            return SendResult(send_id=1, platform_message_id="m", status="sent", was_duplicate=False)

        monkeypatch.setattr("apps.api.main.run_send", _fake_run_send)
        resp = client.post(
            "/send",
            json={
                "account_id": aid,
                "recipient": "urn:li:member:1",
                "text": "hi",
                "x_li_track": "RT",
                "csrf_token": "RC",
            },
        )
        assert resp.status_code == 200
        assert captured_kwargs["x_li_track"] == "RT"
        assert captured_kwargs["csrf_token"] == "RC"
