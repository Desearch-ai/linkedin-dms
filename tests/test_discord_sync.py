"""Discord Sync fixture prototype tests."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.cli import __main__ as cli_main
from libs.core.storage import Storage


@pytest.fixture
def discord_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "discord-sync.sqlite")


@pytest.fixture
def discord_storage(discord_db_path: str) -> Storage:
    storage = Storage(db_path=discord_db_path)
    storage.migrate()
    yield storage
    storage.close()


def test_discord_fixture_ingest_is_idempotent_and_persists_minimum_dataset(discord_storage: Storage) -> None:
    first = discord_storage.ingest_discord_fixtures()
    second = discord_storage.ingest_discord_fixtures()

    assert first == {
        "accounts": 2,
        "guilds": 2,
        "channels": 3,
        "users": 5,
        "members": 6,
        "messages": 20,
        "lead_signals": 3,
    }
    assert second == first

    conn = sqlite3.connect(discord_storage.db_path)
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "discord_accounts",
            "discord_guilds",
            "discord_channels",
            "discord_users",
            "discord_members",
            "discord_messages",
            "discord_lead_signals",
        )
    }
    conn.close()
    assert counts == {
        "discord_accounts": 2,
        "discord_guilds": 2,
        "discord_channels": 3,
        "discord_users": 5,
        "discord_members": 6,
        "discord_messages": 20,
        "discord_lead_signals": 3,
    }


def test_discord_message_list_and_search_filters_by_account_guild_channel(discord_storage: Storage) -> None:
    discord_storage.ingest_discord_fixtures()

    messages = discord_storage.list_discord_messages(
        account_id="acct-growth", guild_id="guild-bittensor", channel_id="chan-alpha"
    )
    assert len(messages) == 8
    assert messages[0]["account_label"] == "Growth research bot fixture"
    assert messages[0]["guild_name"] == "Bittensor Builders"
    assert messages[0]["channel_name"] == "alpha-research"

    search = discord_storage.search_discord_messages("validator", guild_id="guild-bittensor")
    assert search
    assert all("validator" in (row["content"] or "").lower() for row in search)


def test_discord_cli_ingest_list_search_and_show_commands(capsys: pytest.CaptureFixture[str], discord_db_path: str) -> None:
    rc = cli_main.main(["discord", "fixture-ingest", "--db-path", discord_db_path])
    assert rc == 0
    ingest = json.loads(capsys.readouterr().out)
    assert ingest["ok"] is True
    assert ingest["counts"]["messages"] == 20

    rc = cli_main.main(["discord", "list-messages", "--db-path", discord_db_path, "--channel-id", "chan-alpha"])
    assert rc == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["ok"] is True
    assert len(listed["messages"]) == 8

    rc = cli_main.main(["discord", "search", "--db-path", discord_db_path, "--query", "lead"])
    assert rc == 0
    results = json.loads(capsys.readouterr().out)
    assert results["ok"] is True
    assert results["messages"]

    rc = cli_main.main(["discord", "show-commands"])
    assert rc == 0
    commands = json.loads(capsys.readouterr().out)
    assert commands["ok"] is True
    assert "discord fixture-ingest" in commands["commands"]
    assert "discord list-messages" in commands["commands"]
    assert "discord search" in commands["commands"]
    assert "discord show-commands" in commands["commands"]


def test_discord_api_read_only_contract_and_no_live_sync_endpoint(discord_storage: Storage) -> None:
    discord_storage.ingest_discord_fixtures()

    import apps.api.main as api_mod

    original_storage = api_mod.storage
    api_mod.storage = discord_storage
    try:
        client = TestClient(api_mod.app)
        accounts = client.get("/discord/accounts")
        assert accounts.status_code == 200
        assert len(accounts.json()["accounts"]) == 2

        messages = client.get(
            "/discord/messages",
            params={"account_id": "acct-growth", "guild_id": "guild-bittensor", "channel_id": "chan-alpha"},
        )
        assert messages.status_code == 200
        payload = messages.json()
        assert payload["ok"] is True
        assert len(payload["messages"]) == 8
        assert {"account_label", "guild_name", "channel_name", "author_display_name"} <= set(payload["messages"][0])

        signals = client.get("/discord/lead-signals")
        assert signals.status_code == 200
        assert len(signals.json()["lead_signals"]) == 3

        commands = client.get("/discord/commands")
        assert commands.status_code == 200
        assert "discord fixture-ingest" in commands.json()["commands"]

        rejected = client.post("/discord/sync", json={"token": "should-not-exist"})
        assert rejected.status_code == 404
    finally:
        api_mod.storage = original_storage


def test_discord_ui_static_assets_reference_commands_and_browser() -> None:
    root = Path("apps/ui")
    html = (root / "index.html").read_text()
    js = (root / "app.js").read_text()

    assert "Discord Sync" in html
    assert "discord fixture-ingest" in html
    assert "/discord/messages" in js
    assert "account_id" in js
    assert "guild_id" in js
    assert "channel_id" in js
