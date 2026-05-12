from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from apps.cli import __main__ as cli_main
from libs.core import crypto
from libs.core.redaction import redact_string
from libs.core.storage import Storage
from libs.providers.discord.provider import DiscordAPIError


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DESEARCH_DB_PATH", str(tmp_path / "discord.sqlite"))
    monkeypatch.delenv("DESEARCH_API_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_SYNC_CLIENT_ID", raising=False)
    monkeypatch.delenv("DISCORD_SYNC_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("DISCORD_SYNC_REDIRECT_URI", raising=False)
    monkeypatch.delenv("DISCORD_SYNC_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DESEARCH_ENCRYPTION_KEY", raising=False)
    crypto._warned_no_key = False


@pytest.fixture()
def storage(tmp_path) -> Storage:
    s = Storage(db_path=tmp_path / "discord.sqlite")
    s.migrate()
    yield s
    s.close()


@pytest.fixture()
def client(storage: Storage):
    import apps.api.main as api_mod

    original_storage = api_mod.storage
    api_mod.storage = storage
    yield TestClient(api_mod.app)
    api_mod.storage = original_storage


def _enable_discord_oauth(monkeypatch):
    monkeypatch.setenv("DISCORD_SYNC_CLIENT_ID", "client-123")
    monkeypatch.setenv("DISCORD_SYNC_CLIENT_SECRET", "client-credential-value")
    monkeypatch.setenv("DISCORD_SYNC_REDIRECT_URI", "http://localhost:8000/discord/auth/callback")


def test_discord_oauth_callback_stores_connected_identity_and_encrypted_token(client, storage, monkeypatch):
    _enable_discord_oauth(monkeypatch)
    monkeypatch.setenv("DESEARCH_ENCRYPTION_KEY", Fernet.generate_key().decode())

    start = client.get("/discord/auth/start")
    assert start.status_code == 200
    state = start.json()["state"]
    assert "client_secret" not in start.text

    provider = MagicMock()
    provider.exchange_code.return_value = {
        "access_token": "access-token-value",
        "refresh_token": "refresh-token-value",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "identify guilds",
    }
    provider.get_current_user.return_value = {
        "id": "u1",
        "username": "cosmic",
        "global_name": "Giga",
    }

    with patch("apps.api.main._make_discord_provider", return_value=provider):
        resp = client.get(f"/discord/auth/callback?code=oauth-code&state={state}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["account"]["discord_user_id"] == "u1"
    assert body["account"]["status"] == "connected"
    assert "access-token-value" not in resp.text
    assert "refresh-token-value" not in resp.text

    token = storage.get_discord_account_token(body["account"]["id"])
    assert token is not None
    assert token["access_token"] == "access-token-value"




def test_discord_oauth_callback_uses_state_not_bearer_header(client, storage, monkeypatch):
    _enable_discord_oauth(monkeypatch)
    monkeypatch.setenv("DESEARCH_API_TOKEN", "local-api-token")
    monkeypatch.setenv("DESEARCH_ENCRYPTION_KEY", Fernet.generate_key().decode())

    start = client.get("/discord/auth/start", headers={"Authorization": "Bearer local-api-token"})
    assert start.status_code == 200
    state = start.json()["state"]

    provider = MagicMock()
    provider.exchange_code.return_value = {"access_token": "callback-token-value", "expires_in": 3600, "scope": "identify guilds"}
    provider.get_current_user.return_value = {"id": "u-callback", "username": "oauth-user"}

    with patch("apps.api.main._make_discord_provider", return_value=provider):
        resp = client.get(f"/discord/auth/callback?code=oauth-code&state={state}")

    assert resp.status_code == 200
    assert resp.json()["account"]["discord_user_id"] == "u-callback"


def test_discord_reconnect_without_encryption_clears_prior_persisted_token(storage, monkeypatch):
    monkeypatch.setenv("DESEARCH_ENCRYPTION_KEY", Fernet.generate_key().decode())
    account_id = storage.upsert_discord_account(
        discord_user_id="u1",
        username="cosmic",
        global_name="Giga",
        scopes=["identify", "guilds"],
        status="connected",
        token_material={"access_token": "old-token-value"},
        token_expires_at="2099-01-01T00:00:00+00:00",
    )
    assert storage.get_discord_account(account_id)["token_persisted"] is True

    monkeypatch.delenv("DESEARCH_ENCRYPTION_KEY", raising=False)
    storage.upsert_discord_account(
        discord_user_id="u1",
        username="cosmic",
        global_name="Giga",
        scopes=["identify", "guilds"],
        status="connected_token_not_persisted",
        token_material=None,
        token_expires_at="2099-01-01T01:00:00+00:00",
        last_error="DESEARCH_ENCRYPTION_KEY not configured; Discord OAuth token material was not persisted",
    )

    account = storage.get_discord_account(account_id)
    assert account["status"] == "connected_token_not_persisted"
    assert account["token_persisted"] is False
    assert storage.get_discord_account_token(account_id) is None

def test_discord_sync_guilds_channels_messages_dedupes_and_lists_live_rows(client, storage, monkeypatch):
    monkeypatch.setenv("DESEARCH_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DISCORD_SYNC_BOT_TOKEN", "bot-credential-value")
    account_id = storage.upsert_discord_account(
        discord_user_id="u1",
        username="cosmic",
        global_name="Giga",
        scopes=["identify", "guilds"],
        status="connected",
        token_material={"access_token": "oauth-token-value", "refresh_token": "oauth-refresh-value", "expires_at": "2099-01-01T00:00:00+00:00"},
        token_expires_at="2099-01-01T00:00:00+00:00",
    )

    provider = MagicMock()
    provider.list_user_guilds.return_value = [
        {"id": "g1", "name": "Desearch", "permissions": "8", "owner": True},
    ]
    provider.list_guild_channels.return_value = [
        {"id": "c1", "guild_id": "g1", "name": "general", "type": 0, "parent_id": None},
    ]
    provider.list_channel_messages.return_value = [
        {
            "id": "m1",
            "channel_id": "c1",
            "author": {"id": "u2", "username": "alice", "global_name": "Alice"},
            "content": "hello bittensor",
            "timestamp": "2026-05-12T08:00:00+00:00",
        }
    ]

    with patch("apps.api.main._make_discord_provider", return_value=provider):
        guilds = client.post("/discord/sync/guilds", json={"account_id": account_id})
        channels = client.post("/discord/sync/channels", json={"account_id": account_id, "guild_id": "g1"})
        first_messages = client.post("/discord/sync/messages", json={"account_id": account_id, "channel_id": "c1", "limit": 10})
        second_messages = client.post("/discord/sync/messages", json={"account_id": account_id, "channel_id": "c1", "limit": 10})

    assert guilds.json()["upserted"] == 1
    assert channels.json()["upserted"] == 1
    assert first_messages.json()["inserted"] == 1
    assert first_messages.json()["duplicates"] == 0
    assert second_messages.json()["inserted"] == 0
    assert second_messages.json()["duplicates"] == 1

    listed = client.get("/discord/messages?channel_id=c1&q=bittensor")
    assert listed.status_code == 200
    assert listed.json()["messages"][0]["platform_message_id"] == "m1"
    assert listed.json()["messages"][0]["source"] == "live"


def test_discord_permission_errors_are_stored_as_actionable_rows(client, storage, monkeypatch):
    monkeypatch.setenv("DISCORD_SYNC_BOT_TOKEN", "bot-credential-value")
    account_id = storage.upsert_discord_account(
        discord_user_id="u1",
        username="cosmic",
        global_name=None,
        scopes=["identify", "guilds"],
        status="connected",
        token_material=None,
        token_expires_at=None,
    )
    storage.upsert_discord_guild(
        account_id=account_id,
        guild={"id": "g1", "name": "Private", "permissions": "0"},
        provenance="oauth:guilds",
    )

    provider = MagicMock()
    provider.list_guild_channels.side_effect = DiscordAPIError(403, "Missing Access", route="GET /guilds/g1/channels")

    with patch("apps.api.main._make_discord_provider", return_value=provider):
        resp = client.post("/discord/sync/channels", json={"account_id": account_id, "guild_id": "g1"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    errors = client.get("/discord/errors?account_id=" + str(account_id)).json()["errors"]
    assert errors[0]["scope"] == "channels"
    assert errors[0]["status_code"] == 403
    assert "Missing Access" in errors[0]["message"]
    assert "bot-credential-value" not in resp.text


def test_discord_callback_without_encryption_connects_identity_but_disables_token_persistence(client, storage, monkeypatch):
    _enable_discord_oauth(monkeypatch)
    start = client.get("/discord/auth/start")
    state = start.json()["state"]
    provider = MagicMock()
    provider.exchange_code.return_value = {"access_token": "plain-token-value", "refresh_token": "plain-refresh-value", "expires_in": 3600, "scope": "identify guilds"}
    provider.get_current_user.return_value = {"id": "u1", "username": "cosmic"}

    with patch("apps.api.main._make_discord_provider", return_value=provider):
        resp = client.get(f"/discord/auth/callback?code=oauth-code&state={state}")

    assert resp.status_code == 200
    account = resp.json()["account"]
    assert account["status"] == "connected_token_not_persisted"
    assert account["token_persisted"] is False
    assert storage.get_discord_account_token(account["id"]) is None
    assert "plain-token-value" not in resp.text


def test_discord_redaction_covers_oauth_and_bot_secret_names():
    redacted = redact_string("access_token=abc refresh_token=def bot_token=ghi client_secret=jkl DISCORD_SYNC_BOT_TOKEN=mno")
    assert "abc" not in redacted
    assert "def" not in redacted
    assert "ghi" not in redacted
    assert "jkl" not in redacted
    assert "mno" not in redacted
    assert redacted.count("[REDACTED]") >= 5


def test_cli_discord_help_exposes_real_live_commands(capsys):
    rc = cli_main.main(["discord", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "auth-url" in out
    assert "auth-status" in out
    assert "sync-guilds" in out
    assert "sync-channels" in out
    assert "sync-messages" in out
    assert "fixture-ingest" not in out


def test_discord_ui_exposes_live_auth_fetch_controls(client):
    resp = client.get("/discord")
    assert resp.status_code == 200
    html = resp.text
    assert "/discord/auth/start" in html
    assert "/discord/auth/status" in html
    assert "/discord/sync/guilds" in html
    assert "/discord/sync/channels" in html
    assert "/discord/sync/messages" in html
    assert "live API MVP" in html


def test_discord_session_connect_fetches_identity_and_persists_encrypted_session(client, storage, monkeypatch):
    monkeypatch.setenv("DESEARCH_ENCRYPTION_KEY", Fernet.generate_key().decode())
    provider = MagicMock()
    provider.get_current_user.return_value = {"id": "u-session", "username": "cosmic", "global_name": "Giga"}

    with patch("apps.api.main._make_discord_session_provider", return_value=provider):
        resp = client.post(
            "/discord/session/connect",
            json={"cookie_header": "__dcfduid=session-cookie-value; locale=en-US", "user_agent": "Mozilla/5.0"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["account"]["discord_user_id"] == "u-session"
    assert body["account"]["status"] == "session_connected"
    assert body["account"]["scopes"] == ["session:web"]
    assert body["account"]["token_persisted"] is True
    assert "session-cookie-value" not in resp.text
    persisted = storage.get_discord_account_session(body["account"]["id"])
    assert persisted is not None
    assert persisted["kind"] == "session:web"
    assert "session-cookie-value" in persisted["cookie_header"]


def test_discord_session_connect_without_encryption_does_not_store_cookie_and_blocks_later_sync(client, storage):
    provider = MagicMock()
    provider.get_current_user.return_value = {"id": "u-ephemeral", "username": "cosmic"}

    with patch("apps.api.main._make_discord_session_provider", return_value=provider):
        resp = client.post("/discord/session/connect", json={"cookie_header": "discord_session=secret-value"})

    assert resp.status_code == 200
    account = resp.json()["account"]
    assert account["status"] == "session_connected_not_persisted"
    assert account["token_persisted"] is False
    assert "secret-value" not in resp.text
    assert storage.get_discord_account_session(account["id"]) is None

    sync = client.post("/discord/sync/guilds", json={"account_id": account["id"]})
    assert sync.status_code == 200
    assert sync.json()["ok"] is False
    assert "session:web material is not persisted" in sync.json()["errors"][0]["message"]
    assert "secret-value" not in sync.text


def test_discord_session_sync_guilds_channels_messages_dedupes_and_marks_session_web(client, storage, monkeypatch):
    monkeypatch.setenv("DESEARCH_ENCRYPTION_KEY", Fernet.generate_key().decode())
    account_id = storage.upsert_discord_account(
        discord_user_id="u1",
        username="cosmic",
        global_name="Giga",
        scopes=["session:web"],
        status="session_connected",
        token_material={"kind": "session:web", "cookie_header": "discord_session=secret-value", "user_agent": "Mozilla/5.0"},
        token_expires_at=None,
    )

    provider = MagicMock()
    provider.list_user_guilds.return_value = [{"id": "g1", "name": "Desearch", "permissions": "8", "owner": True}]
    provider.list_guild_channels.return_value = [{"id": "c1", "guild_id": "g1", "name": "general", "type": 0, "parent_id": None}]
    provider.list_channel_messages.return_value = [
        {
            "id": "m1",
            "channel_id": "c1",
            "author": {"id": "u2", "username": "alice", "global_name": "Alice"},
            "content": "hello bittensor",
            "timestamp": "2026-05-12T08:00:00+00:00",
        }
    ]

    with patch("apps.api.main._make_discord_session_provider", return_value=provider):
        guilds = client.post("/discord/sync/guilds", json={"account_id": account_id})
        channels = client.post("/discord/sync/channels", json={"account_id": account_id, "guild_id": "g1"})
        first_messages = client.post("/discord/sync/messages", json={"account_id": account_id, "channel_id": "c1", "limit": 10})
        second_messages = client.post("/discord/sync/messages", json={"account_id": account_id, "channel_id": "c1", "limit": 10})

    assert guilds.json()["upserted"] == 1
    assert channels.json()["upserted"] == 1
    assert first_messages.json()["inserted"] == 1
    assert second_messages.json()["duplicates"] == 1
    assert provider.list_guild_channels.call_args.kwargs == {}
    assert provider.list_channel_messages.call_args.kwargs == {"limit": 10, "before": None, "after": None}

    guild = client.get(f"/discord/guilds?account_id={account_id}").json()["guilds"][0]
    channel = client.get(f"/discord/channels?account_id={account_id}&guild_id=g1").json()["channels"][0]
    message = client.get("/discord/messages?channel_id=c1&q=bittensor").json()["messages"][0]
    assert guild["provenance"] == "session:web"
    assert channel["provenance"] == "session:web"
    assert message["provenance"] == "session:web"
    assert message["source"] == "live"


def test_discord_session_permission_errors_are_stored_as_actionable_rows(client, storage, monkeypatch):
    monkeypatch.setenv("DESEARCH_ENCRYPTION_KEY", Fernet.generate_key().decode())
    account_id = storage.upsert_discord_account(
        discord_user_id="u1",
        username="cosmic",
        global_name=None,
        scopes=["session:web"],
        status="session_connected",
        token_material={"kind": "session:web", "cookie_header": "discord_session=secret-value"},
        token_expires_at=None,
    )
    provider = MagicMock()
    provider.list_guild_channels.side_effect = DiscordAPIError(403, "Missing Access cookie_header=secret-value", route="GET /guilds/g1/channels (session:web)")

    with patch("apps.api.main._make_discord_session_provider", return_value=provider):
        resp = client.post("/discord/sync/channels", json={"account_id": account_id, "guild_id": "g1"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    errors = client.get("/discord/errors?account_id=" + str(account_id)).json()["errors"]
    assert errors[0]["scope"] == "channels"
    assert errors[0]["status_code"] == 403
    assert "Missing Access" in errors[0]["message"]
    assert "secret-value" not in resp.text
    assert "secret-value" not in json.dumps(errors)


def test_discord_session_redaction_covers_cookie_session_secret_names():
    redacted = redact_string("cookie_header=abc cookies=def discord_authorization=ghi DISCORD_SESSION_COOKIE=jkl x_super_properties=mno")
    for secret in ("abc", "def", "ghi", "jkl", "mno"):
        assert secret not in redacted
    assert redacted.count("[REDACTED]") >= 5


def test_discord_session_provider_uses_read_only_web_gets_and_normalizes_shapes():
    import httpx
    from libs.providers.discord.session_provider import DiscordSessionAuth, DiscordSessionProvider

    seen: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.headers)))
        if request.url.path.endswith("/users/@me"):
            return httpx.Response(200, json={"id": "u1", "username": "cosmic"})
        if request.url.path.endswith("/users/@me/guilds"):
            return httpx.Response(200, json=[{"id": "g1", "name": "Desearch"}])
        if request.url.path.endswith("/guilds/g1/channels"):
            return httpx.Response(200, json=[{"id": "c1", "name": "general", "type": 0}])
        if request.url.path.endswith("/channels/c1/messages"):
            assert request.url.params["limit"] == "25"
            return httpx.Response(200, json=[{"id": "m1", "content": "hello", "timestamp": "2026-05-12T08:00:00+00:00"}])
        return httpx.Response(404, json={"message": "not found"})

    provider = DiscordSessionProvider(
        auth=DiscordSessionAuth(cookie_header="discord_session=secret-value", user_agent="Mozilla/5.0"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert provider.get_current_user()["id"] == "u1"
    assert provider.list_user_guilds()[0]["id"] == "g1"
    assert provider.list_guild_channels("g1")[0]["id"] == "c1"
    assert provider.list_channel_messages("c1", limit=25)[0]["id"] == "m1"
    assert {method for method, _, _ in seen} == {"GET"}
    assert seen[0][2]["cookie"] == "discord_session=secret-value"
    assert not any(hasattr(provider, name) for name in ("send_message", "create_message", "delete_message", "react", "join_guild"))


def test_cli_and_ui_label_discord_as_session_web_not_bot_required(client, capsys):
    rc = cli_main.main(["discord", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "session-connect" in out
    assert "session/web" in out
    assert "bot-authorized" not in out

    html = client.get("/discord").text
    assert "session/web" in html
    assert "/discord/session/connect" in html
    assert "bot-token" not in html.lower()
