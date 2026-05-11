"""Fixture-only Discord Sync dataset and command metadata.

No live Discord API, gateway, browser automation, user token, cookie, or credential
material belongs in this module. These records are synthetic product-shape data
for local prototype browsing and tests.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

DISCORD_COMMANDS = [
    "discord fixture-ingest",
    "discord list-messages",
    "discord search",
    "discord show-commands",
    "discord fixture-ingest --db-path ./discord_sync.sqlite",
    "discord list-accounts --db-path ./discord_sync.sqlite",
    "discord list-guilds --db-path ./discord_sync.sqlite",
    "discord list-channels --db-path ./discord_sync.sqlite --guild-id guild-bittensor",
    "discord list-messages --db-path ./discord_sync.sqlite --account-id acct-growth --guild-id guild-bittensor --channel-id chan-alpha",
    "discord search --db-path ./discord_sync.sqlite --query validator",
    "discord show-commands",
]

_CREATED_AT = "2026-05-12T00:00:00+00:00"

DISCORD_FIXTURES: dict[str, list[dict[str, Any]]] = {
    "accounts": [
        {
            "id": "acct-growth",
            "label": "Growth research bot fixture",
            "account_type": "approved-bot-fixture",
            "approved_scope": "fixture-only read model; no credentials or live Discord access",
            "created_at": _CREATED_AT,
        },
        {
            "id": "acct-founder-export",
            "label": "Founder consented export fixture",
            "account_type": "consented-export-fixture",
            "approved_scope": "fixture-only imported export; no live sync",
            "created_at": _CREATED_AT,
        },
    ],
    "guilds": [
        {
            "id": "guild-bittensor",
            "name": "Bittensor Builders",
            "description": "Synthetic server for subnet operator and validator research signals.",
            "created_at": _CREATED_AT,
        },
        {
            "id": "guild-ai-founders",
            "name": "AI Founders Lab",
            "description": "Synthetic server for early-stage AI startup discovery signals.",
            "created_at": _CREATED_AT,
        },
    ],
    "channels": [
        {
            "id": "chan-alpha",
            "guild_id": "guild-bittensor",
            "name": "alpha-research",
            "kind": "text",
            "topic": "Subnet alpha, validator ops, launch research",
        },
        {
            "id": "chan-growth",
            "guild_id": "guild-bittensor",
            "name": "growth-leads",
            "kind": "text",
            "topic": "Lead requests and partner intro opportunities",
        },
        {
            "id": "chan-founder-intros",
            "guild_id": "guild-ai-founders",
            "name": "founder-intros",
            "kind": "text",
            "topic": "Founder requests, tool discovery, warm intro context",
        },
    ],
    "users": [
        {
            "id": "user-ada",
            "username": "ada_validator",
            "display_name": "Ada Validator",
            "profile_summary": "Validator operator comparing search and crawl quality tooling.",
        },
        {
            "id": "user-ben",
            "username": "ben_builder",
            "display_name": "Ben Builder",
            "profile_summary": "Subnet founder looking for monitoring and growth data.",
        },
        {
            "id": "user-cyra",
            "username": "cyra_growth",
            "display_name": "Cyra Growth",
            "profile_summary": "Growth lead tracking AI communities and warm intros.",
        },
        {
            "id": "user-dev",
            "username": "devrel_mira",
            "display_name": "Mira DevRel",
            "profile_summary": "DevRel operator evaluating Discord intelligence workflows.",
        },
        {
            "id": "user-eli",
            "username": "eli_founder",
            "display_name": "Eli Founder",
            "profile_summary": "AI founder searching for launch-partner evidence and messaging.",
        },
    ],
    "members": [
        {"guild_id": "guild-bittensor", "user_id": "user-ada", "roles": ["validator", "operator"], "joined_at": _CREATED_AT},
        {"guild_id": "guild-bittensor", "user_id": "user-ben", "roles": ["builder"], "joined_at": _CREATED_AT},
        {"guild_id": "guild-bittensor", "user_id": "user-cyra", "roles": ["growth"], "joined_at": _CREATED_AT},
        {"guild_id": "guild-ai-founders", "user_id": "user-cyra", "roles": ["growth"], "joined_at": _CREATED_AT},
        {"guild_id": "guild-ai-founders", "user_id": "user-dev", "roles": ["devrel"], "joined_at": _CREATED_AT},
        {"guild_id": "guild-ai-founders", "user_id": "user-eli", "roles": ["founder"], "joined_at": _CREATED_AT},
    ],
}

_MESSAGE_PLAN = [
    ("acct-growth", "guild-bittensor", "chan-alpha", "user-ada", "Validator dashboards still miss semantic search regressions after miner updates."),
    ("acct-growth", "guild-bittensor", "chan-alpha", "user-ben", "Looking for a crawl API that can explain why validator scores moved overnight."),
    ("acct-growth", "guild-bittensor", "chan-alpha", "user-cyra", "Lead signal: teams asking for subnet launch monitoring need evidence packs."),
    ("acct-growth", "guild-bittensor", "chan-alpha", "user-dev", "Could Discord topic tracking surface validator pain before support tickets arrive?"),
    ("acct-growth", "guild-bittensor", "chan-alpha", "user-ada", "Keyword idea: validator churn, search freshness, crawler reliability."),
    ("acct-growth", "guild-bittensor", "chan-alpha", "user-ben", "A local fixture browser would help review context before any Growth handoff."),
    ("acct-growth", "guild-bittensor", "chan-alpha", "user-cyra", "Need human approval before turning this lead into outbound messaging."),
    ("acct-growth", "guild-bittensor", "chan-alpha", "user-dev", "Discrawl-like account and guild filters are enough for the first prototype."),
    ("acct-growth", "guild-bittensor", "chan-growth", "user-cyra", "Lead: validator ops teams want weekly search-quality summaries."),
    ("acct-founder-export", "guild-bittensor", "chan-growth", "user-ben", "Partner request: compare Desearch API against generic SERP providers."),
    ("acct-growth", "guild-bittensor", "chan-growth", "user-ada", "Evidence should quote messages and keep channel provenance visible."),
    ("acct-founder-export", "guild-bittensor", "chan-growth", "user-cyra", "Growth draft should stay paused until Giga approves a campaign."),
    ("acct-founder-export", "guild-bittensor", "chan-growth", "user-dev", "No outbound Discord sends from this prototype, only read-only browsing."),
    ("acct-founder-export", "guild-ai-founders", "chan-founder-intros", "user-eli", "Founder intro request: need AI search API with citations for investor research."),
    ("acct-growth", "guild-ai-founders", "chan-founder-intros", "user-dev", "Topic signal: local Discord intelligence can package evidence for review."),
    ("acct-founder-export", "guild-ai-founders", "chan-founder-intros", "user-cyra", "Lead signal: founder asked for warm intro to search infrastructure teams."),
    ("acct-founder-export", "guild-ai-founders", "chan-founder-intros", "user-eli", "Keyword tracking should catch crawl, search, validator, and outbound intent."),
    ("acct-growth", "guild-ai-founders", "chan-founder-intros", "user-dev", "UI should browse messages by account, guild, and channel without credentials."),
    ("acct-founder-export", "guild-ai-founders", "chan-founder-intros", "user-cyra", "Human-reviewed suggestions can later flow to Growth App, not auto-send."),
    ("acct-growth", "guild-ai-founders", "chan-founder-intros", "user-eli", "Fixture-only seed data makes demos safe while product shape is validated."),
]

DISCORD_FIXTURES["messages"] = [
    {
        "id": f"msg-{idx:02d}",
        "account_id": account_id,
        "guild_id": guild_id,
        "channel_id": channel_id,
        "author_user_id": user_id,
        "content": content,
        "sent_at": datetime(2026, 5, 12, 9, idx, tzinfo=timezone.utc).isoformat(),
        "raw": {"fixture": True, "sequence": idx},
    }
    for idx, (account_id, guild_id, channel_id, user_id, content) in enumerate(_MESSAGE_PLAN, start=1)
]

DISCORD_FIXTURES["lead_signals"] = [
    {
        "id": "signal-validator-quality",
        "account_id": "acct-growth",
        "guild_id": "guild-bittensor",
        "channel_id": "chan-alpha",
        "message_id": "msg-01",
        "keyword": "validator",
        "topic": "Subnet search quality monitoring",
        "summary": "Validator operators are asking for explainable search/crawl quality signals.",
        "evidence": ["msg-01", "msg-02", "msg-04"],
        "created_at": _CREATED_AT,
    },
    {
        "id": "signal-growth-evidence-pack",
        "account_id": "acct-growth",
        "guild_id": "guild-bittensor",
        "channel_id": "chan-growth",
        "message_id": "msg-09",
        "keyword": "lead",
        "topic": "Human-reviewed Growth evidence packs",
        "summary": "Growth handoff needs message evidence and explicit human approval.",
        "evidence": ["msg-09", "msg-11", "msg-12"],
        "created_at": _CREATED_AT,
    },
    {
        "id": "signal-founder-search-api",
        "account_id": "acct-founder-export",
        "guild_id": "guild-ai-founders",
        "channel_id": "chan-founder-intros",
        "message_id": "msg-14",
        "keyword": "search API",
        "topic": "AI founder search infrastructure intent",
        "summary": "Founders are asking for citation-backed AI search infrastructure.",
        "evidence": ["msg-14", "msg-16", "msg-17"],
        "created_at": _CREATED_AT,
    },
]
