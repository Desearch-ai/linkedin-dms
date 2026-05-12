# Discord Sync Real Product Live Validation — Task #1720 (fresh attempt #3)

## PR / QA rejection fix

- `pr_url`: https://github.com/Desearch-ai/linkedin-dms/pull/83
- Explicit rejection fix: created real GitHub PR https://github.com/Desearch-ai/linkedin-dms/pull/83 for `Desearch-ai/linkedin-dms`, with code/docs changes and a branch pushed to GitHub.

## Origin

- `origin_url`: https://discord.com/channels/1476670318469976148/1503553746783830097/1503656622554349569
- `origin_text`: Giga rejected fixture-prototype framing: “I need to build a real product… Where are the real accounts, server channel fetch, and authentication of a user?”

## Rollback / stop-the-line plan

- Do not send, delete, invite, DM, modify channels, change Discord server/account settings, or use personal Discord email/passwords.
- If local runtime starts, stop only the validation uvicorn process started for this task; do not stop unrelated services.
- If temp DBs/artifacts are created, remove only task-scoped temp paths after evidence is captured.
- If a branch/PR is created for this validation, rollback is: close the PR, delete the remote branch, and delete the local branch after preserving the artifact.
- If any command emits a secret/token/session/cookie, stop, redact the artifact/logs, and do not publish those logs.
- If scoped Discord Sync credentials are missing/invalid, stop live Discord fetch attempts and record the exact blocker + checked locations.

## Branch under test

- Repo: `/Users/giga/projects/desearch/linkedin-dms` (`Desearch-ai/linkedin-dms`).
- Requested source branch in task: `objective/o-89-build-real-discord-sync-product`.
- Actual available Task #1719 branch found locally/remotely: `objective/o-89-build-real-discord-sync-product-with-user-authen`.
- Base commit validated: `133fe5d`.
- Validation/fix branch: `task/1720-real-discord-validation-pr-proof`.

## Real product-path issue found and fixed

During validation, the Discord Sync runtime path needs to survive a DB that records `schema_version=5` while one or more `discord_*` tables are absent from an interrupted/partial local migration. I added an idempotent migration-5 replay guard and a regression test.

File-specific QA references:

- `libs/core/storage.py:286-292` replays Discord migration 5 if the current schema version is already 5+ but required Discord tables are missing.
- `libs/core/storage.py:294-307` defines/checks the exact required Discord table set.
- `tests/test_storage.py:408-472` creates a partial schema-version-5 DB and verifies migration self-heals all Discord tables.
- Existing live product path remains in `apps/api/main.py:566-737` and `apps/cli/__main__.py:115-180,293-383`.

## Credential/auth preflight (redacted)

Checked locations and result:

| Location | Result |
|---|---|
| Repo `.env*` under `/Users/giga/projects/desearch/linkedin-dms` | no env files found |
| Current process env | `DISCORD_SYNC_CLIENT_ID`, `DISCORD_SYNC_CLIENT_SECRET`, `DISCORD_SYNC_REDIRECT_URI`, `DISCORD_SYNC_BOT_TOKEN`, `DESEARCH_ENCRYPTION_KEY`, `DESEARCH_DB_PATH` all missing |
| macOS Keychain by service name | all required refs missing (`security` rc=44) |
| OpenClaw config/auth profile key-name audit | no usable `DISCORD_SYNC_*` refs found |

Exact blocker: **no usable persisted credential found** for `DISCORD_SYNC_CLIENT_ID`, `DISCORD_SYNC_CLIENT_SECRET`, `DISCORD_SYNC_REDIRECT_URI`, or `DISCORD_SYNC_BOT_TOKEN`. Because the scoped Discord Sync credentials are absent, this fresh attempt did not attempt OAuth login, guild fetch, channel fetch, message fetch, sends, deletes, invites, DMs, or server modifications.

## Runtime validation

Start command used for isolated fresh runtime DB:

```bash
cd /tmp/task1720-api-run-fresh3
PYTHONPATH=/Users/giga/projects/desearch/linkedin-dms \
  /Users/giga/projects/desearch/linkedin-dms/.venv/bin/python -m uvicorn apps.api.main:app --host 0.0.0.0 --port 8919
```

Reachable URLs:

- Local health: `http://127.0.0.1:8919/health` → HTTP 200 `{"ok":true}`
- Local UI: `http://127.0.0.1:8919/discord` → HTTP 200 HTML
- Tailscale health: `http://100.113.216.73:8919/health` → HTTP 200 `{"ok":true}`
- Tailscale UI: `http://100.113.216.73:8919/discord` → HTTP 200 HTML

UI evidence:

- Screenshot: `/Users/giga/.docs/cosmic-brain/projects/discord-sync/tasks/ops/artifacts-1720/discord-ui-missing-auth-state-fresh3.png`
- UI exposes live auth/status, connect URL, sync guilds, sync channels, sync messages, list/search messages, and error controls.
- Authenticated account/guild/channel/message screenshots are blocked by missing scoped Discord Sync credentials above.

## CLI/API evidence

API `/discord/auth/status` returned HTTP 200:

```json
{
  "ok": true,
  "config": {
    "oauth_configured": false,
    "missing_oauth": ["DISCORD_SYNC_CLIENT_ID", "DISCORD_SYNC_CLIENT_SECRET", "DISCORD_SYNC_REDIRECT_URI"],
    "bot_configured": false,
    "token_persistence_enabled": false,
    "credential_refs": ["DISCORD_SYNC_CLIENT_ID", "DISCORD_SYNC_CLIENT_SECRET", "DISCORD_SYNC_REDIRECT_URI", "DISCORD_SYNC_BOT_TOKEN"]
  },
  "accounts": []
}
```

API `/discord/auth/start` returned the expected blocker HTTP 503:

```json
{"detail":"Missing Discord OAuth config: DISCORD_SYNC_CLIENT_ID, DISCORD_SYNC_CLIENT_SECRET, DISCORD_SYNC_REDIRECT_URI"}
```

CLI `discord auth-status` exited 0 with the same missing-config state. CLI `discord auth-url` exited 1 with the expected blocker:

```text
error: Missing Discord OAuth config: DISCORD_SYNC_CLIENT_ID, DISCORD_SYNC_CLIENT_SECRET, DISCORD_SYNC_REDIRECT_URI
```

## DB evidence

Isolated runtime DB: `/tmp/task1720-api-run-fresh3/desearch_linkedin_dms.sqlite`.

Counts after blocker validation:

```json
{
  "discord_accounts": 0,
  "discord_guilds": 0,
  "discord_channels": 0,
  "discord_messages": 0,
  "discord_sync_errors": 0,
  "discord_oauth_states": 1
}
```

Notes:

- `discord_oauth_states=1` was created by the `/discord/auth/start` attempt before the expected missing-OAuth blocker response.
- No live records were inserted because no authenticated connection existed.
- No fixture records were inserted.

## Verification commands and exit status

- Artifact rollback plan write → 0.
- `git fetch origin --prune` → 0.
- Redacted credential preflight → 0.
- Partial-v5 DB self-heal smoke on copied DB → 0, `missing_after=[]`, `accounts=0`.
- `uv run pytest` → 0, `435 passed in 10.57s`.
- Isolated uvicorn runtime start → 0, port 8919 listening.
- Local/Tailscale health and UI HTTP checks → 0 / HTTP 200.
- API `/discord/auth/status` → 0 / HTTP 200.
- API `/discord/auth/start` → HTTP 503, accepted expected blocker due missing OAuth refs.
- CLI `discord auth-status` → 0.
- CLI `discord auth-url` → exit 1, accepted expected blocker due missing OAuth refs.
- First GitHub PR-create helper printed the PR URL but exited 1 because I placed a shell pipe on the heredoc terminator; rerun PR lookup exited 0 and confirmed the real PR URL.
- Headless Chrome screenshot wrote the PNG; Chrome process required SIGTERM after the screenshot was written, documented as non-critical because the artifact exists and runtime/UI HTTP checks passed.

## Irreversible changes

None to Discord or production runtime. GitHub PR creation is reversible by closing the PR and deleting the branch.

## Secrets review

No raw token, secret, Discord email/password, cookie, OAuth code, or session value appears in this artifact or the repo validation doc.
